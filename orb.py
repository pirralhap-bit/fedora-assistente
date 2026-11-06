"""Buraco negro flutuante (Windows): janela sem borda, sempre no topo, com transparência por pixel.

    python orb.py            # o server.py já sobe isto sozinho (FEDORA_ORB=0 desliga)

Clique = falar / encerrar a gravação. Arrastar = mover (a posição fica salva). Botão direito = menu.
Ele só mostra o que o servidor manda em /api/events (nível do microfone e estado); some sozinho se o servidor cair.
"""
import ctypes
import json
import math
import os
import pathlib
import subprocess
import sys
import threading
import time
import urllib.request
from ctypes import wintypes

import numpy as np

PORT = os.environ.get("JARVIS_PORT", "8720")
BASE = f"http://localhost:{PORT}"
DADOS = pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home())) / "fedora-assistente"
POS_FILE = DADOS / "orb.json"
SIZE, HOLE = 150, 104
FPS = 30

# estado visual: (respiração, velocidade do anel, brilho)
ESTADOS = {"idle": (1.6, 0.7, 0.55), "listen": (2.6, 1.2, 1.0), "dict": (2.6, 1.2, 1.0),
           "think": (4.0, 2.6, 0.9), "mute": (0.8, 0.0, 0.22)}

_Y, _X = np.mgrid[0:SIZE, 0:SIZE].astype(np.float32)
_D = np.hypot(_X - (SIZE - 1) / 2, _Y - (SIZE - 1) / 2)
_A = np.arctan2(_Y - (SIZE - 1) / 2, _X - (SIZE - 1) / 2)


def frame(t, level, state):
    """BGRA pré-multiplicado, SIZE x SIZE."""
    resp, giro, brilho = ESTADOS.get(state, ESTADOS["idle"])
    breath = 0.5 + 0.5 * math.sin(t * resp)
    r = HOLE * 0.17 * (1 + 0.10 * breath + 0.45 * level)
    glow = r * (2.0 + 0.6 * level)
    # brilho de acreção âmbar
    u = np.clip((_D - r * 0.9) / (glow - r * 0.9), 0, 1)
    ga = np.interp(u, [0, 0.35, 1], [0.85, 0.35 + 0.3 * level, 0.0]) * brilho
    gr = np.interp(u, [0, 0.35, 1], [1.0, 0.95, 0.6])
    gg = np.interp(u, [0, 0.35, 1], [0.72, 0.50, 0.25])
    gb = np.interp(u, [0, 0.35, 1], [0.30, 0.12, 0.05])
    a = ga
    rgb = np.stack([gr * ga, gg * ga, gb * ga], axis=-1)
    # anel fino que gira
    spin = t * giro
    in_arc = ((_A - spin) % (2 * math.pi)) < math.pi * 1.2
    ring = np.clip(1.2 - np.abs(_D - r * 1.25), 0, 1) * in_arc * 0.75 * (0.5 + 0.5 * brilho)
    rgb = rgb * (1 - ring[..., None]) + np.array([1.0, 0.82, 0.5]) * ring[..., None]
    a = a + (1 - a) * ring
    # horizonte de eventos (preto)
    h = np.clip(r - _D + 0.5, 0, 1)
    rgb = rgb * (1 - h[..., None])
    a = a + (1 - a) * h
    out = np.empty((SIZE, SIZE, 4), np.uint8)
    out[..., 0] = np.clip(rgb[..., 2] * 255, 0, 255)
    out[..., 1] = np.clip(rgb[..., 1] * 255, 0, 255)
    out[..., 2] = np.clip(rgb[..., 0] * 255, 0, 255)
    out[..., 3] = np.clip(a * 255, 0, 255)
    return out


# ---------------- janela em camadas (Win32) ----------------
u32, g32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("gdi32", use_last_error=True)
u32.GetDC.restype = wintypes.HDC
u32.GetDC.argtypes = [wintypes.HWND]
u32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
g32.CreateCompatibleDC.restype = wintypes.HDC
g32.CreateCompatibleDC.argtypes = [wintypes.HDC]
g32.CreateDIBSection.restype = wintypes.HBITMAP
g32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
g32.SelectObject.restype = wintypes.HGDIOBJ
u32.GetAncestor.restype = wintypes.HWND
u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
u32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
u32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p, wintypes.HDC,
                                    ctypes.c_void_p, wintypes.COLORREF, ctypes.c_void_p, wintypes.DWORD]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BLEND(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class Camada:
    def __init__(self, hwnd):
        self.hwnd = hwnd
        ex = u32.GetWindowLongW(hwnd, -20)
        u32.SetWindowLongW(hwnd, -20, ex | 0x80000 | 0x80 | 0x08000000)   # LAYERED | TOOLWINDOW | NOACTIVATE
        self.screen = u32.GetDC(None)
        self.mem = g32.CreateCompatibleDC(self.screen)
        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), SIZE, -SIZE, 1, 32, 0)
        self.bits = ctypes.c_void_p()
        self.bmp = g32.CreateDIBSection(self.mem, ctypes.byref(bi), 0, ctypes.byref(self.bits), None, 0)
        g32.SelectObject(self.mem, self.bmp)
        self.blend = BLEND(0, 0, 255, 1)

    def desenha(self, bgra, x, y):
        ctypes.memmove(self.bits, bgra.tobytes(), SIZE * SIZE * 4)
        pos, tam, orig = wintypes.POINT(x, y), wintypes.SIZE(SIZE, SIZE), wintypes.POINT(0, 0)
        u32.UpdateLayeredWindow(self.hwnd, self.screen, ctypes.byref(pos), ctypes.byref(tam), self.mem,
                                ctypes.byref(orig), 0, ctypes.byref(self.blend), 2)   # ULW_ALPHA


