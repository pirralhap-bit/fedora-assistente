"""Always-on ear: PipeWire mic -> voice activity -> whisper.cpp. Wakes on "Ok, Fedora".

Audio never touches the disk: each speech segment is posted in memory to the local
whisper servers and dropped unless it starts with the wake phrase.
"""
import array
import difflib
import io
import json
import math
import os
import pathlib
import queue
import re
import shlex
import subprocess
import threading
import time
import unicodedata
import urllib.request
import uuid
import wave

import speech

RATE, FRAME = 16000, 320                      # 20 ms frames, 16-bit mono
STT_URL = os.environ.get("WHISPER_URL", "http://127.0.0.1:8178/inference")   # small model, kept loaded
NAME = os.environ.get("ASSISTANT_NAME", "Fedora")
DEBUG = os.environ.get("FEDORA_DEBUG") == "1"
HINT = f"Ok, {NAME}."     # initial prompt: tells whisper the name is a word it should expect
APP_URL = f"http://localhost:{os.environ.get('JARVIS_PORT', '8720')}/"
APP_CMD = os.environ.get("FEDORA_APP_CMD", "google-chrome-stable --app={url}")
PREFIX = {"ok", "okay", "okey", "oquei", "oke", "ei", "hey", "e"}
SILENCE = {"wake": 60, "command": 80, "dictate": 150}      # frames (20 ms) of quiet that end a segment; ~1 s pauses mid-sentence are normal
MAX_LEN = {"wake": 20 * 50, "command": 15 * 50, "dictate": 10 * 60 * 50}


def fold(text):
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode()


def strip_wake(text):
    """Return the command after "Ok, <name>", or None when the phrase is absent.
    The name may come out slightly wrong ("fedota", "fedor"), so it is matched by similarity."""
    t = " ".join(re.sub(r"[\[(][^\])]{0,40}[\])]", " ", text).split())
    words = re.findall(r"[\w']+", fold(t).lower())
    if len(words) < 2 or words[0] not in PREFIX:
        return None
    k = 2 if words[1] in ("a", "o") and len(words) > 2 else 1
    if difflib.SequenceMatcher(None, words[k], fold(NAME).lower()).ratio() < 0.66:
        return None
    # cut the original text right after the name word
    m = re.search(r"[\w']+", t)
    for _ in range(k + 1):
        end = m.end()
        m = re.compile(r"[\w']+").search(t, end)
    return t[end:].strip(" ,.!?;:")


