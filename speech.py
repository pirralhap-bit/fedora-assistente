"""Fedora's voice: Piper (local TTS) streamed straight to the speakers with pw-play."""
import json
import os
import re
import subprocess
import threading

PIPER = os.path.expanduser(os.environ.get("PIPER_BIN", "~/.local/opt/piper/piper"))
VOICE = os.path.expanduser(os.environ.get("PIPER_VOICE", "~/.local/share/piper-voices/pt_BR-faber-medium.onnx"))
MAX_CHARS = 900
_lock = threading.Lock()
_procs = []


def ready():
    return os.path.exists(PIPER) and os.path.exists(VOICE)


def _rate():
    try:
        with open(VOICE + ".json") as f:
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


def say(text):
    """Speak in the background, cutting off whatever was being said."""
    text = clean(text)
    if not text or not ready():
        return False
    stop()

    def run():
        tts = subprocess.Popen([PIPER, "-m", VOICE, "--output_raw"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        play = subprocess.Popen(["pw-play", "--rate", str(_rate()), "--channels", "1", "--format", "s16", "-"],
                                stdin=tts.stdout, stderr=subprocess.DEVNULL)
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

    threading.Thread(target=run, daemon=True, name="speech").start()
    return True


if __name__ == "__main__":
    assert clean("**Olá** [dry] veja [aqui](http://x.y) 🙂 ok") == "Olá veja aqui ok", clean("**Olá** [dry] veja [aqui](http://x.y) 🙂 ok")
    print("clean ok; voz pronta:", ready())
