"""Fast path: Gemini (flash-lite) picks a shortcut from atalhos.md, code does it. Anything unsure goes to Claude (returns None)."""
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

import llm
import sysinfo

CATALOG = pathlib.Path(__file__).with_name("atalhos.md")
HOME = pathlib.Path.home()
WIN = os.name == "nt"
_DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
_MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
          "outubro", "novembro", "dezembro"]


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFD", str(s or "").casefold()) if not unicodedata.combining(c)).strip()


def _detach(cmd):
    if WIN:
        if cmd and cmd[0] == "startfile":      # ["startfile", path_or_url]: the Windows "open" verb
            os.startfile(cmd[1])
        else:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        return
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
    if re.search(r"\.\.|[\\/:]", arg):   # only known names or a plain folder of the home dir: HOME / "C:\x" is C:\x
        return None
    a = re.sub(r"^(a |o |as |os |meus? |minhas? |pasta (de |do |da )?)+", "", fold(arg))
    if a in ("", "pessoal", "home", "casa", "inicio"):
        return HOME
    for key, xdg in _XDG.items():
        if a.startswith(key):
            if WIN:
                alvo = {"DOWNLOAD": "Downloads", "DOCUMENTS": "Documents", "PICTURES": "Pictures", "MUSIC": "Music",
                        "VIDEOS": "Videos", "DESKTOP": "Desktop"}[xdg]
                return HOME / alvo
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
    _detach(["startfile", str(p)] if WIN else ["xdg-open", str(p)])
    return f"Pronto, abri {'a pasta pessoal' if p == HOME else 'a pasta ' + p.name}."


def _apps_win():
    """Start Menu shortcuts (.lnk): id = the shortcut path, opened with the shell."""
    raiz = [os.path.join(os.environ.get("ProgramData", ""), "Microsoft/Windows/Start Menu/Programs"),
            os.path.join(os.environ.get("APPDATA", ""), "Microsoft/Windows/Start Menu/Programs")]
    vistos = set()
    for d in raiz:
        for f in glob.glob(d + "/**/*.lnk", recursive=True):
            nome = pathlib.Path(f).stem
            if fold(nome) in vistos or re.search(r"uninstall|desinstal|readme|license", nome, re.I):
                continue
            vistos.add(fold(nome))
            yield f, {fold(nome)}, nome


def _apps():
    if WIN:
        yield from _apps_win()
        return
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


