"""Daily notes in the Obsidian vault: list, read, save (with a conflict check), plus the vault summary."""
import datetime
import os
import pathlib
import re
import time
import unicodedata
import zoneinfo

import memory

DAILY = pathlib.Path(os.path.expanduser(os.environ.get("JARVIS_DAILY_DIR", memory.VAULT / "08-DNT 📅 Daily Notes")))
INBOX = memory.VAULT / "00-INB 📥 Inbox" / "_a-processar"
TZ = zoneinfo.ZoneInfo(os.environ.get("JARVIS_TZ", "America/Sao_Paulo"))
MONTHS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
          "setembro", "outubro", "novembro", "dezembro"]
DAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
MAX_NOTE = 2 * 1024 * 1024
# The only notes read/save will touch: the vault's own layout (today_rel). Closes CON.md/NUL.md, fullwidth dots,
# arbitrary folders. ASCII digits only; the 28 real notes of 2026-11-07 all match.
_LAYOUT = re.compile(r"[0-9]{4}/[0-9]{2} - [^/]+/[0-9]{2} - [^/]+\.md")


class Conflict(Exception):
    pass


def today_rel(now=None):
    """Same layout the vault already uses: 2026/11 - novembro/03 - terça-feira.md"""
    d = (now or datetime.datetime.now(TZ)).date()
    return f"{d.year}/{d.month:02d} - {MONTHS[d.month - 1]}/{d.day:02d} - {DAYS[d.weekday()]}.md"


def _path(rel):
    """The note file for an id from the UI ('2026/11 - novembro/07 - sábado.md'; '\\' accepted too).
    Rejected before touching the disk: absolute/UNC/drive paths (resolve() on \\\\host\\share would hit the network),
    '..', ':' (NTFS alternate streams), control and other characters Windows forbids; then whatever resolves
    (symlinks included) outside DAILY."""
    rel = str(rel).replace("\\", "/")
    parts = pathlib.PurePosixPath(rel).parts
    if (not parts or rel.startswith("/") or ".." in parts or any(c < " " or c in ':<>"|?*' for c in rel)
            or pathlib.PureWindowsPath(rel).anchor or not _LAYOUT.fullmatch(_nfc(rel))):
        raise ValueError("note outside the daily folder")
    p = (DAILY / rel).resolve()
    if p.suffix != ".md" or not p.is_relative_to(DAILY.resolve()):
        raise ValueError("note outside the daily folder")
    return p


def _shown(p):
    """Path shown in the UI: relative to the vault when the daily folder lives in it."""
    try:
        return str(p.relative_to(memory.VAULT.resolve()))
    except ValueError:
        return str(p)


def _mtime(p):
    """mtime as a decimal STRING: st_mtime_ns (~1.7e18) is above 2**53 and JSON.parse in the UI would round it."""
    return str(p.stat().st_mtime_ns)


