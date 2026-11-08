"""Fedora's voice: Piper (local TTS) streamed straight to the speakers (Windows: sounddevice via _Saida; Linux: pw-play)."""
import json
import os
import re
import subprocess
import threading

WIN = os.name == "nt"
_DADOS = os.path.join(os.environ.get("LOCALAPPDATA", ""), "fedora-assistente")
PIPER = os.path.expanduser(os.environ.get("PIPER_BIN", os.path.join(_DADOS, "piper", "piper.exe") if WIN else "~/.local/opt/piper/piper"))
VOICE = os.path.expanduser(os.environ.get("PIPER_VOICE", os.path.join(_DADOS, "voices", "pt_BR-faber-medium.onnx") if WIN else "~/.local/share/piper-voices/pt_BR-faber-medium.onnx"))
MAX_CHARS = 900
_lock = threading.Lock()
_procs = []


def ready():
    return os.path.exists(PIPER) and os.path.exists(VOICE)


def _rate():
    try:
        with open(VOICE + ".json", encoding="utf-8") as f:
            return json.load(f)["audio"]["sample_rate"]
    except (OSError, KeyError, ValueError):
        return 22050


def clean(text):
    """Strip what should not be read aloud: markdown, links, stage directions, emoji."""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)          # [label](url) -> label
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[\[(][^\])]{0,60}[\])]", " ", t)              # [dry], (risos)
    t = re.sub(r"[*_#>`|~]+", " ", t)
    t = "".join(ch for ch in t if ch.isalnum() or ch.isspace() or ch in ".,;:!?%-'\"ºª°/")
    t = " ".join(t.split())
    return t[:MAX_CHARS]


def stop():
    with _lock:
        procs, _procs[:] = list(_procs), []
    for p in procs:
        if p.poll() is None:
            p.kill()


def speaking():
    with _lock:
        return any(p.poll() is None for p in _procs)


class _Saida:
    """Stand-in for the pw-play process on Windows: plays raw s16 mono through sounddevice, killable like a Popen."""
    def __init__(self, src, rate):
        self.returncode = None
        self._stop = False
        self._t = threading.Thread(target=self._run, args=(src, rate), daemon=True)
        self._t.start()

    def _run(self, src, rate):
        import sounddevice as sd
        try:
            with sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16") as out:
                while not self._stop:
                    buf = src.read(4096)
                    if not buf:
                        break
                    out.write(buf[:len(buf) - len(buf) % 2])
            self.returncode = 1 if self._stop else 0
        except Exception:  # noqa: BLE001 — no audio device: behave like a failed player
            self.returncode = 1

    def poll(self):
        return self.returncode

    def wait(self):
        self._t.join()

    def kill(self):
        self._stop = True


def _play(text, then=None):
    tts = subprocess.Popen([PIPER, "-m", VOICE, "--output_raw"], stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    # Windows plays through _Saida (sounddevice); Linux pw-play needs --raw: without it pw-play tries to sniff a file format on stdin and fails silently
    if WIN:
        play = _Saida(tts.stdout, _rate())
    else:
        play = subprocess.Popen(["pw-play", "--raw", "--rate", str(_rate()), "--channels", "1", "--format", "s16", "-"],
                                stdin=tts.stdout)
    if not WIN:
        tts.stdout.close()
    with _lock:
        _procs.extend([tts, play])
    try:
        tts.stdin.write(re.sub(r"(?<=[.!?])\s+", "\n", text).encode() + b"\n")  # one sentence per line
        tts.stdin.close()
    except BrokenPipeError:
        pass
    play.wait()
    tts.wait()
    with _lock:
        for p in (tts, play):
            if p in _procs:
                _procs.remove(p)
    if then and play.returncode == 0:   # finished on its own, not cut off by stop()
        then()


def say(text, then=None):
    """Speak in the background, cutting off whatever was being said. `then` runs once the voice has finished."""
    text = clean(text)
    if not text or not ready():
        return False
    stop()
    threading.Thread(target=_play, args=(text, then), daemon=True, name="speech").start()
    return True


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["--aviso-em"]:   # Windows timer: sleep, then the same notice
        import time
        time.sleep(float(sys.argv[2]))
        sys.argv[1:3] = ["--aviso"]
    if sys.argv[1:2] == ["--aviso"]:   # Linux timer (atalhos.timer via systemd-run); on Windows --aviso-em above (detached Popen)
        msg = " ".join(sys.argv[2:]) or "Seu timer terminou."
        if not WIN:
            subprocess.run(["notify-send", "-a", "Fedora", "-u", "critical", "Fedora", msg])
        _play(clean(msg))
        sys.exit(0)
    assert clean("**Olá** [dry] veja [aqui](http://x.y) 🙂 ok") == "Olá veja aqui ok", clean("**Olá** [dry] veja [aqui](http://x.y) 🙂 ok")
    print("clean ok; voz pronta:", ready())