_STEAM = pathlib.Path(os.environ.get("STEAM_DIR") or (r"C:\Program Files (x86)\Steam" if WIN else HOME / ".var/app/com.valvesoftware.Steam/.local/share/Steam"))
def _jogos():
    """(appid, name) of installed Steam games, every library, without Proton/runtimes."""
    libs = re.findall(r'"path"\s+"([^"]+)"', (_STEAM / "steamapps/libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")) \
        if (_STEAM / "steamapps/libraryfolders.vdf").exists() else []
    for lib in libs or [str(_STEAM)]:
        lib = lib.replace("\\\\", "/")
        for f in glob.glob(lib + "/steamapps/appmanifest_*.acf"):
            try:
                t = pathlib.Path(f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            appid, name = re.search(r'"appid"\s+"(\d+)"', t), re.search(r'"name"\s+"([^"]+)"', t)
            if appid and name and not re.search(r"proton|runtime|redistributable", name.group(1), re.I):
                yield appid.group(1), name.group(1)


def _tecla(vk, vezes=1):
    import ctypes
    for _ in range(vezes):
        ctypes.windll.user32.keybd_event(vk, 0, 1, 0)       # KEYEVENTF_EXTENDEDKEY
        ctypes.windll.user32.keybd_event(vk, 0, 3, 0)       # ... | KEYEVENTF_KEYUP


def _volume_win(a):
    VK_MUTE, VK_DOWN, VK_UP = 0xAD, 0xAE, 0xAF               # each press moves 2 %
    if re.search(r"mud|mute|silenc", a):
        _tecla(VK_MUTE)
        return "Pronto."
    n = re.search(r"\d+", a)
    if n:
        v = min(100, int(n.group()))
        _tecla(VK_DOWN, 50)
        _tecla(VK_UP, round(v / 2))
        return f"Volume em {v} por cento."
    if re.search(r"mais|aument|sobe|alto", a):
        _tecla(VK_UP, 5)
    elif re.search(r"menos|abaix|diminu|baix", a):
        _tecla(VK_DOWN, 5)
    else:
        return None
    return "Pronto."


def volume(arg, msg=""):
    a = fold(arg)
    if WIN:
        return _volume_win(a)
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


def _section(title):
    """`- nome: valor` lines under a '## title' heading of atalhos.md."""
    txt = CATALOG.read_text(encoding="utf-8")
    m = re.search(rf"^## {title}.*?$(.*?)(?=^## |\Z)", txt, flags=re.M | re.S)
    return dict((fold(k), v.strip()) for k, v in re.findall(r"^- ([^:\n]+):\s*(\S+)", m.group(1) if m else "", flags=re.M))


def _browser(url):
    if WIN:
        import webbrowser
        webbrowser.open(url)
        return
    _detach(["google-chrome-stable", url])


def inventario():
    """Everything 'abrir' can open, id -> (name, how). Generated each time: install a game and it is there."""
    out = {f"app:{i}": (name, ["startfile", i] if WIN else ["gtk-launch", i]) for i, _, name in _apps()}
    out.update({f"jogo:{i}": (name, ["startfile", f"steam://rungameid/{i}"] if WIN else ["xdg-open", f"steam://rungameid/{i}"])
                for i, name in _jogos()})
    out.update({f"site:{k}": (k, ["startfile", url] if WIN else ["google-chrome-stable", url]) for k, url in _section("Sites").items()})
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
    if WIN:
        return None          # no portable API for desktop monitors: Claude handles it
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
    if WIN:
        VK = {"Next": 0xB0, "Previous": 0xB1, "PlayPause": 0xB3}
        if re.search(r"proxim|pula|avanca|next", a):
            k, resp = "Next", "Próxima."
        elif re.search(r"anterior|volta", a):
            k, resp = "Previous", "Voltando."
        elif re.search(r"paus|para|parar|toca|play|continu|despaus|retom", a):
            k, resp = "PlayPause", "Pronto."
        else:
            return None
        _tecla(VK[k])
        return resp
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
    if WIN:
        if re.search(r"area|regiao|pedaco|parte|janela", a):
            import ctypes
            for vk in (0x5B, 0x10, 0x53):
                ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            for vk in (0x53, 0x10, 0x5B):
                ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
            return "Selecione a área."
        from PIL import ImageGrab
        pasta = HOME / "Pictures" / "Capturas"
        pasta.mkdir(parents=True, exist_ok=True)
        ImageGrab.grab(all_screens=True).save(pasta / time.strftime("print-%Y%m%d-%H%M%S.png"))
        return "Print salvo em Imagens, Capturas."
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
    # argv only (never a shell): the text came from the microphone
    if WIN:
        import sys
        subprocess.Popen([sys.executable, str(pathlib.Path(__file__).with_name("speech.py")), "--aviso-em", str(secs), aviso],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        r = subprocess.CompletedProcess([], 0)
    else:
      r = subprocess.run(["systemd-run", "--user", "--collect", f"--on-active={secs}", "--timer-property=AccuracySec=1s",
                        "--unit", f"fedora-timer-{int(time.time())}", "/usr/bin/python3",
                        str(pathlib.Path(__file__).with_name("speech.py")), "--aviso", aviso], capture_output=True, timeout=10)
    if r.returncode:
        return None
    h, mnt = divmod(secs // 60, 60)
    quando = " e ".join(x for x in (f"{h} hora{'s' * (h > 1)}" if h else "", f"{mnt} minuto{'s' * (mnt > 1)}" if mnt else "") if x) or f"{secs} segundos"
    return f"Timer de {quando}" + (f" pra {motivo}." if motivo else ".")


# cidade padrão do clima: FEDORA_CIDADE="Nome,lat,lon" no .env
_n, _lat, _lon = os.environ.get("FEDORA_CIDADE", "São Paulo,-23.550,-46.633").split(",")
_CASA = (_n.strip(), float(_lat), float(_lon))


def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "fedora-assistente"}), timeout=6) as r:
        return json.load(r)


