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
GATILHOS = ("a fundo", "modo analista", "modo completo", "modo b3", "day trade", "operar na b3", "boleta b3")
# light tools + the Agent tool (+2.8k tokens, measured): only when the request is about delegating or reviewing
GATILHOS_EQUIPE = ("equipe", "delegue", "delegar", "junior", "writer", "design", "arquiteto", "senior", "revisor", "peca ao qa", "mande o qa", "passe pro qa")


def completo(message):
    """Mode of this turn: True = full Claude Code (skills B3, apostilas...), "equipe" = light + subagents, False = light."""
    m = fold(message)
    if not LEVE or any(g in m for g in GATILHOS):
        return True
    if any(g in m for g in GATILHOS_EQUIPE):
        return "equipe"
    return False


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


NOMES_AGENTES = {
    'techlead': 'tech lead',
    'junior': 'júnior',
    'writer': 'writer',
    'design': 'design',
    'arquiteto': 'arquiteto',
    'senior': 'sênior',
    'qa': 'Q A',
}


def classificar_pedido(message):
    m = fold(message)
    comp = completo(message)
    if comp == 'equipe':
        return 'equipe'
    if comp is True:
        return 'completo'
    if any(p in m for p in ('cofre', 'vault', 'nota', 'diario', 'retrospectiva', 'anota', 'anote', 'backlog')):
        return 'cofre'
    if any(p in m for p in ('abra', 'abrir', 'feche', 'fechar', 'arquivo', 'pasta', 'codigo', 'teste', 'sistema', 'terminal', 'git')):
        return 'pc'
    return 'conversa'


def fala_etapa_inicio(message, modo=False, motor=None):
    tipo = 'equipe' if modo == 'equipe' else ('completo' if modo is True else classificar_pedido(message))
    frases = {
        'equipe': 'Entendido, vou planejar e acionar a equipe.',
        'completo': 'Entendido, vou analisar a fundo o seu pedido.',
        'cofre': 'Entendido, consultando o seu cofre.',
        'pc': 'Entendido, verificando no sistema.',
        'conversa': 'Entendido, processando o seu pedido.',
    }
    return frases.get(tipo, 'Entendido, processando o seu pedido.')


def fala_etapa_repasse(agente):
    agente_str = str(agente or '').strip().lower()
    nome = NOMES_AGENTES.get(agente_str, str(agente or 'agente'))
    if agente_str == 'techlead':
        return 'Repassando para triagem do tech lead.'
    if agente_str == 'qa':
        return 'Enviando para revisão do Q A.'
    if agente_str == 'arquiteto':
        return 'Consultando o arquiteto.'
    return f'Repassando a tarefa para o {nome}.'


def fala_etapa_retorno(agente, ok=True, escalar=False):
    agente_str = str(agente or '').strip().lower()
    nome = NOMES_AGENTES.get(agente_str, str(agente or 'agente'))
    if escalar:
        return f'Recebi o retorno do {nome}, que pediu escalonamento.'
    if not ok:
        return f'O agente {nome} retornou com falha.'
    if agente_str == 'qa':
        return 'Recebi a validação do Q A.'
    if agente_str == 'techlead':
        return 'Recebi a triagem do tech lead.'
    return f'Recebi o retorno do {nome}.'


if __name__ == "__main__":
    import tempfile
    FILE = pathlib.Path(tempfile.mkdtemp()) / "c.md"
    LEVE, JANELA, MAX_TURNOS = True, 900, 10
    assert not completo("quantos graus amanhã") and completo("Analisa a fundo a PETR4") and completo("modo analista")
    assert completo("peça ao júnior para ajustar o README") == "equipe" and completo("delegue isso à equipe") == "equipe"
    assert completo("faz isso a fundo com a equipe") is True
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

    # Testes das funcoes de classificacao e fala em etapas
    assert classificar_pedido("delegue para a equipe") == "equipe"
    assert classificar_pedido("analisa a fundo") == "completo"
    assert classificar_pedido("leia o backlog e a nota do vault") == "cofre"
    assert classificar_pedido("abra o terminal e rode o git") == "pc"
    assert classificar_pedido("boa tarde como vai") == "conversa"

    assert fala_etapa_inicio("qualquer", modo="equipe") == "Entendido, vou planejar e acionar a equipe."
    assert fala_etapa_inicio("qualquer", modo=True) == "Entendido, vou analisar a fundo o seu pedido."
    assert fala_etapa_inicio("leia a nota no cofre") == "Entendido, consultando o seu cofre."
    assert fala_etapa_inicio("abra o arquivo de teste") == "Entendido, verificando no sistema."
    assert fala_etapa_inicio("olá tudo bem") == "Entendido, processando o seu pedido."

    assert fala_etapa_repasse("techlead") == "Repassando para triagem do tech lead."
    assert fala_etapa_repasse("qa") == "Enviando para revisão do Q A."
    assert fala_etapa_repasse("arquiteto") == "Consultando o arquiteto."
    assert fala_etapa_repasse("junior") == "Repassando a tarefa para o júnior."
    assert fala_etapa_repasse("writer") == "Repassando a tarefa para o writer."

    assert fala_etapa_retorno("junior", escalar=True) == "Recebi o retorno do júnior, que pediu escalonamento."
    assert fala_etapa_retorno("writer", ok=False) == "O agente writer retornou com falha."
    assert fala_etapa_retorno("qa") == "Recebi a validação do Q A."
    assert fala_etapa_retorno("techlead") == "Recebi a triagem do tech lead."
    assert fala_etapa_retorno("junior") == "Recebi o retorno do júnior."

    print("conversa ok")
