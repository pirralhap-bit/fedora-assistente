"""Ollama model lifetime: loaded for as long as the Fedora window is open, unloaded soon after it closes."""
import json
import os
import subprocess
import threading
import time
import urllib.request

import sysinfo

MODEL = os.environ.get("JARVIS_OLLAMA_MODEL", "qwen3:8b")
GRACE = 30          # seconds after the window closes; an F5 reconnects well inside this
_lock = threading.Lock()
_open = 0
_timer = None


def keep_alive():
    """What every Ollama request should send, so a request never shortens the window's hold."""
    return -1 if _open else "10m"


def _call(keep):
    body = json.dumps({"model": MODEL, "keep_alive": keep}).encode()
    req = urllib.request.Request(sysinfo.OLLAMA_URL + "/api/generate", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        r.read()


def _load():
    # the service may be off after "desliga o Ollama"; opening the window brings it back
    subprocess.run(["systemctl", "--user", "start", "ollama"], capture_output=True, timeout=30)
    for _ in range(20):
        try:
            return _call(-1)
        except OSError:
            time.sleep(1)
    print("[modelo] Ollama não respondeu; modelo não carregado", flush=True)


def _unload():
    with _lock:
        if _open:
            return
    try:
        _call(0)
    except OSError:
        pass   # already stopped: nothing to unload


def window_opened():
    global _open, _timer
    with _lock:
        _open += 1
        if _timer:
            _timer.cancel()
            _timer = None
        first = _open == 1
    if first:
        threading.Thread(target=_load, daemon=True, name="modelo-load").start()


def window_closed():
    global _open, _timer
    with _lock:
        _open = max(0, _open - 1)
        if _open == 0:
            _timer = threading.Timer(GRACE, _unload)
            _timer.daemon = True
            _timer.start()


if __name__ == "__main__":
    calls = []
    _load = lambda: calls.append("load")          # noqa: E731
    _call = lambda keep: calls.append(keep)       # noqa: E731
    GRACE = 0.2
    window_opened(); time.sleep(0.05)
    assert calls == ["load"] and keep_alive() == -1, calls
    window_closed(); window_opened(); time.sleep(0.3)        # F5: reconnect inside the grace window
    assert 0 not in calls, calls                              # never unloaded (re-load of a loaded model is a no-op)
    window_closed(); time.sleep(0.4)
    assert calls[-1] == 0 and keep_alive() == "10m", calls
    print("modelo ok")