def clima(arg, msg=""):
    cidade = re.sub(r"^(em |no |na |de )|\b(hoje|amanha|agora)\b", "", fold(arg)).strip()
    nome, lat, lon = _CASA
    if cidade and cidade != fold(_CASA[0]):
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
                                                 midia, print_, timer, clima, sistema)}


# ── filtro local: decide, sem rede, se o pedido pode sair do PC rumo ao Gemini ──
# Duas portas, AMBAS necessárias, sobre o pedido normalizado (fold, sem pontuação, sem "aí"/"por favor"/"me" no começo):
#   (a) FORMA de comando: a frase COMEÇA com um verbo/expressão de algum atalho ("abre", "sobe", "me avisa", "daqui a")
#       ou é uma pergunta curta de atalho ("que horas", "vai chover", "como tá o tempo", "quanto de memória");
#   (b) OBJETO compatível com esse atalho (volume/som, brilho/tela, música, print, uma duração, clima...).
# Cada REGRA abaixo junta as duas: (atalho, começo, objeto ou None, máximo de palavras). "abrir" com um nome que o
# catálogo não conhece também passa (o Gemini sabe que "orca" é OrcaSlicer), mas só se a frase for curta e sem cara
# de conversa (_ABRIR_LIVRE). Pesquisa só passa com "no google/na internet" ou curta e sem nada pessoal (_PESQUISA_LIVRE).
# Erro para o lado seguro: barrar um atalho custa um turno do Claude; deixar passar conversa manda a frase à rede.
# `python atalhos.py --teste-filtro` mede os dois lados com as listas do QA (metas: >= 90 % e <= 20 %).
MAX_PALAVRAS = 12
_FILLER_INICIO = re.compile(r"^(?:ai|entao|ok|okay|beleza|fedora|ei|oi|olha|por favor|por gentileza|faz favor|"
                            r"faca o favor de|da uma|voce pode|pode|consegue|quero que voce|so|me)\s+")
_FILLER_FIM = re.compile(r"\s+(?:pra mim|por favor|faz favor|agora|ai)$")
_DURACAO = (r"\b(?:\d+|um|uma|dois|duas|tres|quatro|cinco|seis|sete|oito|nove|dez|quinze|vinte|trinta|quarenta|"
            r"cinquenta|meia)\s*(?:horas?|h|minutos?|min|segundos?|seg)\b|\bmeia hora\b")
_PASTAS = (r"\bpasta\b|\bdownloads?\b|\bdocumentos\b|\bimagens\b|\bfotos\b|\bmusicas\b|\bvideos\b|\barea de trabalho\b"
           r"|\bdesktop\b|\bprojetos\b")
