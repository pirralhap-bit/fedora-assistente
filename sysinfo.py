"""Live machine numbers for the orbit: CPU, RAM, AMD GPU busy/VRAM, and what Ollama has loaded."""
import glob
import json
import os
import threading
import time
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
_LOCK = threading.Lock()
_prev = {}


def _cpu():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle, total = v[3] + v[4], sum(v)
    with _LOCK:
        p = _prev.get("cpu")
        _prev["cpu"] = (idle, total)
    if not p or total == p[1]:
        return None
    return round(100 * (1 - (idle - p[0]) / (total - p[1])), 1)


def _ram():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            m[k] = int(v.split()[0])  # kB
    return round((m["MemTotal"] - m["MemAvailable"]) / 2**20, 1), round(m["MemTotal"] / 2**20, 1)


def _gpu():
    for busy in glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"):
        d = os.path.dirname(busy)
        try:
            read = lambda n: int(open(os.path.join(d, n)).read())  # noqa: E731
            return dict(busy=read("gpu_busy_percent"),
                        vram=round(read("mem_info_vram_used") / 2**30, 1),
                        vram_total=round(read("mem_info_vram_total") / 2**30, 1))
        except (OSError, ValueError):
            continue
    return None


def ollama():
    """Models Ollama has in memory right now, or None when it is not running."""
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/ps", timeout=0.4) as r:
            return [m.get("name") for m in json.load(r).get("models", [])]
    except (OSError, ValueError):
        return None


def snapshot():
    used, total = _ram()
    return dict(cpu=_cpu(), threads=os.cpu_count(), ram=used, ram_total=total,
                gpu=_gpu(), ollama=ollama(), at=time.time())