# ---------------- ligação com o servidor ----------------
def _token():
    try:
        return (DADOS / "fedora-assistente.token").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ear(action):
    req = urllib.request.Request(BASE + "/api/ear", json.dumps({"action": action}).encode(), {
        "Content-Type": "application/json", "Origin": BASE, "X-Jarvis-Token": _token()})
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except OSError:
        pass


def abrir_janela():
    edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    subprocess.Popen([edge, f"--app={BASE}/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    import tkinter as tk

    st = dict(level=0.0, shown=0.0, state="idle", alive=time.time())

    def sse():
        falhas = 0
        while True:
            try:
                with urllib.request.urlopen(BASE + "/api/events", timeout=40) as r:
                    falhas = 0
                    for raw in r:
                        st["alive"] = time.time()
                        if not raw.startswith(b"data:"):
                            continue
                        ev = json.loads(raw[5:])
                        if ev.get("t") == "level":
                            st["level"] = max(st["level"], min(1.0, float(ev.get("v", 0)) * 2.5))
                        elif ev.get("t") == "state":
                            st["state"] = ev.get("v", "idle")
            except (OSError, ValueError):
                pass
            falhas += 1
            if falhas > 15:           # servidor sumiu: o buraco negro vai junto
                os._exit(0)
            time.sleep(2)
    threading.Thread(target=sse, daemon=True).start()

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    pos = dict(x=sw - SIZE - 24, y=sh - SIZE - 80)
    try:
        saved = json.loads(POS_FILE.read_text(encoding="utf-8"))
        if 0 <= saved["x"] <= sw - 40 and 0 <= saved["y"] <= sh - 40:
            pos = saved
    except (OSError, ValueError, KeyError):
        pass
    root.geometry(f"{SIZE}x{SIZE}+{pos['x']}+{pos['y']}")
    root.update_idletasks()
    camada = Camada(u32.GetAncestor(root.winfo_id(), 2))   # GA_ROOT

    drag = dict(x=0, y=0, moved=False)
    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="Abrir janela da Fedora", command=abrir_janela)
    menu.add_command(label="Microfone desligado / ligado", command=lambda: ear("on" if st["state"] == "mute" else "off"))
    menu.add_separator()
    menu.add_command(label="Esconder o buraco negro", command=lambda: os._exit(0))

    def press(e):
        drag.update(x=e.x_root - pos["x"], y=e.y_root - pos["y"], moved=False, x0=e.x_root, y0=e.y_root)

    def move(e):
        if abs(e.x_root - drag["x0"]) + abs(e.y_root - drag["y0"]) > 4:
            drag["moved"] = True
        if drag["moved"]:
            pos["x"], pos["y"] = e.x_root - drag["x"], e.y_root - drag["y"]

    def release(_e):
        if drag["moved"]:
            try:
                DADOS.mkdir(parents=True, exist_ok=True)
                POS_FILE.write_text(json.dumps(pos), encoding="utf-8")
            except OSError:
                pass
        else:
            threading.Thread(target=ear, args=("stop" if st["state"] in ("listen", "dict") else "listen",), daemon=True).start()

    root.bind("<ButtonPress-1>", press)
    root.bind("<B1-Motion>", move)
    root.bind("<ButtonRelease-1>", release)
    root.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

    t0 = time.time()

    def tick():
        st["shown"] += (st["level"] - st["shown"]) * 0.25
        st["level"] *= 0.92
        camada.desenha(frame(time.time() - t0, st["shown"], st["state"]), pos["x"], pos["y"])
        root.attributes("-topmost", True)
        root.after(int(1000 / FPS), tick)
    tick()
    root.mainloop()


if __name__ == "__main__":
    if "--teste" in sys.argv:
        for est in ESTADOS:
            f = frame(1.0, 0.5, est)
            assert f.shape == (SIZE, SIZE, 4) and f[..., 3].max() > 200 and f[0, 0, 3] == 0, est
        print("orb ok")
    else:
        main()