_V_ABRIR = r"(?:abre|abra|abrir|inicia|inicie|iniciar|bota|bote|poe|ponha|liga|ligue|chama|chame|executa|execute|roda|rode|entra|entre|coloca|coloque|mostra|mostre)"
REGRAS = [
    ("hora", r"(?:que horas?|qual a hora|diz (?:as|a) horas?|fala (?:as|a) horas?|horas)\b", None, 6),
    ("data", r"(?:que dia|que data|qual (?:e )?a data|hoje e dia|dia de hoje|em que dia)\b", None, 8),
    ("abrir_pasta", _V_ABRIR + r"\b", _PASTAS, 8),
    ("abrir", _V_ABRIR + r"\b", None, 0),                       # objeto: catálogo de sites (_sites) ou _ABRIR_LIVRE
    ("pesquisar_web", r"(?:pesquis\w*|procur\w*|busc\w*|googl\w*|joga|poe|bota)\b", r"\b(?:no google|na internet|na web)\b", 12),
    ("volume", r"(?:sobe|suba|aumenta|aumente|abaixa|abaixe|diminui|diminua|baixa)\b", r"\b(?:volume|som|audio|isso|\d+)\b", 8),
    ("volume", r"(?:coloca|poe|bota|deixa) o (?:volume|som)\b", None, 8),
    ("volume", r"(?:muta|mute|desmuta|silencia|silencie|volume|som|tira o som|(?:mais|menos) (?:alto|baixo)"
               r"|(?:ta|esta) (?:muito )?(?:alto|baixo)|esse som|o som)\b", None, 6),
    ("brilho", r"(?:aumenta|aumente|diminui|diminua|abaixa|abaixe|sobe|baixa|deixa|poe|coloca|brilho)\b",
               r"\bbrilho\b|\btela (?:mais )?(?:clara|escura)\b", 8),
    ("midia", r"(?:pausa|pause|pausar|despausa|pula|pule|proxima|avanca|retoma|play|anterior)\b", None, 5),
    ("midia", r"(?:toca|toque|poe|bota|coloca|volta|continua|para)\b", r"\b(?:musica|tocar|tocando|faixa|spotify|proxima|anterior)\b", 8),
    ("print", r"(?:printa|printe|print|captura|screenshot)\b", None, 8),
    ("print", r"(?:tira|tire|manda|faz|faca)\b", r"\b(?:print|captura|screenshot|foto da tela)\b", 8),
    ("timer", r"(?:avisa|lembra|acorda|desperta|timer|cronometro|lembrete|daqui|em|(?:poe|coloca|bota) um (?:timer|cronometro))\b",
              _DURACAO, 12),
    ("clima", r"(?:(?:hoje|amanha|agora) )?(?:vai (?:chover|esfriar|esquentar|fazer (?:frio|calor|sol)|dar sol|ter sol)|chove"
              r"|esfria|esquenta|faz sol|(?:ta|esta) (?:chovendo|quente|frio|calor|garoando)"
              r"|como (?:ta|esta|vai (?:estar|ficar)|fica) o (?:tempo|clima)|(?:o )?tempo (?:amanha|hoje|agora|em|la fora)"
              r"|olhada no tempo|previsao|qual (?:e )?a (?:temperatura|previsao)|quantos graus|temperatura|clima (?:em|de|hoje|amanha))\b",
              None, 10),
    ("sistema", r"(?:como (?:ta|esta) o (?:sistema|pc|computador)|status do sistema|mostra o sistema)$", None, 5),
    ("sistema", r"(?:quanto de (?:memoria|ram|cpu|gpu|placa de video|processador)|uso de (?:memoria|ram|cpu|gpu))\b", None, 8),
]
_REGRAS = [(n, re.compile(c), re.compile(o) if o else None, w) for n, c, o, w in REGRAS]
# um nome que o catálogo não conhece só passa em frase curta e sem cara de conversa/dado pessoal
_ABRIR_LIVRE = {"abre": 6, "abra": 6, "abrir": 6, "inicia": 6, "inicie": 6, "iniciar": 6, "bota": 6, "bote": 6,
                "liga": 4, "ligue": 4, "chama": 4, "chame": 4, "executa": 4, "execute": 4, "roda": 4, "rode": 4,
                "entra": 4, "entre": 4}
_CONVERSA = set("que sobre quando porque se como um uma uns umas pro pra para com contato meu minha meus minhas seu sua "
                "nota notas vault arquivo arquivos email chamado".split())
_PESQUISA_LIVRE = 7          # "pesquisa preço do PLA": sem "no google", só curta e sem nada pessoal (_CONVERSA)
_SITES = []


def _sites():
    if not _SITES:
        _SITES.extend(_section("Sites") or ["-"])
    return _SITES


def normaliza_pedido(msg):
    """fold + sem pontuação + sem as muletas do começo/fim ("aí", "por favor", "me", "pra mim")."""
    m = " ".join(re.sub(r"[^\w\s]", " ", fold(msg)).split())
    m = re.sub(r"\bai\b", " ", m).split()
    m = " ".join(m)
    while True:
        n = _FILLER_FIM.sub("", _FILLER_INICIO.sub("", m))
        if n == m:
            return m
        m = n


