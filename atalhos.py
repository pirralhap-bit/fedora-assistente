"""Fast path: Ollama picks a shortcut from atalhos.md, code does it. Anything unsure goes to Claude (returns None)."""
import configparser
import difflib
import glob
import json
import os
import pathlib
import re
import subprocess
import time
import unicodedata
import urllib.parse
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
def hora(_, msg=""):
    t = time.localtime()
    return f"São {t.tm_hour} e {t.tm_min:02d}." if t.tm_min else f"São {t.tm_hour} horas em ponto."


def data(_, msg=""):
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


def abrir_pasta(arg, msg=""):
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
        generic = e.get("GenericName[pt_BR]") or e.get("GenericName") or ""
        yield pathlib.Path(f).stem, names, e.get("Name", "") + (f" ({generic})" if generic else "")


_STEAM = HOME / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
def _jogos():
    """(appid, name) of installed Steam games, every library, without Proton/runtimes."""
    libs = re.findall(r'"path"\s+"([^"]+)"', (_STEAM / "steamapps/libraryfolders.vdf").read_text(errors="replace")) \
        if (_STEAM / "steamapps/libraryfolders.vdf").exists() else []
    for lib in libs or [str(_STEAM)]:
        for f in glob.glob(lib + "/steamapps/appmanifest_*.acf"):
            t = pathlib.Path(f).read_text(errors="replace")
            appid, name = re.search(r'"appid"\s+"(\d+)"', t), re.search(r'"name"\s+"([^"]+)"', t)
            if appid and name and not re.search(r"proton|runtime|redistributable", name.group(1), re.I):
                yield appid.group(1), name.group(1)


def volume(arg, msg=""):
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


def sistema(_, msg=""):
    s = sysinfo.snapshot()
    g = s.get("gpu") or {}
    br = lambda x: f"{x:.1f}".replace(".", ",")   # noqa: E731  (Piper reads "7.1" as "sete ponto um")
    out = f"Memória em {br(s['ram'])} de {s['ram_total']:.0f} gigas."
    if g.get("vram") is not None:
        out += f" Placa de vídeo com {br(g['vram'])} de {g['vram_total']:.0f} gigas ocupados, {g.get('busy', 0)} por cento de uso."
    return out


def descarregar_modelo(_, msg=""):
    subprocess.run(["ollama", "stop", modelo.MODEL], capture_output=True, timeout=20)
    return "Modelo descarregado. A placa de vídeo está livre."


def desligar_ollama(_, msg=""):
    subprocess.Popen(["systemctl", "--user", "stop", "ollama"])   # don't wait: this very request came through Ollama
    return "Desligando o Ollama. Ele volta quando você abrir a Fedora de novo."


def _section(title):
    """`- nome: valor` lines under a '## title' heading of atalhos.md."""
    txt = CATALOG.read_text(encoding="utf-8")
    m = re.search(rf"^## {title}.*?$(.*?)(?=^## |\Z)", txt, flags=re.M | re.S)
    return dict((fold(k), v.strip()) for k, v in re.findall(r"^- ([^:\n]+):\s*(\S+)", m.group(1) if m else "", flags=re.M))


def _browser(url):
    _detach(["google-chrome-stable", url])


def inventario():
    """Everything 'abrir' can open, id -> (name, how). Generated each time: install a game and it is there."""
    out = {f"app:{i}": (name, ["gtk-launch", i]) for i, _, name in _apps()}
    out.update({f"jogo:{i}": (name, ["xdg-open", f"steam://rungameid/{i}"]) for i, name in _jogos()})
    out.update({f"site:{k}": (k, ["google-chrome-stable", url]) for k, url in _section("Sites").items()})
    return dict(sorted(out.items()))   # stable order: the prompt prefix stays cacheable