def _wav(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def whisper(url, pcm, timeout=120):
    secs = len(pcm) / (2 * RATE)
    ctx = 0 if secs > 25 else min(1500, int(secs / 30 * 1500) + 128)  # short clips: smaller encoder, much faster
    b = uuid.uuid4().hex
    parts = [("response_format", b"json"), ("audio_ctx", str(ctx).encode()), ("prompt", HINT.encode())]
    body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode() + v + b"\r\n"
                    for k, v in parts)
    body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="s.wav"\r\n'
             f"Content-Type: audio/wav\r\n\r\n").encode() + _wav(pcm) + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(url, body, {"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        text = json.load(r).get("text", "")
    return " ".join(re.sub(r"[\[(][^\])]{0,40}[\])]", " ", text).split()).strip()


class Ear:
    def __init__(self):
        self.on = True
        self.discard_until = 0.0
        self.mode = "wake"
        self.deadline = 0.0
        self.force_end = False
        self.subs = []
        self.pending = []
        self.lock = threading.Lock()
        self.jobs = queue.Queue()
        self.proc = None
        self.error = None

    # ---- events to the page (Server-Sent Events) ----
    def subscribe(self):
        q = queue.Queue(maxsize=200)
        with self.lock:
            self.subs.append(q)
            for ev in self.pending:
                q.put(ev)
            self.pending.clear()
        q.put(dict(t="state", v=self.state()))
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def emit(self, ev, keep=False):
        with self.lock:
            subs = list(self.subs)
            if not subs and keep:
                self.pending = [e for e in self.pending if time.time() - e.get("at", 0) < 30] + [dict(ev, at=time.time())]
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass
        return bool(subs)

    def state(self):
        return "mute" if not self.on else {"wake": "idle", "command": "listen", "dictate": "dict", "busy": "think"}[self.mode]

    def _open_window(self):
        try:
            subprocess.Popen(shlex.split(APP_CMD.format(url=APP_URL)), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass

    # ---- control from the page ----
    def control(self, action):
        if action == "off":
            self.on = False
            self._stop_proc()
        elif action == "on":
            self.on = True
            self.mode = "wake"
        elif action in ("listen", "dictate"):
            if not self.on:
                return self.state()
            self.mode = "command" if action == "listen" else "dictate"
            self.deadline = time.time() + (8 if action == "listen" else 15)
            # drop what is already buffered (her own voice) and let the speaker go quiet: only speech that starts now counts
            self.discard_until = time.time() + 0.6
        elif action == "stop":
            self.force_end = True
            if self.mode != "wake":
                self.deadline = time.time()
        self.emit(dict(t="state", v=self.state()))
        return self.state()

    def _stop_proc(self):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()

    # ---- capture loop ----
    def start(self):
        threading.Thread(target=self._capture, daemon=True, name="ear-capture").start()
        threading.Thread(target=self._work, daemon=True, name="ear-work").start()

    def _capture(self):
        while True:
            if not self.on:
                time.sleep(0.3)
                continue
            try:
                self.proc = subprocess.Popen(["pw-record", "--rate", str(RATE), "--channels", "1",
                                              "--format", "s16", "-"], stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL)
                self.error = None
                self._listen(self.proc.stdout)
            except OSError as e:
                self.error = f"microfone: {e}"
            self._stop_proc()
            time.sleep(1)

    def _listen(self, stream):
        floor, loud, speech, quiet, pre = 300.0, 0, None, 0, []
        last_level = 0.0
        while self.on and self.proc:
            chunk = stream.read(FRAME * 2)
            if not chunk or len(chunk) < FRAME * 2:
                return
            s = array.array("h", chunk)
            lv = math.sqrt(sum(x * x for x in s) / FRAME)
            now = time.time()
            if now - last_level > 0.08:
                last_level = now
                self.emit(dict(t="level", v=round(min(1.0, lv / 4000), 3)))
            if self.mode == "busy" or now < self.discard_until:   # transcribing, or her voice still in the room
                speech, pre, loud = None, [], 0
                continue
            if self.mode in ("command", "dictate") and speech is None and now > self.deadline:
                self.mode = "wake"
                self.emit(dict(t="state", v=self.state()))
            thr = max(floor * 3, 450)
            if speech is None:
                floor = floor * 0.98 + lv * 0.02 if lv < thr else floor
                pre = (pre + [chunk])[-15:]                       # 300 ms pre-roll so the first syllable survives
                loud = loud + 1 if lv > thr else 0
                if loud >= 3:
                    speech, quiet = pre[:], 0
                    if self.mode != "wake":
                        self.emit(dict(t="state", v="listen" if self.mode == "command" else "dict"))
                continue
            speech.append(chunk)
            quiet = 0 if lv > thr else quiet + 1
            if quiet >= SILENCE[self.mode] or len(speech) >= MAX_LEN[self.mode] or self.force_end:
                self.force_end = False
                if len(speech) - quiet > 15:                      # ignore clicks shorter than ~0.3 s
                    self.jobs.put((self.mode, b"".join(speech)))
                    if self.mode != "wake":
                        self.mode = "busy"
                speech, pre, loud = None, [], 0

    # ---- transcription worker ----
    def _work(self):
        while True:
            mode, pcm = self.jobs.get()
            try:
                self._handle(mode, pcm)
            except Exception as e:  # noqa: BLE001 — a whisper hiccup must not kill the ear
                self.emit(dict(t="error", message=f"transcrição falhou: {str(e)[:160]}"))
                if self.mode == "busy":
                    self.mode = "wake"
                self.emit(dict(t="state", v=self.state()))

    def _handle(self, mode, pcm):
        if DEBUG:   # keep the last 30 clips in RAM (tmpfs, gone at reboot) to compare speech models on the real voice
            d = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "fedora-ouvido"
            d.mkdir(mode=0o700, exist_ok=True)
            (d / time.strftime(f"%H%M%S-{mode}.wav")).write_bytes(_wav(pcm))
            for old in sorted(d.glob("*.wav"))[:-30]:
                old.unlink()
        if mode == "wake":
            heard = whisper(STT_URL, pcm, timeout=30)
            rest = strip_wake(heard)
            if DEBUG:  # off by default: it writes what was said to the journal
                print(f"[ouvido] {len(pcm) / 32000:.1f}s ouvi={heard!r} -> {'comando=' + repr(rest) if rest is not None else 'ignorado'}", flush=True)
            if rest is None:
                return
            speech.stop()                                        # "Ok, Fedora" interrupts her
            self.emit(dict(t="wake"))
            if len(rest) < 2:                                     # just the name: wait for the command
                self.mode, self.deadline = "command", time.time() + 8
                if not self.emit(dict(t="state", v="listen")):
                    self._open_window()
                return
            if not self.emit(dict(t="command", text=rest), keep=True):
                self._open_window()
            return
        self.emit(dict(t="state", v="think"))
        text = whisper(STT_URL, pcm, timeout=600)
        if DEBUG:
            print(f"[ouvido] {mode} {len(pcm) / 32000:.1f}s ouvi={text!r}", flush=True)
        self.mode = "wake"
        if mode == "dictate":
            self.emit(dict(t="dictation", text=text), keep=True)
        else:
            cmd = strip_wake(text)
            self.emit(dict(t="command", text=cmd if cmd is not None else text), keep=True)
        self.emit(dict(t="state", v=self.state()))


EAR = Ear()


if __name__ == "__main__":
    # self-check of the wake phrase parser
    for said, want in [("Ok, Fedora.", ""), ("Okay Fedora, abre as notas diárias", "abre as notas diárias"),
                       ("OK, Fedora! Como está o sistema?", "Como está o sistema"), ("Ei Fedora ditar", "ditar"),
                       ("O Fedora travou de novo", None), ("Fedora", None), ("ok fedor, salvar", "salvar"),
                       ("Eu instalei o Fedora ontem", None), ("Ok, fedota!", ""), ("ok, feet, apré", None),
                       ("Ok, a moda com está o sistema.", None), ("Ok vamos lá", None)]:
        got = strip_wake(said)
        assert got == want, (said, got, want)
    print("wake phrase parser ok")
