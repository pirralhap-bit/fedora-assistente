"""Live machine numbers for the orbit: CPU, RAM, GPU busy/VRAM, disk, network, and how the last Gemini call went."""
import glob
import json
import os
import subprocess
import threading
import time

import llm

_LOCK = threading.Lock()
_prev = {}


def _cpu():
    if os.name == "nt":
        import psutil
        return psutil.cpu_percent(interval=None)
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
    if os.name == "nt":
        import psutil
        m = psutil.virtual_memory()
        return round(m.used / 2**30, 1), round(m.total / 2**30, 1)
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            m[k] = int(v.split()[0])  # kB
    return round((m["MemTotal"] - m["MemAvailable"]) / 2**20, 1), round(m["MemTotal"] / 2**20, 1)


# Windows: one long-lived PowerShell reads the GPU performance counters every 2 s (Get-Counter is too slow to spawn per request).
# AMD, Intel and NVIDIA all expose "GPU Engine" and "GPU Adapter Memory"; busy = the busiest engine type, VRAM = the biggest adapter.
_PS_GPU = r"""
$ErrorActionPreference='SilentlyContinue'
$pai=__PAI__
$total=(Get-CimInstance Win32_VideoController | Sort-Object AdapterRAM -Descending | Select-Object -First 1).AdapterRAM
Get-Counter -Counter '\GPU Engine(*)\Utilization Percentage','\GPU Adapter Memory(*)\Dedicated Usage' -SampleInterval 2 -Continuous | ForEach-Object {
  if(-not (Get-Process -Id $pai)){ exit }   # o servidor parou: este leitor não fica órfão
  $eng=@{}; $vram=0
  foreach($c in $_.CounterSamples){
    if($c.Path -like '*gpu engine*'){
      if($c.InstanceName -match 'luid_(0x[0-9a-f]+_0x[0-9a-f]+).*engtype_(\w+)'){ $k=$Matches[1]+'|'+$Matches[2]; $eng[$k]=[double]$eng[$k]+$c.CookedValue }
    } elseif($c.CookedValue -gt $vram){ $vram=$c.CookedValue }
  }
  $busy=0; foreach($v in $eng.Values){ if($v -gt $busy){ $busy=$v } }
  Write-Output ('{"busy":'+[math]::Round([math]::Min(100,$busy))+',"vram":'+[long]$vram+',"total":'+[long]$total+'}')
}
"""
_GPU_WIN = {"v": None, "at": 0.0, "proc": None}


def _gpu_reader(proc):
    for line in proc.stdout:
        try:
            d = json.loads(line)
            _GPU_WIN["v"] = dict(busy=int(d["busy"]), vram=round(d["vram"] / 2**30, 1),
                                 vram_total=round(max(d["total"], d["vram"]) / 2**30, 1))
            _GPU_WIN["at"] = time.time()
        except (ValueError, KeyError, TypeError):
            continue


def _gpu_win():
    with _LOCK:
        if _GPU_WIN["proc"] is None:
            try:
                proc = subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_GPU.replace("__PAI__", str(os.getpid()))],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError:
                _GPU_WIN["proc"] = False
                return None
            _GPU_WIN["proc"] = proc
            threading.Thread(target=_gpu_reader, args=(proc,), daemon=True).start()
    if _GPU_WIN["proc"] is False or time.time() - _GPU_WIN["at"] > 10:
        return None   # no reading yet (first seconds) or the reader died
    return _GPU_WIN["v"]


def _gpu():
    if os.name == "nt":
        return _gpu_win()
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


def _disk():
    try:
        import psutil
        drive = (os.environ.get("SystemDrive") or "C:") + "\\" if os.name == "nt" else "/"
        d = psutil.disk_usage(drive)
        return dict(nome=drive.rstrip("\\") or "/", used=round(d.used / 2**30, 1), total=round(d.total / 2**30, 1))
    except Exception:
        return None


def _net():
    """Bytes per second down/up since the previous call (None until there are two samples)."""
    try:
        import psutil
        c = psutil.net_io_counters()
    except Exception:
        return None
    now = time.time()
    with _LOCK:
        p = _prev.get("net")
        if p and now - p[0] < 0.5:
            return p[3]   # called twice in a row: keep the last rate
        rate = None
        if p and now > p[0]:
            rate = dict(down=max(0, (c.bytes_recv - p[1]) / (now - p[0])), up=max(0, (c.bytes_sent - p[2]) / (now - p[0])))
        _prev["net"] = (now, c.bytes_recv, c.bytes_sent, rate)
    return rate


def snapshot():
    used, total = _ram()
    return dict(cpu=_cpu(), threads=os.cpu_count(), ram=used, ram_total=total,
                gpu=_gpu(), disk=_disk(), net=_net(), gemini=dict(llm.STATUS) or None, at=time.time())
