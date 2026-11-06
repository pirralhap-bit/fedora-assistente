"""
Token savers for the Claude turn:
- a Claude session lives at most JANELA seconds idle / MAX_TURNOS turns; after that a fresh one starts
  and gets the last few exchanges from a small file instead of the whole (ever-growing) history;
- light mode by default (no skills list, basic tools); "a fundo" / "modo analista" / "modo completo" turns everything on.
"""
import os
import pathlib
import time

from atalhos import fold

_state = pathlib.Path(os.environ.get("XDG_STATE_HOME") or pathlib.Path.home() / ".local" / "state")
FILE = pathlib.Path(os.environ.get("FEDORA_CONVERSA") or _state / "fedora" / "conversa.md")
JANELA = int(os.environ.get("FEDORA_JANELA", "900"))       # 0 = no limit (old behaviour: resume forever)
MAX_TURNOS = int(os.environ.get("FEDORA_MAX_TURNOS", "10"))
LEVE = os.environ.get("FEDORA_MODO_LEVE", "1") != "0"      # 0 = always the full Claude Code
KEEP = 5
GATILHOS = ("a fundo", "modo analista", "modo completo")


def completo(message):
    """True when this turn needs skills (B3, apostilas...)."""
    return not LEVE or any(g in fold(message) for g in GATILHOS)


def continua(session, full, agora=None):
    """Whether this turn may --resume the current Claude session."""
    if not session.get("id") or session.get("modo") != full:   # switching modes changes the prompt: fresh session
        return False
    if JANELA <= 0:
        return True
    agora = time.time() if agora is None else agora
    return agora - session.get("ultima", 0) < JANELA and session.get("turnos", 0) < MAX_TURNOS


def _curto(text, n=300):
    text = " ".join(str(text).split())
    return text[:n] + ("…" if len(text) > n else "")


def lembrar(pergunta, resposta):
    """Append one exchange (one line), keep the last KEEP."""
    if not resposta:
        return
    linha = f"- {time.strftime('%d/%m %H:%M')} · você: {_curto(pergunta)} · Fedora: {_curto(resposta)}"
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text("\n".join((_linhas() + [linha])[-KEEP:]) + "\n", encoding="utf-8")
    except OSError:
        pass   # losing the recap only costs continuity, never the answer


def _linhas():
    try:
        return [l for l in FILE.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]
    except OSError:
        return []


def recente():
    linhas = _linhas()
    if not linhas:
        return ""
    return ("## Conversa recente (sessão nova; use só se o pedido continuar algo daqui)\n" + "\n".join(linhas))


if __name__ == "__main__":
    import tempfile
    FILE = pathlib.Path(tempfile.mkdtemp()) / "c.md"
    LEVE, JANELA, MAX_TURNOS = True, 900, 10
    assert not completo("quantos graus amanhã") and completo("Analisa a fundo a PETR4") and completo("modo analista")
    s = dict(id="x", modo=False, ultima=1000, turnos=3)
    assert continua(s, False, agora=1500)
    assert not continua(s, False, agora=1000 + 900)            # window over
    assert not continua(s, True, agora=1500)                   # mode switch
    assert not continua(dict(s, turnos=10), False, agora=1500) # too many turns
    assert not continua(dict(s, id=None), False, agora=1500)
    JANELA = 0
    assert continua(s, False, agora=10**9)                     # 0 = resume forever
    assert recente() == ""
    for i in range(7):
        lembrar(f"pergunta {i}", "resposta\nem duas linhas " + "x" * 400)
    ls = _linhas()
    assert len(ls) == KEEP and "pergunta 6" in ls[-1] and "pergunta 1" not in recente() and "…" in ls[0], ls
    lembrar("sem resposta", "")
    assert len(_linhas()) == KEEP and "sem resposta" not in recente()
    print("conversa ok")
