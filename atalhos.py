"""Fast path: Ollama picks a shortcut from atalhos.md, code does it. Anything unsure goes to Claude (returns None)."""
import configparser
import glob
import json
import os
import pathlib
import re
import subprocess
import time
import unicodedata
import urllib.request

import modelo
import sysinfo

CATALOG = pathlib.Path(__file__).with_name("atalhos.md")
HOME = pathlib.Path.home()
_DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
_MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
          "outubro", "novembro", "dezembro"]


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFD", str(s or "").casefold()) if not unicodedata.combining(c)).strip()


def _detach(cmd):
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


# ── handlers: return the spoken reply, or None to hand the request to Claude ──
def hora(_):
    t = time.localtime()
    return f"São {t.tm_hour} e {t.tm_min:02d}." if t.tm_min else f"São {t.tm_hour} horas em ponto."


def data(_):
    t = time.localtime()
    return f"Hoje é {_DIAS[t.tm_wday]}, {t.tm_mday} de {_MESES[t.tm_mon - 1]} de {t.tm_year}."


_XDG = {"download": "DOWNLOAD", "documento": "DOCUMENTS", "imagem": "PICTURES", "imagens": "PICTURES", "foto": "PICTURES",
        "musica": "MUSIC", "video": "VIDEOS", "area de trabalho": "DESKTOP", "desktop": "DESKTOP"}


def _pasta(arg):
    a = re.sub(r"^(a |o |as |os |meus? |minhas? |pasta (de |do |da )?)+", "", fold(arg))
    if a in ("", "pessoal", "home", "casa", "inicio"):
        return HOME
    for key, xdg in _XDG.items():
        if a.startswith(key):
            out = subprocess.run(["xdg-user-dir", xdg], capture_output=True, text=True).stdout.strip()
            return pathlib.Path(out) if out else None
    for p in [HOME / arg] + sorted(HOME.iterdir()):   # a folder of the home dir, by name without accents/case
        if p.is_dir() and fold(p.name) in (a, fold(arg)):
            return p
    return None


def abrir_pasta(arg):
    p = _pasta(arg)
    if not p or not p.is_dir():
        return None
    _detach(["xdg-open", str(p)])
    return f"Pronto, abri {'a pasta pessoal' if p == HOME else 'a pasta ' + p.name}."


def _apps():
    dirs = ["/usr/share/applications", str(HOME / ".local/share/applications"),
            "/var/lib/flatpak/exports/share/applications", str(HOME / ".local/share/flatpak/exports/share/applications")]
    for f in (f for d in dirs for f in glob.glob(d + "/*.desktop")):
        c = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            c.read(f, encoding="utf-8")
            e = c["Desktop Entry"]
        except (configparser.Error, KeyError, UnicodeDecodeError):
            continue
        if e.get("NoDisplay", "").lower() == "true" or e.get("Type") != "Application":
            continue
        names = {fold(v) for k, v in e.items() if k.startswith("name") or k.startswith("genericname")}
        yield pathlib.Path(f).stem, names


def abrir_app(arg):
    a = re.sub(r"^(o |a |um |uma |app |aplicativo |programa )+", "", fold(arg))
    if not a:
        return None
    exact = [i for i, names in _apps() if a in names]
    loose = exact or [i for i, names in _apps() if any(n.startswith(a) or a in n.split() for n in names)]
    if not loose:
        return None
    _detach(["gtk-launch", loose[0]])
    return f"Abrindo {arg.strip()}."


def volume(arg):
    a = fold(arg)
    sink = "@DEFAULT_AUDIO_SINK@"
    if re.search(r"\bmud|mute|silenc", a):
        subprocess.run(["wpctl", "set-mute", sink, "toggle"])
        return "Pronto."
    n = re.search(r"\d+", a)
    if n:
        v = min(100, int(n.group()))
        subprocess.run(["wpctl", "set-volume", sink, f"{v}%"])
        subprocess.run(["wpctl", "set-mute", sink, "0"])
        return f"Volume em {v} por cento."
    step = "10%+" if re.search(r"mais|aument|sobe|alto", a) else "10%-" if re.search(r"menos|abaix|diminu|baix", a) else None
    if not step:
        return None
    subprocess.run(["wpctl", "set-volume", "-l", "1.0", sink, step])
    return "Pronto."


