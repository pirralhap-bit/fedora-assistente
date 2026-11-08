"""
Turn log: one JSON line per turn (voice or chat) in turnos.jsonl, OUTSIDE the repo and the vault — the same state
folder conversa.py uses (FEDORA_TURNLOG overrides the file). retro.py reads it to write the weekly retrospective.

    python turnolog.py --teste     (offline, temporary folder)

Never breaks a turn: every failure here is swallowed (and printed once per kind).
"""
import datetime
import json
import os
import pathlib
import re
import threading
import time

from atalhos import fold

_state = pathlib.Path(os.environ.get("XDG_STATE_HOME") or pathlib.Path.home() / ".local" / "state")   # = conversa.py
FILE = pathlib.Path(os.environ.get("FEDORA_TURNLOG") or _state / "fedora" / "turnos.jsonl")
MAX_BYTES = 5 * 1024 * 1024       # past this, turnos.jsonl -> turnos.1.jsonl (only one old file is kept)
PEDIDO_CURTO = 60                 # an ordinary turn keeps only the start of the request (privacy, size)
PEDIDO_SINAL = 400                # a flagged turn keeps the whole request (up to this), for the retrospective
JANELA_REPETE = 30                # seconds: same request again within this = "repeticao"
ERRO_MAX = 160
ROTAS = ("comando", "atalho", "filtro_local→claude", "claude", "erro")

_LOCK = threading.Lock()
_ANTERIOR = {"pedido": None, "mono": 0.0, "rota": None}   # the previous turn, for repetition detection
_AVISOS = set()


def _aviso(tipo, e):
    if tipo not in _AVISOS:
        _AVISOS.add(tipo)
        print(f"[turnolog] {tipo}: {type(e).__name__}: {str(e)[:120]} (o turno segue; aviso só uma vez)", flush=True)


def antigo(path=None):
    p = pathlib.Path(path or FILE)
    return p.with_name(p.stem + ".1" + p.suffix)


def normaliza(pedido):
    """atalhos.fold (case, accents) + no punctuation, single spaces: Whisper writes "abre o orca!" or "Abre o Orca."."""
    return " ".join(re.sub(r"[^\w\s]", " ", fold(pedido)).split())


# before anything is written: the value after a trigger word ("senha do banco é X", "meu pix: X") and any number with
# >= 6 digits (CPF, CNPJ, card, account, phone, even split by . - / or spaces) become [oculto]
_GATILHO = re.compile(r"(?i)\b(senhas?|tokens?|cpf|cnpj|cart[aã]o|pix|chaves?)\b"
                      r"((?:\s+(?:d[aoe]s?|meu|minha|novo|nova|atual|é|e|eh|era|ficou|vai ser|pix|banco|do banco|cartão|cartao|"
                      r"github|wifi|api|cr[eé]dito|d[eé]bito))*\s*[:=]?\s*)([^\s,;]+)")
_DIGITOS = re.compile(r"(?<![\w])\d(?:[\d.\-/ ]*\d)?(?![\w])")
OCULTO = "[oculto]"


def mascara(texto):
    """The request with secrets and long numbers replaced by [oculto] (QA, lote E)."""
    t = _DIGITOS.sub(lambda m: OCULTO if sum(c.isdigit() for c in m.group()) >= 6 else m.group(), str(texto or ""))
    return _GATILHO.sub(lambda m: m.group(1) + m.group(2) + OCULTO, t)


def _sinais(reg, agora):
    """Flags of this turn; reads and updates _ANTERIOR (call under _LOCK)."""
    s = []
    pedido_n = normaliza(reg.get("pedido"))
    ant = _ANTERIOR
    recente = ant["pedido"] is not None and agora - ant["mono"] <= JANELA_REPETE and pedido_n == ant["pedido"]
    if recente:
        s.append("repeticao")
        # the previous one went to Claude only because the local filter kept it off Gemini, and the user had to ask
        # again (or it became a shortcut now): the flag sits on THIS line, which carries the whole request
        if ant["rota"] == "filtro_local→claude":
            s.append("atalho_barrado_e_repetido")
    if reg.get("escalar"):
        s.append("escalar")
    if reg.get("cancelado"):
        s.append("cancelado")
    if reg.get("erro"):
        s.append("erro")
    if reg.get("resposta_vazia"):
        s.append("resposta_vazia")
    ant.update(pedido=pedido_n, mono=agora, rota=reg.get("rota"))
    return s


