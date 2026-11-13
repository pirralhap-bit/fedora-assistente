"""Fedora's voice: Piper (local TTS) streamed straight to the speakers (Windows: sounddevice via _Saida; Linux: pw-play)."""
import json
import os
import re
import subprocess
import sys
import threading

WIN = os.name == "nt"
_DADOS = os.path.join(os.environ.get("LOCALAPPDATA", ""), "fedora-assistente")
PIPER = os.path.expanduser(os.environ.get("PIPER_BIN", os.path.join(_DADOS, "piper", "piper.exe") if WIN else "~/.local/opt/piper/piper"))
VOICE = os.path.expanduser(os.environ.get("PIPER_VOICE", os.path.join(_DADOS, "voices", "pt_BR-faber-medium.onnx") if WIN else "~/.local/share/piper-voices/pt_BR-faber-medium.onnx"))
MAX_CHARS = 900
_lock = threading.Lock()
_procs = []
_fila = []        # (text, then) waiting for their turn; guarded by _lock
_worker = False   # True while the "speech" thread is draining _fila; guarded by _lock
MUDO = False      # voz muda: say() recusa sem enfileirar; so na memoria (reiniciar volta a falar); guarded by _lock


def mudo():
    return MUDO


def definir_mudo(ligado):
    """Liga/desliga o mudo da fala; ligar corta a fala em curso e esvazia a fila."""
    global MUDO
    with _lock:
        MUDO = bool(ligado)
    if MUDO:
        stop()
    return MUDO


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
        _fila[:] = []
        procs, _procs[:] = list(_procs), []
    for p in procs:
        if p.poll() is None:
            p.kill()


def speaking():
    with _lock:
        return bool(_fila or _worker or any(p.poll() is None for p in _procs))


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


def _drenar():
    """Worker: plays the queue one item at a time; ends (and frees the slot) when it is empty."""
    global _worker
    while True:
        with _lock:
            if not _fila:
                _worker = False
                return
            text, then = _fila.pop(0)
        try:
            _play(text, then)
        except Exception as e:  # noqa: BLE001 — a failed item (or its `then`) must not strand the queue with _worker stuck True
            print(f"speech: {type(e).__name__}: {e}", file=sys.stderr)


def say(text, then=None, fila=False):
    """Speak in the background without blocking. `fila=False` cuts off whatever was being said (and drops what was queued);
    `fila=True` waits its turn after it. `then` runs once this item's voice has finished on its own."""
    global _worker
    text = clean(text)
    if MUDO or not text or not ready():
        return False
    if not fila:
        stop()
    with _lock:
        if MUDO:   # mudo ligado entre a checagem e aqui: nao enfileira
            return False
        _fila.append((text, then))
        if not _worker:
            _worker = True
            threading.Thread(target=_drenar, daemon=True, name="speech").start()
    return True


if __name__ == "__main__":
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
    assert clean("```x```") == "" and len(clean("a" * 2000)) == MAX_CHARS
    voz = ready()

    # Queue, offline: _play and ready() are simulated, Piper is never touched.
    import time
    tocados, feitos = [], []

    class _Falso:
        def __init__(self):
            self.fim, self.morto = threading.Event(), False

        def poll(self):
            return 0 if self.fim.is_set() else None

        def kill(self):
            self.morto = True
            self.fim.set()

    def _play(text, then=None):  # noqa: F811 — test double: "plays" until released or killed
        p = _Falso()
        with _lock:
            _procs.append(p)
        tocados.append(text)
        p.fim.wait(5)
        with _lock:
            if p in _procs:
                _procs.remove(p)
        if then and not p.morto:
            then()

    def ready():  # noqa: F811
        return True

    def espera(cond, msg):
        fim = time.time() + 3
        while not cond():
            assert time.time() < fim, (msg, tocados)
            time.sleep(0.005)

    def libera():
        with _lock:
            _procs[0].fim.set()

    def falha():
        raise RuntimeError("then de teste")

    # 1. sequential: one at a time, in order, each `then` after its own item
    assert say("um", lambda: feitos.append("um"), fila=True) and say("dois", fila=True)
    assert say("três", lambda: feitos.append("três"), fila=True)
    espera(lambda: tocados == ["um"], "primeiro item")
    time.sleep(0.05)
    assert tocados == ["um"] and speaking() and feitos == [], (tocados, feitos)
    libera()
    espera(lambda: tocados == ["um", "dois"], "segundo item")
    assert feitos == ["um"] and speaking()
    libera()
    espera(lambda: tocados == ["um", "dois", "três"], "terceiro item")
    libera()
    espera(lambda: not speaking(), "fila esvaziar")
    assert feitos == ["um", "três"] and not _worker and not _fila and not _procs

    # 2. say(fila=False) cuts the current item, drops the queued one and speaks right away
    del tocados[:], feitos[:]
    say("a", lambda: feitos.append("a"), fila=True)
    say("b", fila=True)
    espera(lambda: tocados == ["a"], "item a")
    assert say("c", lambda: feitos.append("c"))
    espera(lambda: tocados == ["a", "c"], "interrupcao")
    libera()
    espera(lambda: not speaking(), "fim da interrupcao")
    assert tocados == ["a", "c"] and feitos == ["c"], (tocados, feitos)

    # 3. stop() kills the current item and empties the queue
    del tocados[:], feitos[:]
    say("x", lambda: feitos.append("x"), fila=True)
    say("y", fila=True)
    espera(lambda: tocados == ["x"], "item x")
    stop()
    espera(lambda: not speaking(), "stop")
    assert tocados == ["x"] and feitos == [] and not _fila and not _worker, (tocados, feitos)

    # 4. a `then` that raises does not strand the worker
    import io
    del tocados[:]
    err, sys.stderr = sys.stderr, io.StringIO()   # the worker logs the failure there; keep the test output clean
    try:
        say("e", falha, fila=True)
        say("f", fila=True)
        espera(lambda: tocados == ["e"], "item e")
        libera()
        espera(lambda: tocados == ["e", "f"], "fila segue depois de erro")
        libera()
        espera(lambda: not speaking(), "fim depois de erro")
    finally:
        log, sys.stderr = sys.stderr.getvalue(), err
    assert "then de teste" in log, log

    # 5. nothing to say / no voice: refuses without queueing
    assert say("**", fila=True) is False

    # 6. mudo: ligar corta a fala em curso e esvazia a fila; com mudo nada enfileira; desligar volta a falar
    del tocados[:], feitos[:]
    say("m1", lambda: feitos.append("m1"), fila=True)
    say("m2", fila=True)
    espera(lambda: tocados == ["m1"], "item m1")
    assert definir_mudo(True) is True and mudo()
    espera(lambda: not speaking(), "mudo corta")
    assert say("calada") is False and say("calada", fila=True) is False and not _fila and not speaking()
    assert tocados == ["m1"] and feitos == [], (tocados, feitos)
    assert definir_mudo(False) is False and not mudo()
    assert say("voltei", fila=True)
    espera(lambda: tocados == ["m1", "voltei"], "volta a falar")
    libera()
    espera(lambda: not speaking(), "fim depois do mudo")
    tocados[:] = ["e", "f"]

    ready = lambda: False  # noqa: E731
    assert say("oi", fila=True) is False and say("oi") is False and not speaking() and tocados == ["e", "f"]
    print("clean ok; fila ok; voz pronta:", voz)