def sistema(_):
    s = sysinfo.snapshot()
    g = s.get("gpu") or {}
    br = lambda x: f"{x:.1f}".replace(".", ",")   # noqa: E731  (Piper reads "7.1" as "sete ponto um")
    out = f"Memória em {br(s['ram'])} de {s['ram_total']:.0f} gigas."
    if g.get("vram") is not None:
        out += f" Placa de vídeo com {br(g['vram'])} de {g['vram_total']:.0f} gigas ocupados, {g.get('busy', 0)} por cento de uso."
    return out


def descarregar_modelo(_):
    subprocess.run(["ollama", "stop", modelo.MODEL], capture_output=True, timeout=20)
    return "Modelo descarregado. A placa de vídeo está livre."


def desligar_ollama(_):
    subprocess.Popen(["systemctl", "--user", "stop", "ollama"])   # don't wait: this very request came through Ollama
    return "Desligando o Ollama. Ele volta quando você abrir a Fedora de novo."


HANDLERS = {f.__name__: f for f in (hora, data, abrir_pasta, abrir_app, volume, sistema, descarregar_modelo, desligar_ollama)}


# ── routing ──
def classify(message):
    """Ask the local model which shortcut this is. Never waits for a cold load (17 s): no model, no fast path."""
    if modelo.MODEL not in (sysinfo.ollama() or []):
        return None
    prompt = (CATALOG.read_text(encoding="utf-8") +   # same prefix every time: Ollama reuses its cache
              '\nEscolha UM atalho acima para o pedido. Se não for claramente um deles, use "nenhum".\n'
              'Responda só JSON: {"atalho": "<nome exato da lista>", "arg": "<texto ou vazio>"}\n'
              f"Pedido: {message}")
    body = json.dumps({"model": modelo.MODEL, "prompt": prompt, "stream": False, "format": "json", "think": False,
                       "keep_alive": modelo.keep_alive(), "options": {"temperature": 0}}).encode()
    req = urllib.request.Request(sysinfo.OLLAMA_URL + "/api/generate", body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            out = json.loads(json.load(r).get("response") or "{}")
    except (OSError, ValueError):
        return None
    name = re.sub(r"\W.*", "", str(out.get("atalho") or "").strip())     # "abrir_app(chrome)" -> "abrir_app"
    arg = out.get("arg")
    return name, (arg if isinstance(arg, str) else "")


def route(message):
    """Spoken reply if a shortcut handled the request, else None (Claude takes it)."""
    picked = classify(message)
    if not picked or picked[0] not in HANDLERS:
        return None
    name, arg = picked
    try:
        reply = HANDLERS[name](arg)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None
    if reply:
        print(f"[atalho] {message!r} -> {name}({arg!r})", flush=True)
    return reply


if __name__ == "__main__":
    # handlers (no Ollama needed)
    assert hora("").startswith("São ") and data("").startswith("Hoje é ")
    assert _pasta("meus downloads") and _pasta("meus downloads").is_dir()
    assert _pasta("pasta projetos") == HOME / "Projetos", _pasta("pasta projetos")
    assert _pasta("xyz inexistente") is None
    assert any("chrome" in n for i, names in _apps() for n in names)
    # routing with the real model: every phrase must land on the right shortcut (handlers not run)
    cases = [("que horas são", "hora"), ("me diz as horas", "hora"), ("que dia é hoje?", "data"),
             ("abre meus downloads", "abrir_pasta"), ("abre o chrome pra mim", "abrir_app"), ("aumenta o volume", "volume"),
             ("volume 30", "volume"), ("como está o sistema", "sistema"), ("descarrega o modelo", "descarregar_modelo"),
             ("quanto é 17 vezes 23?", "nenhum"), ("como vai estar o tempo amanhã?", "nenhum"),
             ("apaga o arquivo teste.txt", "nenhum"), ("fecha o chrome", "nenhum"), ("cria um arquivo lista.txt", "nenhum")]
    if modelo.MODEL in (sysinfo.ollama() or []):
        bad = [(m, want, classify(m)) for m, want in cases if (classify(m) or ("?",))[0] != want]
        assert not bad, bad
        print(f"roteamento ok ({len(cases)} frases)")
    else:
        print("modelo não carregado: roteamento não testado")
    print("atalhos ok")