def pode_ir_ao_gemini(msg):
    """Pura e offline: True se o pedido pode ir ao Gemini escolher um atalho; False = direto ao Claude, sem rede."""
    m = normaliza_pedido(msg)
    ws = m.split()
    if not 0 < len(ws) <= MAX_PALAVRAS:
        return False
    for nome, comeco, objeto, maxw in _REGRAS:
        c = comeco.match(m)
        if not c:
            continue
        resto = m[c.end():]
        if nome == "abrir":
            if any(re.search(rf"\b{re.escape(s)}\b", resto) for s in _sites()) and len(ws) <= 8:
                return True
            if len(ws) <= _ABRIR_LIVRE.get(ws[0], 0) and len(ws) >= 2 and not _CONVERSA & set(ws):
                return True
            continue
        if len(ws) <= maxw and (objeto is None or objeto.search(m)):
            return True
    return bool(re.match(r"(?:pesquis|procur|busc)\w*\b", m)) and len(ws) <= _PESQUISA_LIVRE and not _CONVERSA & set(ws)


# ── routing ──
def classify(message, info=None):
    """Ask Gemini which shortcut this is. Slow or offline: no fast path, Claude takes it.
    7.5 s in total, and after a failure the circuit breaker (llm.json_call disjuntor=True) skips Gemini for 5 minutes.
    info (optional dict) gets gemini = ok|disjuntor|fora, ms_gemini and disjuntor_abriu (this call switched it off)."""
    info = info if isinstance(info, dict) else {}
    if not llm.ready():
        info["gemini"] = "fora"
        return None
    if llm.disjuntor_aberto():
        info["gemini"] = "disjuntor"
        return None
    t0 = time.monotonic()
    try:
        prompt = (CATALOG.read_text(encoding="utf-8") +
                  '\nEscolha UM atalho acima para o pedido. Se não for claramente um deles, use "nenhum".\n'
                  'Responda só JSON: {"atalho": "<nome exato da lista>", "arg": "<texto ou vazio>"}\n'
                  f"Pedido: {message}")
        out = llm.json_call(prompt, llm.MODEL_RAPIDO, timeout=7.5, temperature=0, disjuntor=True)
    except Exception:  # noqa: BLE001 — anything wrong here only costs the fast path
        info.update(gemini="fora", ms_gemini=int((time.monotonic() - t0) * 1000))
        if llm.disjuntor_aberto():
            info["disjuntor_abriu"] = True
        return None
    info.update(gemini="ok", ms_gemini=int((time.monotonic() - t0) * 1000))
    if not isinstance(out, dict):
        return None
    name = re.sub(r"\W.*", "", str(out.get("atalho") or "").strip())     # "abrir_app(chrome)" -> "abrir_app"
    arg = out.get("arg")
    return name, (arg if isinstance(arg, str) else "")


# the model picks, the code double-checks: a pick whose verb isn't in the sentence goes to Claude
_ABRIR = r"\b(abr|entr|inici|execut|rod|lig|bot|coloc|mostr|cham)"   # "chama o chrome" (QA, lote E)
GUARD = {"abrir": _ABRIR, "abrir_pasta": _ABRIR,
         "pesquisar_web": r"\b(pesquis|procur|busc|googl)"}
_NUNCA = r"\b(fech|encerr|mat[ae]|sai[rd]|apag|delet|remov|exclu|deslig)"


def _confere(name, message):
    m = fold(message)
    if name in GUARD and (not re.search(GUARD[name], m) or re.search(_NUNCA, m)):
        return False
    return True


def route(message, info=None):
    """Spoken reply if a shortcut handled the request, else None (Claude takes it).
    info (optional dict, for turnolog): filtrado (the local filter kept it off the network), gemini
    (ok|disjuntor|fora|nao_usado), ms_gemini, disjuntor_abriu, atalho (name, only when it answered)."""
    info = info if isinstance(info, dict) else {}
    if not pode_ir_ao_gemini(message):
        info.update(filtrado=True, gemini="nao_usado")
        return None
    info["filtrado"] = False
    picked = classify(message, info)
    if not picked or picked[0] not in HANDLERS or not _confere(picked[0], message):
        return None
    name, arg = picked
    try:
        reply = HANDLERS[name](arg, message)
    except Exception as e:  # noqa: BLE001 — ImportError (PIL), JSON from a web API...: Claude takes it
        print(f"[atalho] {name}({arg!r}) falhou: {type(e).__name__}: {str(e)[:120]}", flush=True)
        return None
    if reply:
        info["atalho"] = name
        print(f"[atalho] {message!r} -> {name}({arg!r})", flush=True)
    return reply