def _achar(nome):
    """Installed item for the official name the model wrote. Generic: same name, then all its words, then the closest."""
    alvo = re.findall(r"\w+", fold(nome))
    if not alvo:
        return None
    itens = [(re.findall(r"\w+", fold(v[0].split(" (")[0])), v) for v in inventario().values()]
    for nomes, v in itens:
        if nomes == alvo:
            return v
    contem = [(len(n), v) for n, v in itens if set(alvo) <= set(n)] or \
        [(len(re.findall(r"\w+", v[0])), v) for n, v in itens if set(alvo) <= set(re.findall(r"\w+", fold(v[0])))]  # "calculadora" -> KCalc (Calculadora científica)
    if contem:
        return min(contem, key=lambda x: x[0])[1]          # "steam" -> Steam, not "Steam Linux Runtime"
    ratio, v = max(((difflib.SequenceMatcher(None, " ".join(alvo), " ".join(n)).ratio(), v) for n, v in itens),
                   key=lambda x: x[0])
    return v if ratio >= 0.7 else None


def abrir(arg, msg=""):
    """The model writes the official name (it knows GTA, Konsole, Trailhead); the code finds it among what is installed."""
    item = _achar(arg)
    if not item:
        return None
    _detach(item[1])
    return f"Abrindo {item[0].split(' (')[0]}."


def pesquisar_web(arg, msg=""):
    q = re.sub(r"^(no google|na internet|na web)\s+|\s+(no google|na internet|na web)$", "", arg.strip(), flags=re.I)
    if not q:
        return None
    _browser("https://www.google.com/search?q=" + urllib.parse.quote_plus(q))
    return f"Pesquisando {q}."


_DISPLAYS = "/org/kde/ScreenBrightness"


def _gdbus(path, method, *args, dest="org.kde.ScreenBrightness"):
    return subprocess.run(["gdbus", "call", "--session", "--dest", dest, "--object-path", path, "--method", method, *args],
                          capture_output=True, text=True, timeout=5).stdout


def brilho(arg, msg=""):
    a = fold(arg)
    n = re.search(r"\d+", a)
    step = 10 if re.search(r"mais|aument|sobe|clar", a) else -10 if re.search(r"menos|abaix|diminu|baix|escur", a) else None
    if not n and step is None:
        return None
    names = re.findall(r"'(display\d+)'", _gdbus(_DISPLAYS, "org.freedesktop.DBus.Properties.Get",
                                                    "org.kde.ScreenBrightness", "DisplaysDBusNames"))
    for d in names:
        path = f"{_DISPLAYS}/{d}"
        mx = int(re.search(r"\d+", _gdbus(path, "org.freedesktop.DBus.Properties.Get", "org.kde.ScreenBrightness.Display", "MaxBrightness")).group())
        if n:
            pct = min(100, int(n.group()))
        else:
            cur = int(re.search(r"\d+", _gdbus(path, "org.freedesktop.DBus.Properties.Get", "org.kde.ScreenBrightness.Display", "Brightness")).group())
            pct = max(0, min(100, round(cur * 100 / mx) + step))
        _gdbus(path, "org.kde.ScreenBrightness.Display.SetBrightness", str(round(pct * mx / 100)), "0")
    return f"Brilho em {pct} por cento." if names else None


def _players():
    out = _gdbus("/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames", dest="org.freedesktop.DBus")
    names = re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", out)
    def playing(n):
        st = _gdbus("/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties.Get", "org.mpris.MediaPlayer2.Player",
                    "PlaybackStatus", dest=n)
        return "Playing" in st
    return sorted(names, key=lambda n: (not playing(n), "plasma-browser-integration" in n))   # the one playing first


def midia(arg, msg=""):
    a = fold(arg + " " + msg)
    action = ("Next" if re.search(r"proxim|pula|avanca|next", a) else "Previous" if re.search(r"anterior|volta", a)
              else "Pause" if re.search(r"paus|para\b|parar", a) else "Play" if re.search(r"toca|play|continu|despaus|retom", a)
              else None)
    players = _players()
    if not action or not players:
        return None
    _gdbus("/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player." + action, dest=players[0])
    return {"Next": "Próxima.", "Previous": "Voltando.", "Pause": "Pausado.", "Play": "Tocando."}[action]


def print_(arg, msg=""):
    a = fold(arg + " " + msg)
    mode = "-a" if "janela" in a else "-r" if re.search(r"area|regiao|pedaco|parte", a) else "-f"
    pasta = pathlib.Path(subprocess.run(["xdg-user-dir", "PICTURES"], capture_output=True, text=True).stdout.strip() or HOME) / "Capturas"
    pasta.mkdir(parents=True, exist_ok=True)
    out = pasta / time.strftime("print-%Y%m%d-%H%M%S.png")
    _detach(["spectacle", "-b", "-n", mode, "-o", str(out)])
    return "Selecione a área." if mode == "-r" else "Print salvo em Imagens, Capturas."


