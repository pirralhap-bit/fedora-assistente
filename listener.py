"""Ouvido da Fedora (Windows): push-to-talk. O microfone só abre quando você pede.

Gatilhos: atalho global (padrão Ctrl+Alt+Espaço), clique no buraco negro / Espaço na janela, ou
a própria Fedora fazendo uma pergunta. Nada de microfone aberto o tempo todo: não há como ele
captar música, vídeo ou conversa ao lado. O áudio nunca vai pro disco; fica na RAM só até a
transcrição e depois é descartado.

Fluxo: gatilho -> grava (Silero VAD acha o começo e o fim da fala) -> faster-whisper -> evento
"command" (ou "dictation") para a página.
"""
import array
import io
import json
import math
import os
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
import vad

RATE, FRAME = 16000, 512                      # blocos de 32 ms, 16 bits mono (o que o Silero espera)
FPS = RATE / FRAME
STT_URL = os.environ.get("WHISPER_URL", "http://127.0.0.1:8178/inference")
NAME = os.environ.get("ASSISTANT_NAME", "Fedora")
DEBUG = os.environ.get("FEDORA_DEBUG") == "1"
HOTKEY = os.environ.get("FEDORA_HOTKEY", "ctrl+alt+space")
HINT = f"{NAME}, Steam, Obsidian, notas diárias."    # vocabulário esperado, sem frase pronta (frase pronta vira alucinação)
APP_URL = f"http://localhost:{os.environ.get('JARVIS_PORT', '8720')}/"
_EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
APP_CMD = os.environ.get("FEDORA_APP_CMD", f'"{_EDGE}" --app={{url}}')
PREFIX = {"ok", "okay", "okey", "oquei", "oke", "ei", "hey", "e"}

START_TIMEOUT = {"command": 10.0, "dictate": 15.0}     # s esperando você começar a falar
SILENCE = {"command": 3.0, "dictate": 4.0}             # s de silêncio que encerram a fala
MAX_SECS = {"command": 90, "dictate": 600}
MIN_SPEECH = 0.4                                        # s; menos que isso é clique/tosse
AVISO_OCUPADA = "Espere a resposta (ou Esc)."           # atalho/clique com um turno do Claude rodando
# o que o Whisper escreve quando ouve silêncio ou ruído (fim de vídeo do YouTube, créditos de legenda).
# Casado com o texto inteiro, já sem acento, pontuação e caixa: "obrigado, abre a steam" passa; "Obrigado." sozinho não
# (um "obrigado" solto não é pedido nenhum).
_LIXO = [
    r"(muito )?obrigad[oa]s?( a todos| pessoal| gente| galera)?",
    r"(muito )?obrigad[oa]s? (por|pela|pelo) (assistir|assistirem|ver|verem|atencao|audiencia|acompanhar)\b.*",
    r"tchau( tchau)*( pessoal| galera| gente)?", r"ate (a proxima|o proximo|mais|logo)\b.*",
    r"beijos?( pessoal| galera)?", r"e ai", r"e ai (pessoal|galera|gente)\b.*", r"(ola|oi) (pessoal|galera)\b.*", r"voce",
    # only the exact end-of-video shapes: "não esqueça de ligar pro João" and "legendas em português no VLC" are requests
    r"(se )?inscrev(a|am|e)( se)?( no canal)?( e ativ(e|a|em) o sininho)?( e deix(e|a|em) (seu|o|um) like)?",
    r"nao (se )?esquec(a|am) de (se )?inscrever( se)?( no canal)?( e deix(ar|e) (seu|o|um) like)?( e ativar o sininho)?",
    r"(deixe|deixa|deixem) (seu|o|um) like( no video)?( e se inscrev(a|am|e)( no canal)?)?",
    r"ativ(e|a|em) o sininho( de notificacoes| das notificacoes)?",
    r"legendas? (pela comunidade( do)?( amara org)?|adriana zanotto)", r"legendado por .{0,40}", r"amara org",
]
JUNK = re.compile(r"^(?:" + "|".join(_LIXO) + r")$")


def fold(text):
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode()