# conversa comum que o filtro local deve segurar no PC (não é atalho; nada disso vai ao Gemini)
CONVERSA_COMUM = ["me explica o extrato do meu banco", "escreve um email pro cliente", "o que você acha da PETR4 hoje",
                  "resume a nota de ontem", "qual meu saldo", "cria um arquivo lista.txt",
                  "escreve um e-mail pro cliente da Isabella", "qual o saldo do meu pix", "resume a nota de ontem do vault",
                  "quanto tempo leva pra imprimir um vaso", "faz um vídeo curto sobre o sol", "qual o uso de PLA por peça",
                  "bom dia Fedora", "volta pro que a gente falava", "continua a explicação", "muda o tom do texto",
                  "você lembra do que falamos?", "o que tem na tela do relatório",
                  "analisa a fundo o mercado de hoje", "quanto custa o PLA hoje?"]


def _exemplos_md():
    """The quoted examples after "Ex.:" in the shortcut lines of atalhos.md (the arg descriptions are not examples)."""
    out = []
    for nome, linha in re.findall(r"^- `(\w+)`[^\n]*?Ex\.:([^\n]*)", CATALOG.read_text(encoding="utf-8"), flags=re.M):
        out += [(e, nome) for e in re.findall(r'"([^"]+)"', linha)]
    return out


# listas do QA (lote E). Frases naturais de atalho; as 3 de QA_FORA_DO_CATALOGO não são atalho de verdade (disco livre,
# hora em outro fuso, alarme num horário: nenhum handler faz) e ficam fora das métricas.
QA_ATALHOS = ["abre aí o discord", "dá uma olhada no tempo amanhã", "sobe o som", "põe pra tocar", "quanto de memória tá usando",
              "tira print da tela inteira", "me lembra de ligar pro João daqui a 20 minutos", "pausa", "próxima", "silencia",
              "que dia é hoje", "amanhã chove?", "abre o steam", "bota o cs", "lembrete em meia hora",
              "aumenta o volume", "diminui o brilho", "que horas são", "pula essa música", "volta a música", "tá muito alto",
              "deixa a tela mais clara", "tira uma foto da tela", "daqui a 5 min me avisa", "me acorda em dez min",
              "tá quente lá fora", "retoma a música", "para de tocar", "me avisa em meia hora", "põe um timer de 10 minutos",
              "liga o spotify", "abaixa isso", "manda um print", "esse som tá baixo", "como tá o PC", "quanto de GPU tô usando",
              "vai esfriar amanhã?", "joga no google receita de bolo", "tá chovendo?", "vai dar sol amanhã",
              "mostra a pasta downloads", "abre a pasta de documentos", "continua a música", "avança pra próxima", "mais alto",
              "chama o chrome", "lembra de beber água em uma hora", "coloca uma música"]
QA_FORA_DO_CATALOGO = ["quanto de disco livre eu tenho", "que horas são em Tóquio", "coloca um alarme pras 7"]
QA_DEVEM_PASSAR = ["dá uma olhada no tempo amanhã", "daqui a 5 min me avisa", "me acorda em dez min", "vai esfriar amanhã?",
                   "vai dar sol amanhã", "chama o chrome", "deixa a tela mais clara", "tira uma foto da tela"]
QA_DEVEM_BARRAR = ["abre um chamado sobre o erro do cliente Itaú", "o João me ligou hoje às 10 horas sobre o empréstimo",
                   "mostra meu saldo do nubank", "busca no vault minhas anotações de saúde",
                   "rodei o exame e a taxa de colesterol hoje deu 240", "me explica como funciona a memória RAM",
                   "pesquisa o que significa essa cláusula do meu contrato", "entra em contato com a Maria sobre o processo",
                   "como tá o sistema do meu plano de saúde", "coloca no email que o som do vídeo estava ruim"]