_NUM = {"um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8,
        "nove": 9, "dez": 10, "quinze": 15, "vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50}


def duracao(text):
    """Seconds in "20 minutos", "1 hora e meia", "meia hora", "uma hora e 15 minutos", "90 segundos"; None if absent."""
    t = fold(text)
    total, found = 0, False
    for num, unit in re.findall(r"(\d+|" + "|".join(_NUM) + r"|meia)\s*(hora|h\b|minuto|min\b|segundo|seg\b)", t):
        n = 0.5 if num == "meia" else int(num) if num.isdigit() else _NUM[num]
        total += n * (3600 if unit.startswith("h") else 60 if unit.startswith("min") else 1)
        found = True
    if re.search(r"hora[s]? e meia", t):
        total += 1800
    return int(total) if found and total > 0 else None


def timer(arg, msg=""):
    secs = duracao(arg) or duracao(msg)
    if not secs or secs > 24 * 3600:
        return None
    m = re.search(r"\b(pra|para|de)\s+(?!\d)(.+)$", fold(msg))
    motivo = m.group(2) if m and not duracao(m.group(2)) else ""
    aviso = f"Hora de {motivo}." if motivo else "Seu timer terminou."
    # systemd keeps it even if Fedora restarts; argv only (never a shell): the text came from the microphone
    r = subprocess.run(["systemd-run", "--user", "--collect", f"--on-active={secs}", "--timer-property=AccuracySec=1s",
                        "--unit", f"fedora-timer-{int(time.time())}", "/usr/bin/python3",
                        str(pathlib.Path(__file__).with_name("speech.py")), "--aviso", aviso], capture_output=True, timeout=10)
    if r.returncode:
        return None
    h, mnt = divmod(secs // 60, 60)
    quando = " e ".join(x for x in (f"{h} hora{'s' * (h > 1)}" if h else "", f"{mnt} minuto{'s' * (mnt > 1)}" if mnt else "") if x) or f"{secs} segundos"
    return f"Timer de {quando}" + (f" pra {motivo}." if motivo else ".")


_CERQUILHO = ("Cerquilho", -23.165, -47.743)


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "fedora-assistente"}), timeout=6) as r:
        return json.load(r)


def clima(arg, msg=""):
    cidade = re.sub(r"^(em |no |na |de )|\b(hoje|amanha|agora)\b", "", fold(arg)).strip()
    nome, lat, lon = _CERQUILHO
    if cidade and cidade != "cerquilho":
        g = (_get("https://geocoding-api.open-meteo.com/v1/search?count=1&language=pt&name=" + urllib.parse.quote(cidade))
             .get("results") or [None])[0]
        if not g:
            return None
        nome, lat, lon = g["name"], g["latitude"], g["longitude"]
    d = _get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&timezone=auto&forecast_days=2"
             "&current=temperature_2m&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max")
    i = 1 if "amanha" in fold(arg + " " + msg) else 0
    dd = d["daily"]
    out = (f"{'Amanhã' if i else 'Hoje'} em {nome}: máxima de {round(dd['temperature_2m_max'][i])}, mínima de "
           f"{round(dd['temperature_2m_min'][i])} graus, {dd['precipitation_probability_max'][i]} por cento de chance de chuva.")
    return out + (f" Agora faz {round(d['current']['temperature_2m'])} graus." if not i else "")


HANDLERS = {f.__name__.rstrip("_"): f for f in (hora, data, abrir_pasta, abrir, pesquisar_web, volume, brilho,
                                                 midia, print_, timer, clima, sistema, descarregar_modelo, desligar_ollama)}