def strip_wake(text):
    """Tira um "Ok, Fedora" opcional do começo (hábito de quem já usou a versão antiga)."""
    t = " ".join(text.split())
    words = re.findall(r"[\w']+", fold(t).lower())
    if len(words) >= 2 and words[0] in PREFIX and words[1] in (fold(NAME).lower(), "fedor", "fedota", "fedura"):
        m = re.match(r"\W*[\w']+\W+[\w']+[\s,.!?;:]*", t)
        return t[m.end():].strip() if m else t
    if words and words[0] == fold(NAME).lower():
        m = re.match(r"\W*[\w']+[\s,.!?;:]*", t)
        return t[m.end():].strip() if m else t
    return t


def limpa(text):
    """Texto do Whisper -> texto útil, ou "" se parece alucinação."""
    t = " ".join(re.sub(r"[\[(][^\])]{0,40}[\])]", " ", text).split()).strip()
    n = " ".join(re.sub(r"[^\w\s]|_", " ", fold(t).lower()).split())
    if not n or JUNK.match(n):   # só pontuação ("...") também é lixo
        return ""
    words = t.lower().split()
    if len(words) >= 6 and len(set(words)) <= len(words) // 3:   # "vamos vamos vamos vamos..."
        return ""
    return t


def _wav(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def whisper(url, pcm, timeout=120):
    b = uuid.uuid4().hex
    parts = [("response_format", b"json"), ("prompt", HINT.encode())]
    body = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode() + v + b"\r\n"
                    for k, v in parts)
    body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="s.wav"\r\n'
             f"Content-Type: audio/wav\r\n\r\n").encode() + _wav(pcm) + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(url, body, {"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return limpa(json.load(r).get("text", ""))


class Ear:
    def __init__(self):
        self.on = True
        self.mode = None            # None | "command" | "dictate"  (gravando)
        self.thinking = False       # transcrevendo
        self.force_end = False
        self.cancel = False
        self.error = None
        self.subs, self.pending = [], []
        self.passivos = set()   # ids das filas que só olham (o orb): não contam como "página aberta"
        self.lock = threading.Lock()
        self.rec_lock = threading.Lock()   # uma gravação por vez
        self.silero = None
        self.busy = lambda: False          # o server.py liga isto ao RUN_LOCK: há um turno do Claude rodando?

    # ---- eventos para a página (Server-Sent Events) ----
    def subscribe(self, passivo=False):
        """passivo=True: recebe tudo (nível, estado), mas não conta como página e não leva os comandos pendentes."""
        q = queue.Queue(maxsize=200)
        with self.lock:
            self.subs.append(q)
            if passivo:
                self.passivos.add(id(q))
            else:
                for ev in self.pending:
                    q.put(ev)
                self.pending.clear()
        q.put(dict(t="state", v=self.state()))
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)
            self.passivos.discard(id(q))

    def emit(self, ev, keep=False):
        """True se alguma página (assinante não passivo) recebeu; keep=True guarda o evento por 30 s se nenhuma."""
        with self.lock:
            subs = list(self.subs)
            paginas = [q for q in subs if id(q) not in self.passivos]
            if not paginas and keep:
                self.pending = [e for e in self.pending if time.time() - e.get("at", 0) < 30] + [dict(ev, at=time.time())]
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass
        return bool(paginas)

    def _deliver(self, ev):
        """Um comando/ditado para a página; com a janela fechada ele fica pendente e a janela abre."""
        if not self.emit(ev, keep=True):
            self._open_window()

    def state(self):
        if not self.on:
            return "mute"
        if self.thinking:
            return "think"
        return {None: "idle", "command": "listen", "dictate": "dict"}[self.mode]

    def _set(self):
        self.emit(dict(t="state", v=self.state()))

    def _open_window(self):
        try:
            args = [a.strip('"') for a in shlex.split(APP_CMD.format(url=APP_URL), posix=False)]
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    # ---- controle (página, atalho global, voz da Fedora) ----
    def control(self, action):
        if action in ("focus", "blur"):
            return self.state()
        if action == "off":
            self.on = False
            self.cancel = True
        elif action == "on":
            self.on = True
        elif action in ("listen", "dictate"):
            self._start("command" if action == "listen" else "dictate")
        elif action == "stop":
            self.force_end = True
        self._set()
        return self.state()

    def _ocupada(self):
        """Turno do Claude rodando: não abre o microfone (o comando novo seria descartado e a resposta apagada)."""
        try:
            ocupada = bool(self.busy())
        except Exception:  # noqa: BLE001
            ocupada = False
        if ocupada:
            self.emit(dict(t="aviso", message=AVISO_OCUPADA))
        return ocupada

    def _start(self, mode):
        if not self.on or self.mode or self.thinking:
            return
        if mode == "command" and self._ocupada():   # ditado não passa pelo Claude: pode
            return
        self.mode = mode
        self.force_end = self.cancel = False
        threading.Thread(target=self._record, args=(mode,), daemon=True, name="ear-rec").start()

    def hotkey(self):
        """Atalho global: grava / encerra a gravação / corta a fala dela e já grava.
        Com um turno rodando, só avisa: interromper é pelo Esc, nunca implícito."""
        if self.mode:
            self.force_end = True
        elif self._ocupada():
            return
        elif speech.speaking():
            speech.stop()
            self.control("listen")
            if not self.emit(dict(t="wake")):
                self._open_window()
        elif not self.thinking:
            self.control("listen")
            if not self.emit(dict(t="wake")):
                self._open_window()

    # ---- gravação ----
    def _record(self, mode):
        import sounddevice as sd
        try:
            with self.rec_lock:
                if self.silero is None:
                    self.silero = vad.load()
                self.silero and self.silero.reset()
                time.sleep(0.25 if speech.speaking() else 0.0)
                chunks, voiced_n, quiet, started = [], 0, 0, False
                t0 = time.time()
                last_level = 0.0
                self.error = None
                self._set()
                with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME) as mic:
                    while self.on and not self.cancel:
                        data, _ = mic.read(FRAME)
                        chunk = bytes(data)
                        s = array.array("h", chunk)
                        lv = math.sqrt(sum(x * x for x in s) / FRAME)
                        now = time.time()
                        if now - last_level > 0.08:
                            last_level = now
                            self.emit(dict(t="level", v=round(min(1.0, lv / 4000), 3)))
                        if self.silero:
                            voiced = self.silero.prob(chunk) > (0.35 if started else 0.5)
                        else:
                            voiced = lv > 700
                        chunks.append(chunk)
                        if voiced:
                            voiced_n += 1
                            quiet = 0
                            started = started or voiced_n >= 3
                        else:
                            quiet = quiet + 1 if started else 0
                        if not started and now - t0 > START_TIMEOUT[mode] and not self.force_end:
                            break
                        if started and quiet >= int(SILENCE[mode] * FPS):
                            break
                        if len(chunks) >= int(MAX_SECS[mode] * FPS) or (self.force_end and started):
                            break
                        if self.force_end and not started:
                            break
        except Exception as e:  # noqa: BLE001
            self.error = f"microfone: {e}"
            self.emit(dict(t="error", message=self.error))
            started, chunks = False, []
        cancelled = self.cancel or not self.on
        self.mode, self.force_end, self.cancel = None, False, False
        if cancelled or not started or voiced_n / FPS < MIN_SPEECH:
            self._set()
            if not cancelled and not self.error:
                self.emit(dict(t="error", message="Não ouvi nada. Toque no buraco negro ou use Ctrl+Alt+Espaço e fale."))
            return
        pcm = b"".join(chunks)
        self.thinking = True
        self._set()
        try:
            text = whisper(STT_URL, pcm, timeout=600)
        except Exception as e:  # noqa: BLE001 — falha do STT não pode derrubar o ouvido
            self.thinking = False
            self.emit(dict(t="error", message=f"transcrição falhou: {str(e)[:160]}"))
            self._set()
            return
        if DEBUG:
            print(f"[ouvido] {mode} {len(pcm) / 32000:.1f}s ouvi={text!r}", flush=True)
        self.thinking = False
        if mode == "dictate":
            self.emit(dict(t="dictation", text=text), keep=True)
        else:
            self._deliver(dict(t="command", text=strip_wake(text)))
        self._set()

    # ---- atalho global (RegisterHotKey, sem dependências) ----
    def _hotkey_loop(self):
        import ctypes
        from ctypes import wintypes
        mods = {"ctrl": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}
        keys = {"space": 0x20, **{chr(c): c for c in range(0x30, 0x3A)}, **{chr(c).lower(): c for c in range(0x41, 0x5B)},
                **{f"f{i}": 0x6F + i for i in range(1, 13)}}
        m, vk = 0x4000, None            # MOD_NOREPEAT
        for part in HOTKEY.lower().replace(" ", "").split("+"):
            if part in mods:
                m |= mods[part]
            else:
                vk = keys.get(part)
        u = ctypes.windll.user32
        if vk is None or not u.RegisterHotKey(None, 1, m, vk):
            self.emit(dict(t="error", message=f"Atalho {HOTKEY} indisponível (já em uso?). Use o clique no buraco negro."))
            return
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:   # WM_HOTKEY
                try:
                    self.hotkey()
                except Exception as e:  # noqa: BLE001
                    print(f"[atalho] {e}", flush=True)

    def start(self):
        threading.Thread(target=self._hotkey_loop, daemon=True, name="ear-hotkey").start()