def _linha(reg, sinais):
    pedido = " ".join(mascara(reg.get("pedido")).split())
    n = PEDIDO_SINAL if sinais else PEDIDO_CURTO
    out = dict(
        ts=reg.get("ts") or datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        canal=reg.get("canal") or "voz",
        pedido=pedido[:n] + ("…" if len(pedido) > n else ""),
        rota=reg.get("rota") or "claude",
        atalho=reg.get("atalho"),
        modo=reg.get("modo"),
        sessao_nova=bool(reg.get("sessao_nova")),
        agentes=list(reg.get("agentes") or []),
        n_tools=int(reg.get("n_tools") or 0),
        ms=int(reg.get("ms") or 0),
        ms_primeiro_texto=reg.get("ms_primeiro_texto"),
        tokens_total=reg.get("tokens_total"),
        tokens_out=reg.get("tokens_out"),
        erro=(" ".join(mascara(reg["erro"]).split())[:ERRO_MAX] if reg.get("erro") else None),
        cancelado=bool(reg.get("cancelado")),
        gemini=reg.get("gemini") or "nao_usado",
        sinais=sinais,
    )
    for k in ("ms_gemini", "disjuntor_abriu", "filtrado"):   # only when they happened (retro counts them)
        if reg.get(k):
            out[k] = reg[k]
    return json.dumps(out, ensure_ascii=False)


def registrar(reg, path=None, agora=None):
    """Append one turn. reg: the fields above (pedido, rota, ...) plus escalar/resposta_vazia booleans.
    Returns the written dict, or None if anything failed (never raises)."""
    try:
        p = pathlib.Path(path or FILE)
        with _LOCK:
            linha = _linha(reg, _sinais(reg, time.monotonic() if agora is None else agora))
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                if p.stat().st_size >= MAX_BYTES:
                    os.replace(p, antigo(p))
            except FileNotFoundError:
                pass
            with open(p, "a", encoding="utf-8", newline="\n") as fh:   # one write() per line: no torn lines
                fh.write(linha + "\n")
        return json.loads(linha)
    except Exception as e:  # noqa: BLE001 — the log must never cost a turn
        _aviso("falha ao gravar", e)
        return None


def ler(path=None):
    """Every turn on disk, oldest first (turnos.1.jsonl, then turnos.jsonl); broken lines are skipped."""
    p = pathlib.Path(path or FILE)
    out = []
    for f in (antigo(p), p):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    try:
                        d = json.loads(ln)
                        if isinstance(d, dict) and d.get("ts"):
                            out.append(d)
                    except ValueError:
                        pass
        except OSError:
            pass
    return out