def _decode(data):
    """(text with '\\n' only, newline style of the file). Mixed files take the majority style.
    Not UTF-8: ValueError (400 in the server) and the file is left as it is."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("a nota não está em UTF-8; abra e salve pelo Obsidian") from None
    crlf = text.count("\r\n")
    return text.replace("\r\n", "\n"), ("\r\n" if crlf and crlf >= text.count("\n") - crlf else "\n")


def listing(limit=14):
    # as_posix(): on Windows str() gives '\\' and today_rel() uses '/', so today's note was listed twice
    files = sorted((r for r in (p.relative_to(DAILY).as_posix() for p in DAILY.rglob("*.md"))
                    if _LAYOUT.fullmatch(_nfc(r))), reverse=True)[:limit]   # only what read() will open
    # NFC on both sides: a name saved in NFD (macOS sync, some tools) is still today's note, not a second one.
    # The id stays as it is on disk: NTFS doesn't normalize, so the NFC spelling would open a different file.
    t = next((f for f in files if _nfc(f) == today_rel()), today_rel())
    if t not in files:
        files.insert(0, t)
    return [dict(id=f, name=pathlib.PurePath(f).stem, today=f == t) for f in files]


def _nfc(s):
    return unicodedata.normalize("NFC", s)


def read(rel):
    p = _path(rel)
    if not p.exists():
        return dict(id=rel, text="", mtime=None, path=_shown(p))
    text, _ = _decode(p.read_bytes())
    return dict(id=rel, text=text, mtime=_mtime(p), path=_shown(p))


def save(rel, text, mtime):
    """Write the whole note. Refuses if the file changed since it was read (Obsidian edit, sync).
    mtime: the string read()/save() returned; an int from an older UI is accepted (compared as its decimal text).
    Keeps the file's newline style (new file: LF) and writes bytes, so Windows doesn't turn '\\n' into '\\r\\n'."""
    if len(text.encode()) > MAX_NOTE:
        raise ValueError("note too large")
    p = _path(rel)
    current = _mtime(p) if p.exists() else None
    if isinstance(mtime, bool) or not isinstance(mtime, (str, int, type(None))):
        raise Conflict("a nota mudou desde que foi aberta")
    if current != (None if mtime is None or mtime == "" else str(mtime)):
        raise Conflict("a nota mudou desde que foi aberta")
    nl = _decode(p.read_bytes())[1] if current else "\n"
    data = text.replace("\r\n", "\n").replace("\n", nl).encode("utf-8")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.jarvis-tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, p)   # atomic: Obsidian/sync never sees a half-written note
    finally:
        tmp.unlink(missing_ok=True)
    return _mtime(p)


MARCA = "<!-- fedora-anotacoes -->"
MAX_ANOTACAO = 500
_ITEM = re.compile(r"- [0-9]{2}:[0-9]{2} .*")


def anotar(texto, now=None):
    """Append one line ('- HH:MM texto') to today's daily note, in a '## Fedora' section marked by MARCA.
    The section is only extended while it is still the end of the note; if the person wrote below it, a new section
    starts. Never rewrites what is already there. Goes through save(), so it keeps the conflict check and the
    newline style; if the note changed meanwhile it reads again and redoes once. Returns the path shown in the UI."""
    agora = now or datetime.datetime.now(TZ)
    linha = " ".join(str(texto or "").split()).replace("<", "&lt;")[:MAX_ANOTACAO]
    if not linha:
        raise ValueError("anotação vazia")
    if not memory.VAULT.is_dir():      # e.g. run by hand without the server's .env: don't invent a vault folder
        raise ValueError("cofre não encontrado (JARVIS_VAULT)")
    item = f"- {agora:%H:%M} {linha}"
    rel = today_rel(agora)
    for _ in range(2):
        atual = read(rel)
        texto_nota = atual["text"]
        i = texto_nota.rfind(MARCA)
        resto = [x for x in texto_nota[i + len(MARCA):].split("\n") if x.strip()] if i >= 0 else None
        if resto is not None and all(_ITEM.fullmatch(x) for x in resto):
            novo = texto_nota.rstrip("\n") + "\n" + item + "\n"
        else:
            base = texto_nota.rstrip("\n")
            novo = (base + "\n\n" if base.strip() else "") + f"## Fedora\n{MARCA}\n{item}\n"
        try:
            save(rel, novo, atual["mtime"])
            return atual["path"]
        except Conflict:
            continue
    raise Conflict("a nota mudou enquanto eu anotava; tente de novo")


def vault_summary():
    files = [p for p in memory.VAULT.rglob("*.md")
             if not any(x.startswith(".") for x in p.relative_to(memory.VAULT).parts)]
    stats = sorted(((p.stat().st_mtime, p) for p in files), reverse=True)
    cutoff = time.time() - 86400
    last = stats[0] if stats else None
    return dict(total=len(files), today=sum(1 for t, _ in stats if t > cutoff),
                inbox=len(list(INBOX.glob("*.md"))) if INBOX.exists() else 0,
                last=dict(title=last[1].stem, at=last[0]) if last else None)


def _teste():
    """Offline, in a fresh temp folder (DAILY and memory.VAULT are repointed: the real vault is never touched)."""
    import json
    import tempfile
    global DAILY, today_rel
    raiz = pathlib.Path(tempfile.mkdtemp(prefix="fedora-notes-"))
    memory.VAULT, DAILY = raiz, raiz / "daily"
    hoje = today_rel()
    assert hoje.count("/") == 2 and hoje.endswith(".md") and "\\" not in hoje, hoje
    assert today_rel(datetime.datetime(2026, 11, 7, 12, tzinfo=TZ)) == "2026/11 - novembro/07 - sábado.md"

    p = DAILY / hoje
    p.parent.mkdir(parents=True)
    p.write_bytes(b"## Para hoje\n- [ ] A\n")
    (DAILY / "2026/01 - janeiro").mkdir(parents=True)
    (DAILY / "2026/01 - janeiro/02 - sexta-feira.md").write_bytes(b"velha\n")
    (DAILY / "Modelo diario.md").write_bytes(b"template\n")      # outside the layout: neither listed nor opened
    ids = [n["id"] for n in listing()]
    assert ids.count(hoje) == 1 and len(ids) == 2 and all("\\" not in i for i in ids), ids
    assert [n["today"] for n in listing()] == [True, False]

    # read + save + JSON round trip (the browser keeps the string; an int would be rounded by JSON.parse)
    n = read(hoje)
    assert n["text"] == "## Para hoje\n- [ ] A\n" and isinstance(n["mtime"], str) and n["mtime"].isdigit(), n
    assert int(n["mtime"]) > 2 ** 53
    m = json.loads(json.dumps(n))["mtime"]
    m2 = save(hoje, n["text"] + "- [ ] B\n", m)
    assert p.read_bytes() == b"## Para hoje\n- [ ] A\n- [ ] B\n" and m2 == _mtime(p)
    save(hoje, read(hoje)["text"], int(m2))           # old UI sending an int: still accepted
    try:
        save(hoje, "x", m2 if m2 != _mtime(p) else str(int(m2) - 1))
        raise AssertionError("stale mtime saved")
    except Conflict:
        pass
    m3 = read(hoje)["mtime"]
    os.utime(p, ns=(int(m3) + 10 ** 9, int(m3) + 10 ** 9))   # Obsidian/sync touched it
    for velho in (m3, int(m3), 1.7e18, None, "", True):
        try:
            save(hoje, "x", velho)
            raise AssertionError(f"conflict not detected for {velho!r}")
        except Conflict:
            pass
    assert b"x" != p.read_bytes()[:1]

    # newline style: LF stays LF, CRLF stays CRLF, a new note is LF; the UI always sends '\n'
    crlf = DAILY / "2026/01 - janeiro/03 - sábado.md"
    crlf.write_bytes("## Para hoje\r\n- [ ] Café ☕\r\n\r\n## Resumo do dia\r\n".encode())
    r = read("2026/01 - janeiro/03 - sábado.md")
    assert "\r" not in r["text"]
    save(r["id"], r["text"], r["mtime"])
    assert crlf.read_bytes() == "## Para hoje\r\n- [ ] Café ☕\r\n\r\n## Resumo do dia\r\n".encode()
    r = read(r["id"])
    save(r["id"], r["text"].replace("Café", "Chá"), r["mtime"])
    assert crlf.read_bytes() == "## Para hoje\r\n- [ ] Chá ☕\r\n\r\n## Resumo do dia\r\n".encode()
    r = read(hoje)
    save(hoje, r["text"] + "fim\r\n", r["mtime"])          # stray CRLF from the client doesn't mix styles
    assert b"\r" not in p.read_bytes() and p.read_bytes().endswith(b"fim\n")
    novo = "2026/01 - janeiro/04 - domingo.md"
    save(novo, "a\nb\n", None)
    assert (DAILY / novo).read_bytes() == b"a\nb\n"
    try:
        save(novo, "c\n", None)                            # it exists now: None is stale
        raise AssertionError("None accepted for an existing note")
    except Conflict:
        pass

    # atomic write leaves no temp file, even when the replace fails
    assert not list(DAILY.rglob("*.jarvis-tmp")) and not list(DAILY.rglob(".*"))
    real = os.replace
    try:
        def falha(*a):
            raise PermissionError("locked")
        globals()["os"].replace = falha
        r = read(hoje)
        try:
            save(hoje, "perdido\n", r["mtime"])
            raise AssertionError("replace failure swallowed")
        except PermissionError:
            pass
    finally:
        os.replace = real
    assert not list(DAILY.rglob("*.jarvis-tmp")) and b"perdido" not in p.read_bytes()

    # path traversal and friends are refused before touching the disk
    fora = raiz / "fora.md"
    fora.write_bytes(b"segredo\n")
    for mau in ("../fora.md", "2026/../../fora.md", "..\\fora.md", str(fora), fora.as_posix(), "/fora.md",
                "\\\\host\\share\\x.md", "//host/share/x.md", "C:fora.md", "x.md:stream", "nota.txt", "", "a\0.md", "..\x0cora.md", "a?.md",
                "2026/01 - janeiro", "CON.md", "NUL.md", "2026/01 - janeiro/CON.md", "．．/fora.md",
                "a/ ../x.md", "x.md/", "~/x.md", "Modelo diario.md", "2026/01 - janeiro/02 - sexta-feira.md/",
                "２０２６/01 - janeiro/02 - sexta-feira.md", "2026/01 - janeiro/x/02 - y.md"):
        for f in (read, lambda r: save(r, "x", None)):
            try:
                f(mau)
                raise AssertionError(f"accepted {mau!r}")
            except ValueError:
                pass
    assert fora.read_bytes() == b"segredo\n"
    assert read("2026\\01 - janeiro\\02 - sexta-feira.md")["text"] == "velha\n"   # old '\\' ids still open
    # not UTF-8 (cp1252 from an old editor): friendly ValueError on read and save, file untouched
    latin = DAILY / "2026/01 - janeiro/05 - segunda-feira.md"
    latin.write_bytes("Café\r\n".encode("cp1252"))
    for f in (lambda: read(latin.relative_to(DAILY).as_posix()),
              lambda: save(latin.relative_to(DAILY).as_posix(), "x", _mtime(latin))):
        try:
            f()
            raise AssertionError("non-UTF-8 note accepted")
        except ValueError as e:
            assert "UTF-8" in str(e) and not isinstance(e, UnicodeDecodeError), e
    assert latin.read_bytes() == b"Caf\xe9\r\n"

    # the UI path still works: listing -> read -> save today's note
    t = [x for x in listing() if x["today"]][0]
    r = read(t["id"])
    save(t["id"], r["text"] + "ok\n", json.loads(json.dumps(r))["mtime"])
    assert read(t["id"])["text"].endswith("ok\n")
    assert vault_summary()["total"] == 7

    # today's note saved with an NFD name (accents as combining marks): listed once, as today, and it opens
    real_today = today_rel
    try:
        today_rel = lambda now=None: "2026/03 - março/14 - sábado.md"   # noqa: E731 (fixed accented day)
        nfd = unicodedata.normalize("NFD", today_rel())
        assert nfd != today_rel()
        (DAILY / nfd).parent.mkdir(parents=True)
        (DAILY / nfd).write_bytes(b"nfd\n")
        hoje_l = [x for x in listing() if x["today"]]
        assert len(hoje_l) == 1 and hoje_l[0]["id"] == nfd, hoje_l
        assert sum(_nfc(x["id"]) == today_rel() for x in listing()) == 1
        r = read(hoje_l[0]["id"])
        assert r["text"] == "nfd\n"
        save(r["id"], "nfd 2\n", r["mtime"])
        assert (DAILY / nfd).read_bytes() == b"nfd 2\n"
    finally:
        today_rel = real_today
    # anotar(): section created once, lines appended, the person's text above and below is never touched
    agora = datetime.datetime(2026, 3, 20, 9, 5, tzinfo=TZ)
    rel = today_rel(agora)
    alvo = DAILY / rel
    anotar("Atualizei o Graphify\npara a 0.9.105 <b>", agora)                 # new note: created
    assert alvo.read_bytes() == (f"## Fedora\n{MARCA}\n- 09:05 Atualizei o Graphify para a 0.9.105 &lt;b>\n").encode()
    anotar("Segunda linha", agora.replace(minute=40))
    assert alvo.read_text(encoding="utf-8").endswith(f"{MARCA}\n- 09:05 Atualizei o Graphify para a 0.9.105 &lt;b>\n- 09:40 Segunda linha\n")
    assert alvo.read_text(encoding="utf-8").count("## Fedora") == 1
    alvo.write_bytes(alvo.read_bytes() + b"\nEscrevi isto depois.\n")          # the person wrote below the section
    anotar("Terceira", agora.replace(hour=18, minute=0))
    t = alvo.read_text(encoding="utf-8")
    assert t.count("## Fedora") == 2 and "Escrevi isto depois.\n\n## Fedora\n" in t and t.endswith("- 18:00 Terceira\n"), t
    alvo.write_bytes(("# Dia\r\nminha parte\r\n").encode())                    # CRLF note without a section
    anotar("Em CRLF", agora)
    assert alvo.read_bytes() == f"# Dia\r\nminha parte\r\n\r\n## Fedora\r\n{MARCA}\r\n- 09:05 Em CRLF\r\n".encode()
    for vazio in ("", "   \n ", None):
        try:
            anotar(vazio, agora)
            raise AssertionError("empty note accepted")
        except ValueError:
            pass
    assert len(anotar("x" * 2000, agora)) > 0 and max(map(len, alvo.read_text(encoding="utf-8").split("\n"))) <= MAX_ANOTACAO + 20
    assert not list(DAILY.rglob("*.jarvis-tmp"))
    real_vault = memory.VAULT
    try:
        memory.VAULT = raiz / "nao-existe"
        try:
            anotar("x", agora)
            raise AssertionError("missing vault accepted")
        except ValueError as e:
            assert "cofre" in str(e) and not (raiz / "nao-existe").exists()
    finally:
        memory.VAULT = real_vault
    print("notes ok")


if __name__ == "__main__":
    import sys
    if "--teste" in sys.argv:
        _teste()
    elif len(sys.argv) >= 3 and sys.argv[1] == "--anota":
        try:
            print("anotado em", anotar(" ".join(sys.argv[2:])))
        except (ValueError, Conflict, OSError) as e:
            print("não anotei:", e)
            sys.exit(1)
    else:
        print('uso: python notes.py --anota "texto"   |   python notes.py --teste   (offline, pasta temporária)')