EAR = Ear()


if __name__ == "__main__":
    for said, want in [("Ok, Fedora, abre a Steam", "abre a Steam"), ("Fedora, que horas são?", "que horas são?"),
                       ("Ei Fedora ditar", "ditar"), ("abre o Obsidian", "abre o Obsidian"),
                       ("O Fedora travou", "O Fedora travou"), ("Ok, Fedora.", "")]:
        got = strip_wake(said)
        assert got == want, (said, got, want)
    for lixo in ["Obrigado.", "Legendas pela comunidade Amara.org", "vamos vamos vamos vamos vamos vamos vamos", "...",
                 "Obrigado por assistir!", "Obrigada por assistirem o vídeo.", "Muito obrigado.", "Tchau, tchau.", "Tchau!",
                 "E aí, pessoal.", "E aí, galera, tudo bem?", "Inscreva-se no canal", "Se inscreva no canal e ative o sininho!",
                 "Legendas pela comunidade.", "Legenda Adriana Zanotto", "Até a próxima!", "Até a próxima, pessoal.",
                 "Não se esqueça de se inscrever no canal.", "Deixe seu like!", "Você", "Beijos!",
                 "legendas pela comunidade amara.org", "não esqueça de se inscrever no canal",
                 "(música)", "[Música]", "Olá pessoal, sejam bem-vindos"]:
        assert limpa(lixo) == "", lixo
    for ok in ["abre a Steam", "abre o youtube", "que horas são?", "obrigado, agora abre a steam", "e aí, que horas são?",
               "valeu, abre o chrome", "até que horas o mercado abre?", "você pode abrir o chrome?", "legal, abre o obsidian",
               "Não esqueça de ligar pro João às 5", "legendas em português no VLC", "sim", "pode", "confirma", "valeu"]:
        assert limpa(ok) == ok, (ok, limpa(ok))

    # 1) o orb assina como passivo: um comando com a janela fechada fica pendente e a janela abre
    ear = Ear()
    abriu = []
    ear._open_window = lambda: abriu.append(1)
    orb_q = ear.subscribe(passivo=True)
    ear._deliver(dict(t="command", text="abre a steam"))
    assert abriu == [1] and [e["text"] for e in ear.pending] == ["abre a steam"], (abriu, ear.pending)
    ear.emit(dict(t="level", v=0.5))
    vistos = [orb_q.get_nowait()["t"] for _ in range(orb_q.qsize())]
    assert vistos == ["state", "command", "level"], vistos            # o orb continua vendo nível e estado
    page_q = ear.subscribe()                                           # a página abre: recebe o pendente
    assert page_q.get_nowait()["t"] == "command" and not ear.pending
    ear._deliver(dict(t="command", text="x"))
    assert abriu == [1], "com página aberta não abre outra janela"
    ear.unsubscribe(page_q)
    ear.unsubscribe(orb_q)
    assert not ear.subs and not ear.passivos

    # 5) atalho / clique com um turno do Claude rodando: não abre o microfone, avisa
    ear = Ear()
    ear._open_window = lambda: None
    ear.busy = lambda: True
    q = ear.subscribe()
    q.get_nowait()
    ear.hotkey()
    ear.control("listen")
    assert ear.mode is None, ear.mode
    avisos = [e for e in (q.get_nowait() for _ in range(q.qsize())) if e["t"] == "aviso"]
    assert len(avisos) == 2 and avisos[0]["message"] == AVISO_OCUPADA, avisos
    ear.busy = lambda: 1 / 0                                           # um busy quebrado não trava o ouvido
    assert ear._ocupada() is False
    print("listener ok")