def _teste():
    import sys
    import tempfile
    global MAX_BYTES
    sys.stdout.reconfigure(errors="replace")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="turnolog-"))
    f = tmp / "sub" / "turnos.jsonl"
    _ANTERIOR.update(pedido=None, mono=0.0, rota=None)

    # truncation: ordinary turn 60 chars, flagged turn whole (<= 400)
    longo = "me explica com calma " + "x" * 500
    d = registrar(dict(pedido=longo, rota="claude", modo="leve", canal="voz"), f, agora=1000)
    assert d and len(d["pedido"]) == PEDIDO_CURTO + 1 and d["sinais"] == [] and d["gemini"] == "nao_usado", d
    d = registrar(dict(pedido="outra " + longo, rota="erro", erro="Claude went quiet\nfor 180s " + "e" * 300), f, agora=1100)
    assert d["sinais"] == ["erro"] and len(d["pedido"]) == PEDIDO_SINAL + 1 and len(d["erro"]) == ERRO_MAX, d

    # repetition: same request (accents/case folded) within 30 s; and after a local-filter turn
    registrar(dict(pedido="Abre o Ôrca!", rota="filtro_local→claude"), f, agora=2000)
    d = registrar(dict(pedido="abre o orca", rota="atalho", atalho="abrir"), f, agora=2010)
    assert d["sinais"] == ["repeticao", "atalho_barrado_e_repetido"] and d["pedido"] == "abre o orca", d
    d = registrar(dict(pedido="abre o orca", rota="atalho"), f, agora=2100)                 # 90 s later: not a repeat
    assert d["sinais"] == [], d
    d = registrar(dict(pedido="faz o deploy", rota="claude", escalar=True, cancelado=True, resposta_vazia=True,
                       agentes=["senior"], n_tools=3, tokens_total=1234, tokens_out=56, ms_gemini=0), f, agora=3000)
    assert d["sinais"] == ["escalar", "cancelado", "resposta_vazia"] and d["agentes"] == ["senior"] and "ms_gemini" not in d, d
    campos = {"ts", "canal", "pedido", "rota", "atalho", "modo", "sessao_nova", "agentes", "n_tools", "ms", "ms_primeiro_texto",
              "tokens_total", "tokens_out", "erro", "cancelado", "gemini", "sinais"}
    assert all(campos <= set(x) for x in ler(f)) and len(ler(f)) == 6

    # secrets and long numbers never reach the disk
    for antes, depois in (("minha senha do banco é 1234 salva aí", "minha senha do banco é [oculto] salva aí"),
                          ("meu CPF é 123.456.789-00", "meu CPF é [oculto]"), ("o cnpj 12.345.678/0001-90 da loja", "o cnpj [oculto] da loja"),
                          ("a chave pix é fulano@exemplo.com", "a chave pix é [oculto]"), ("token: ghp_abcDEF123", "token: [oculto]"),
                          ("cartão 4111 1111 1111 1111 vence", "cartão [oculto] vence"), ("liga pro 15991228090", "liga pro [oculto]"),
                          ("timer de 20 minutos", "timer de 20 minutos"), ("volume 30", "volume 30"),
                          ("o exame deu 240 hoje às 10", "o exame deu 240 hoje às 10")):
        assert mascara(antes) == depois, (antes, mascara(antes))
    d = registrar(dict(pedido="minha senha é hunter2 e o cpf 12345678900", rota="erro", erro="falhou com cpf 12345678900"), f,
                  agora=5000)
    assert "hunter2" not in json.dumps(d) and "12345678900" not in json.dumps(d) and d["pedido"].count(OCULTO) == 2, d
    assert "hunter2" not in f.read_text(encoding="utf-8")

    # 20 threads x 25 lines: no torn lines, nothing lost
    g = tmp / "conc.jsonl"
    ths = [threading.Thread(target=lambda i=i: [registrar(dict(pedido=f"t{i} n{j} " + "y" * 200, rota="claude"), g)
                                                for j in range(25)]) for i in range(20)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    linhas = g.read_text(encoding="utf-8").splitlines()
    assert len(linhas) == 500 and all(json.loads(x)["rota"] == "claude" for x in linhas), len(linhas)

    # rotation: past MAX_BYTES the file becomes turnos.1.jsonl (one kept), reading sees both
    MAX_BYTES = 2000
    r = tmp / "rot" / "turnos.jsonl"
    for i in range(40):
        registrar(dict(pedido=f"pedido {i} " + "z" * 50, rota="claude"), r)
    assert antigo(r).exists() and r.stat().st_size < 2000 + 400 and not (r.parent / "turnos.2.jsonl").exists()
    assert len(list(r.parent.iterdir())) == 2, list(r.parent.iterdir())
    vistos = [x["pedido"].split()[1] for x in ler(r)]
    assert vistos[-1] == "39" and vistos == sorted(vistos, key=int), vistos
    MAX_BYTES = 5 * 1024 * 1024

    # a write that fails never raises: a folder in place of the file, an invalid path, a read-only file
    (tmp / "pasta.jsonl").mkdir()
    assert registrar(dict(pedido="x"), tmp / "pasta.jsonl") is None
    assert registrar(dict(pedido="x"), "Z:\\nao\\existe\\\0turnos.jsonl") is None
    ro = tmp / "ro.jsonl"
    ro.write_text("", encoding="utf-8")
    os.chmod(ro, 0o444)
    try:
        assert registrar(dict(pedido="x"), ro) is None
    finally:
        os.chmod(ro, 0o666)
    assert registrar(dict(pedido=object()), tmp / "ok.jsonl") is not None   # odd input still logs (str())
    print(f"turnolog ok (offline, {tmp})")


if __name__ == "__main__":
    _teste()