QA_CONVERSA = ["que horas você acha que eu devo acordar amanhã pra treinar", "minha senha do banco é 1234 salva aí",
               "o que eu faço quando a hora do almoço acaba", "me explica como funciona a memória RAM",
               "abre um chamado sobre o erro do cliente Itaú", "bota fé que o projeto sai",
               "escreve o que eu tenho que fazer hoje no dia da semana", "pesquisa o que significa essa cláusula do meu contrato",
               "o João me ligou hoje às 10 horas sobre o empréstimo", "coloca no email que o som do vídeo estava ruim",
               "busca no vault minhas anotações de saúde", "mostra meu saldo do nubank", "lembra de pagar o boleto em 2 dias",
               "qual o clima da reunião com meu chefe", "entra em contato com a Maria sobre o processo",
               "como tá o sistema do meu plano de saúde", "resumo da conversa do meu médico hoje",
               "rodei o exame e a taxa de colesterol hoje deu 240", "execute a sentença do processo",
               "o play do jogo trava o computador"]
META_PASSAM, META_VAZA = 0.90, 0.20


def _teste_filtro(cases):
    """Offline (no network). Measures both sides; the targets are asserted, the numbers are printed either way."""
    import sys
    sys.stdout.reconfigure(errors="replace")
    legit = [m for m, w in cases + _exemplos_md() if w != "nenhum"] + QA_ATALHOS
    barradas = [m for m in legit if not pode_ir_ao_gemini(m)]
    conversa = QA_CONVERSA + [c for c in CONVERSA_COMUM if c not in QA_CONVERSA]
    vazam = [m for m in conversa if pode_ir_ao_gemini(m)]
    passam = 1 - len(barradas) / len(legit)
    print(f"filtro: legítimas que passam {len(legit) - len(barradas)}/{len(legit)} ({passam:.0%}); "
          f"conversa que vaza {len(vazam)}/{len(conversa)} ({len(vazam) / len(conversa):.0%}); "
          f"só a do QA: {sum(pode_ir_ao_gemini(m) for m in QA_CONVERSA)}/{len(QA_CONVERSA)}")
    print(f"  legítimas barradas: {barradas}")
    print(f"  conversa que vaza: {vazam}")
    print(f"  fora do catálogo (sem métrica): {[(m, pode_ir_ao_gemini(m)) for m in QA_FORA_DO_CATALOGO]}")
    assert all(pode_ir_ao_gemini(m) for m in QA_DEVEM_PASSAR), [m for m in QA_DEVEM_PASSAR if not pode_ir_ao_gemini(m)]
    assert not any(pode_ir_ao_gemini(m) for m in QA_DEVEM_BARRAR), [m for m in QA_DEVEM_BARRAR if pode_ir_ao_gemini(m)]
    assert passam >= META_PASSAM and len(vazam) / len(conversa) <= META_VAZA, (passam, vazam)
    assert not pode_ir_ao_gemini("") and not pode_ir_ao_gemini("abre " + "o chrome " * 6)   # > MAX_PALAVRAS
    # frases que não estavam nas listas usadas para desenhar as regras (checagem de generalização, lote E)
    novas = {"abre o notepad": True, "aumenta o som pra 50": True, "qual a previsão pra amanhã": True, "pausa o vídeo": True,
             "toca a próxima": True, "coloca o volume em 30": True, "entra no youtube": True, "abre o gmail": True,
             "me avisa daqui a 2 horas pra tomar remédio": True, "liga pro meu irmão": False, "abre o meu banco": False,
             "abre a conversa com a Ana": False, "me fala uma piada": False, "minha nota fiscal do mês": False}
    assert all(pode_ir_ao_gemini(m) == v for m, v in novas.items()), [m for m, v in novas.items() if pode_ir_ao_gemini(m) != v]
    info = {}
    assert route("me explica o extrato do meu banco", info) is None and info == {"filtrado": True, "gemini": "nao_usado"}
    print("filtro ok (offline)")