# ── routing ──
def classify(message):
    """Ask the local model which shortcut this is. Never waits for a cold load (17 s): no model, no fast path."""
    if modelo.MODEL not in (sysinfo.ollama() or []):
        return None
    prompt = (CATALOG.read_text(encoding="utf-8") +   # same prefix every time: Ollama reuses its cache
              '\nEscolha UM atalho acima para o pedido. Se não for claramente um deles, use "nenhum". Se não for claramente um deles, use "nenhum".\n'
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


# the model picks, the code double-checks: a pick whose verb isn't in the sentence goes to Claude
_ABRIR = r"\b(abr|entr|inici|execut|rod|lig|bot|coloc|mostr)"
GUARD = {"abrir": _ABRIR, "abrir_pasta": _ABRIR,
         "pesquisar_web": r"\b(pesquis|procur|busc|googl)"}
_NUNCA = r"\b(fech|encerr|mat[ae]|sai[rd]|apag|delet|remov|exclu|deslig)"


def _confere(name, message):
    m = fold(message)
    if name in GUARD and (not re.search(GUARD[name], m) or re.search(_NUNCA, m)):
        return False
    return True


def route(message):
    """Spoken reply if a shortcut handled the request, else None (Claude takes it)."""
    picked = classify(message)
    if not picked or picked[0] not in HANDLERS or not _confere(picked[0], message):
        return None
    name, arg = picked
    try:
        reply = HANDLERS[name](arg, message)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, AttributeError, TypeError):
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
    inv = inventario()
    assert "app:google-chrome" in inv and "site:youtube" in inv and any(k.startswith("jogo:") for k in inv)
    assert duracao("20 minutos") == 1200 and duracao("1 hora e meia") == 5400 and duracao("meia hora") == 1800
    assert duracao("uma hora e 15 minutos") == 4500 and duracao("tirar a peça") is None
    assert _section("Sites")["trailhead"].startswith("https://")
    # routing with the real model: every phrase must land on the right shortcut (handlers not run)
    cases = [("que horas são", "hora"), ("me diz as horas", "hora"), ("que dia é hoje?", "data"),
             ("abre meus downloads", "abrir_pasta"), ("abre o chrome pra mim", "abrir:app:google-chrome"), ("aumenta o volume", "volume"),
             ("volume 30", "volume"), ("como está o sistema", "sistema"), ("descarrega o modelo", "descarregar_modelo"),
             ("quanto é 17 vezes 23?", "nenhum"), ("como vai estar o tempo amanhã?", "clima"), ("vai chover hoje?", "clima"),
             ("abre o youtube", "abrir:site:youtube"), ("abre o gta", "abrir:jogo:3240220"), ("bota o cs2", "abrir:jogo:730"),
             ("abre o cities skylines 2", "abrir:jogo:949230"), ("abre o espectáculo", "abrir:app:org.kde.spectacle"),
             ("abre o console", "abrir:app:org.kde.konsole"), ("abre o obsidiana", "abrir:app:md.obsidian.Obsidian"),
             ("abre o trilhear", "abrir:site:trailhead"), ("abre o whatsapp", "abrir:site:whatsapp"),
             ("abre a steam", "abrir:app:com.valvesoftware.Steam"), ("abre a calculadora", "abrir:app:org.kde.kcalc"),
             ("abre o orca slicer", "abrir:app:com.orcaslicer.OrcaSlicer"), ("pesquisa preço do PLA", "pesquisar_web"), ("tira um print", "print"),
             ("brilho 50", "brilho"), ("pausa a música", "midia"), ("pula essa", "midia"), ("timer de 20 minutos", "timer"),
             ("me avisa em 1 hora pra tirar a peça da impressora", "timer"), ("quanto custa o PLA hoje?", "nenhum"),
             ("desliga o computador", "nenhum"),
             ("apaga o arquivo teste.txt", "nenhum"), ("fecha o chrome", "nenhum"), ("cria um arquivo lista.txt", "nenhum")]
    if modelo.MODEL in (sysinfo.ollama() or []):
        def pick(m):
            c = classify(m)
            if not (c and c[0] in HANDLERS and _confere(c[0], m)):
                return "nenhum"
            if c[0] != "abrir":
                return c[0]
            item = _achar(c[1])
            return "abrir:" + next((k for k, v in inventario().items() if v is item or v == item), "?") if item else "nenhum"
        bad = [(m, want, classify(m)) for m, want in cases if pick(m) != want]
        assert not bad, bad
        print(f"roteamento ok ({len(cases)} frases)")
    else:
        print("modelo não carregado: roteamento não testado")
    print("atalhos ok")