if __name__ == "__main__":
    import sys
    # routing with the real model: every phrase must land on the right shortcut (handlers not run)
    def _k(nome):   # inventory id of an installed app by its menu name (Windows ids are .lnk paths)
        return next(k for k, v in inventario().items() if v[0] == nome)
    comuns = [("que horas são", "hora"), ("me diz as horas", "hora"), ("que dia é hoje?", "data"),
              ("abre meus downloads", "abrir_pasta"), ("aumenta o volume", "volume"),
              ("volume 30", "volume"), ("como está o sistema", "sistema"),
              ("quanto é 17 vezes 23?", "nenhum"), ("como vai estar o tempo amanhã?", "clima"), ("vai chover hoje?", "clima"),
              ("abre o youtube", "abrir:site:youtube"), ("abre o trilhear", "abrir:site:trailhead"),
              ("abre o whatsapp", "abrir:site:whatsapp"), ("pesquisa preço do PLA", "pesquisar_web"), ("tira um print", "print"),
              ("brilho 50", "brilho"), ("pausa a música", "midia"), ("pula essa", "midia"), ("timer de 20 minutos", "timer"),
              ("me avisa em 1 hora pra tirar a peça da impressora", "timer"), ("quanto custa o PLA hoje?", "nenhum"),
              ("desliga o computador", "nenhum"),
              ("apaga o arquivo teste.txt", "nenhum"), ("cria um arquivo lista.txt", "nenhum")]
    if WIN:   # ids/names that exist in this machine's Windows inventory (Start Menu .lnk, Steam games); absent apps -> "nenhum"
        cases = comuns + [("abre o gta", "abrir:jogo:271590"), ("bota o cs2", "abrir:jogo:730"),
                          ("abre o explorador", "abrir:" + _k("File Explorer")),
                          ("abre o obsidiana", "abrir:" + _k("Obsidian")), ("abre a steam", "abrir:" + _k("Steam")),
                          ("abre o discord", "abrir:" + _k("Discord")),
                          ("abre o gerenciador de tarefas", "abrir:" + _k("Task Manager")),
                          ("abre o prompt de comando", "abrir:" + _k("Command Prompt")),
                          ("abre o orca slicer", "nenhum"), ("fecha o chrome", "nenhum")]   # orca not installed here
    else:
        cases = comuns + [("abre o chrome pra mim", "abrir:app:google-chrome"), ("abre o gta", "abrir:jogo:3240220"),
                          ("bota o cs2", "abrir:jogo:730"), ("abre o cities skylines 2", "abrir:jogo:949230"),
                          ("abre o espectáculo", "abrir:app:org.kde.spectacle"), ("abre o console", "abrir:app:org.kde.konsole"),
                          ("abre o obsidiana", "abrir:app:md.obsidian.Obsidian"), ("abre a steam", "abrir:app:com.valvesoftware.Steam"),
                          ("abre a calculadora", "abrir:app:org.kde.kcalc"),
                          ("abre o orca slicer", "abrir:app:com.orcaslicer.OrcaSlicer"), ("fecha o chrome", "nenhum")]
    if "--teste-filtro" in sys.argv:
        _teste_filtro(cases)
        sys.exit(0)
    # handlers (no Gemini needed)
    assert hora("").startswith("São ") and data("").startswith("Hoje é ")
    assert _pasta("meus downloads") and _pasta("meus downloads").is_dir()
    assert _pasta("pasta projetos") == HOME / "Projetos", _pasta("pasta projetos")
    assert _pasta("xyz inexistente") is None
    for fora in ("..", "../Public", "Projetos/..", "Projetos\\fedora-assistente", "C:\\Windows", "C:", "/etc"):
        assert _pasta(fora) is None and abrir_pasta(fora) is None, fora
    inv = inventario()
    assert ("app:google-chrome" in inv if not WIN else any(v[0] == "Steam" for v in inv.values()))         and "site:youtube" in inv and any(k.startswith("jogo:") for k in inv)
    assert duracao("20 minutos") == 1200 and duracao("1 hora e meia") == 5400 and duracao("meia hora") == 1800
    assert duracao("uma hora e 15 minutos") == 4500 and duracao("tirar a peça") is None
    assert _section("Sites")["trailhead"].startswith("https://")
    _teste_filtro(cases)
    if llm.ready():
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
        print("sem GEMINI_API_KEY: roteamento não testado")
    print("atalhos ok")
