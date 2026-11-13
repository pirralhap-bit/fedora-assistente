"""
Command matrix for Fedora: slash commands and shortcuts answer locally; everything else
goes to Claude Code with vault context attached.
"""
import json
import os
import pathlib
import re
import sys
import threading
import time
import uuid

import atalhos
import memory

ROOT = pathlib.Path(__file__).resolve().parent
STATE_FILE = pathlib.Path(os.environ.get("JARVIS_STATE", ROOT / "state.json"))
_LOCK = threading.Lock()
_DEFAULT = {"profile": [], "goal": "", "personality": "", "tasks": []}


def _load_unlocked():
    try:
        return {**_DEFAULT, **json.loads(STATE_FILE.read_text(encoding="utf-8"))}
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT)


def load():
    with _LOCK:
        return _load_unlocked()


def _save_unlocked(d):
    tmp = STATE_FILE.with_name(f".{STATE_FILE.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def update_state(mutator):
    with _LOCK:
        d = _load_unlocked()
        result = mutator(d)
        _save_unlocked(d)
        return result


def _tokens_block():
    """Real token/plan numbers from the last turn, so "quantos tokens você gastou?" is answered, not guessed."""
    import runtime   # lazy, like /status
    L = runtime.LIMITS
    lines = []
    if L.get("last"):
        n = lambda v: f"{v:,}".replace(",", ".")   # 56.451, pt-BR
        lines.append(f"- Pergunta anterior: {n(L['last']['total'])} tokens no total (cache incluído), "
                     f"{n(L['last']['out'])} escritos na resposta.")
    for key, name in (("five_hour", "janela de 5 horas"), ("seven_day", "semana")):
        w = L.get(key) or {}
        if w.get("used") is not None:
            resets = time.strftime(" (zera %d/%m %H:%M)", time.localtime(w["resets"])) if w.get("resets") else ""
            lines.append(f"- Uso do plano na {name}: {round(w['used'] * 100)}%{resets}.")
    if not lines:
        return ""
    return ("## Seus tokens (números reais do servidor; use estes, não estime)\n" + "\n".join(lines)
            + "\nA pergunta atual ainda não tem contagem: só fica pronta quando você terminar de responder.")


EQUIPE_FILE = ROOT / "docs" / "EQUIPE.md"


def comando_agentes():
    """The shell command any engine uses to call a team agent: absolute paths with /, so it works in bash and PowerShell."""
    return f'"{pathlib.Path(sys.executable).as_posix()}" "{(ROOT / "agentes.py").as_posix()}"'


def comando_nota():
    """The shell command that appends one line to today's daily note (notes.py --anota)."""
    return f'"{pathlib.Path(sys.executable).as_posix()}" "{(ROOT / "notes.py").as_posix()}" --anota'


NOTA_RULES = """
## Anotar no cofre
Quando o usuário pedir para anotar algo, ou contar algo que aconteceu ou que ele decidiu, ou quando você concluir uma mudança relevante no projeto a pedido dele, grave UMA linha curta na daily note de hoje: {NOTA} "<texto>"
O texto é uma frase em português, como fato ("Atualizei o Graphify para a 0.9.105"). Não anote por conta própria o que ele não pediu nem contou, e nunca anote segredo (senha, token, CPF, cartão). Confirme em poucas palavras ("Anotei.")."""


def _nota():
    return NOTA_RULES.replace("{NOTA}", comando_nota())


def _equipe():
    try:
        return EQUIPE_FILE.read_text(encoding="utf-8").replace("{AGENTES}", comando_agentes())
    except OSError:
        return ""


def system_block(chat=False, equipe=False):
    """The part that rarely changes: goes in the system prompt, so Claude's prompt cache keeps hitting."""
    d = load()
    out = []
    if d["profile"]:
        out.append("## Operator profile\n" + "\n".join(f"- {x}" for x in d["profile"][-40:]))
    if d["goal"]:
        out.append(f"## Standing objective\n{d['goal']}")
    if d["personality"]:
        out.append(f"## Tone overlay\n{d['personality']}")
    out.append((CHAT_STYLE if chat else VOICE_STYLE) + PC_RULES + _nota())
    if equipe and not chat:
        out.append(_equipe())
    return "\n\n".join(out)


_TOKENS_ASK = re.compile(r"\b(tokens?|uso|plano|planos|cotas?)\b")   # só então o bloco de tokens entra no turno


def turn_block(message=""):
    """The part that changes every turn (clock, tokens, queue, vault recall): rides in the user message,
    because anything that changes inside the system prompt throws away the cache behind it."""
    d = load()
    # saves a tool round trip (date) on "que horas são?" and lets "amanhã"/"sexta" resolve without one
    out = [time.strftime("## Agora\n%A, %d/%m/%Y %H:%M (horário local)")]
    tok = _tokens_block() if _TOKENS_ASK.search(atalhos.fold(message)) else ""
    if tok:
        out.append(tok)
    open_tasks = [t for t in d["tasks"] if not t.get("done")]
    if open_tasks:
        out.append("## Mission queue\n" + "\n".join(f"- {t['text']}" for t in open_tasks[:20]))
    recall = memory.context_for(message)
    if recall:
        out.append(recall)
    return "\n\n".join(out)


VOICE_STYLE = """## Como responder
Você é a Fedora, assistente de voz do usuário no PC dele (Windows 11), em português do Brasil.
Responda como uma assistente de voz tipo Alexa: simpática, natural, resposta direta primeiro, uma ou duas frases.
Pergunta de conhecimento geral: responda do que você sabe. Coisa atual (clima, notícia, preço, placar): pesquise na web.
A resposta é lida em voz alta: sem markdown, sem listas, sem emoji, sem caminhos longos nem comandos na fala.
Seja curta e objetiva: no máximo duas frases curtas, com palavras simples e sem jargão técnico. Nada de explicar o que fez por dentro nem listar detalhes; só o resultado. Só se alongue se o usuário pedir mais detalhes.
Quando o conteúdo precisar ser LIDO e não ouvido (código, tabela, lista longa, passo a passo), responda assim: primeiro uma ou duas frases faladas com o resumo; depois uma linha só com --- ; depois o conteúdo completo em markdown, que aparece na tela e não é falado.
"""

CHAT_STYLE = """## Como responder
Você é a Fedora, assistente do usuário no PC dele (Windows 11), em português do Brasil,
agora no modo chat: ele DIGITA e LÊ na tela, como no Claude Code. Pode usar markdown (listas, tabelas, blocos de código,
caminhos e comandos). Resposta direta primeiro; aprofunde quando o pedido pedir. Use as skills quando ajudarem.
Coisa atual (clima, notícia, preço, placar): pesquise na web.
"""

PC_RULES = """
## Você controla o PC
Você tem acesso total ao computador do usuário e deve FAZER o que ele pede, não explicar como fazer:
abrir app (cmd //c start "" <app ou URL>, ou Start-Process no PowerShell), fechar app (taskkill //IM <app>.exe, ou //PID <pid>),
abrir pasta (explorer <pasta>), criar e escrever arquivos, buscar arquivos, pesquisar na web.
Ao terminar, confirme em poucas palavras ("Pronto, abri o Chrome.").
Regras fixas:
- Apagar é SEMPRE mandar para a Lixeira (powershell -NoProfile -Command "Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile('<caminho>','OnlyErrorDialogs','SendToRecycleBin')"; para pasta use DeleteDirectory), nunca rm, del nem Remove-Item. Antes de apagar, diga o que vai apagar e pergunte "confirma?"; só apague depois do sim.
- Antes de fechar um app que pode ter trabalho não salvo (editor, documento, jogo), pergunte "confirma?".
- Nunca desligue, reinicie, suspenda nem encerre a sessão do PC, mesmo que peçam.
- Se o pedido parecer ter sido ouvido pela metade ou não fizer sentido, pergunte em vez de agir."""


# ── slash commands ───────────────────────────────────────────────
_CMD = re.compile(
    r"^\s*/(new|profile|goal|personality|kanban|mission|missions|recall|memory|graph|"
    r"vault|status|commands|help|clear)\b\s*(.*)$", re.I | re.S)


def _help():
    return """Dashboard commands:
/new — start a fresh Claude session
/goal <text|status|clear> — standing objective
/profile <fact> — remember something about you
/personality <tone> — adjust delivery
/mission [task] — read or add to the mission queue
/recall <query> — search the memory vault
/graph — rebuild the memory graph from disk
/vault — where the vault lives and how big it is
/status — runtime, model, session
/clear — clear the response panel
/help — this list

Anything else goes to Claude Code with the relevant vault notes attached."""


_MOTOR_ALVO = {"agy": "agy", "antigravity": "agy", "google": "agy", "gemini": "agy", "claude": "claude", "cloud": "claude",
               "auto": "auto", "automatico": "auto"}   # "cloud" = como o ouvido escreve "Claude"
_MOTOR_VERBO = {"troca", "trocar", "muda", "mudar", "passa", "passar", "coloca", "colocar", "poe", "bota", "volta", "voltar",
                "usa", "usar", "use", "vai", "ir", "fica", "ficar", "deixa", "deixar", "seleciona", "selecionar", "ativa", "ativar"}
_MOTOR_LIGA = {"o", "a", "pra", "para", "pro", "no", "na", "em", "de", "do", "da", "por", "favor", "agora", "ai", "fedora", "ok",
               "entao", "motor", "modo", "principal", "novamente", "so", "que", "e", "ele", "ela"}
_MOTOR_COTA = {"sem", "cota", "acabou", "esta", "ta", "estourou", "zerou"}
_MOTOR_PERGUNTA = {"qual", "que", "motor", "esta", "estao", "ta", "voce", "usando", "agora", "ai", "fedora", "e", "o", "ativo", "atual"}


def motor_local(message):
    """Comando de troca de motor falado/escrito ("troca o motor pro agy", "volta pro claude", "motor automatico"): so casa a
    frase INTEIRA feita de palavras da lista (nada de "usa o claude pra revisar isso"), entao conversa comum nunca cai aqui.
    Devolve ("claude"|"agy"|"auto", "troca"), ("", "status") ou None. Puro."""
    palavras = re.findall(r"[a-z0-9]+", atalhos.fold(message))
    if not palavras or len(palavras) > 9:
        return None
    alvos = [_MOTOR_ALVO[w] for w in palavras if w in _MOTOR_ALVO]
    if "cota" in palavras and alvos and set(alvos) == {"agy"} and ({"sem", "acabou"} & set(palavras)) and \
            set(w for w in palavras if w not in _MOTOR_ALVO) <= (_MOTOR_LIGA | _MOTOR_COTA):
        return "agy", "sem_cota"   # "o agy esta sem cota": bloqueia o agy na regra (motor_regra.bloquear_agy)
    if not alvos and "motor" in palavras and set(palavras) <= _MOTOR_PERGUNTA and ("qual" in palavras or "que" in palavras):
        return "", "status"
    if len(set(alvos)) != 1:
        return None
    resto = [w for w in palavras if w not in _MOTOR_ALVO]
    if not set(resto) <= (_MOTOR_VERBO | _MOTOR_LIGA):
        return None
    if "motor" not in palavras and not (set(resto) & _MOTOR_VERBO):   # so "agy" solto nao e comando
        return None
    if alvos[0] == "auto" and "motor" not in palavras and "modo" not in palavras:   # "usa automatico" sozinho e ambiguo
        return None
    return alvos[0], "troca"


def _motor_resposta(alvo, acao):
    import runtime
    if acao == "status":
        agora = runtime.motor_atual()
        return f"Estou no {agora}" + (" (fixo)." if runtime.motor_forcado() else " (automático).")
    if acao == "sem_cota":
        import time
        import motor_regra
        fim = motor_regra.bloquear_agy()   # sem reset conhecido do agy: padrao curto (BLOQUEIO_PADRAO)
        return f"Certo, o agy fica fora da regra até {time.strftime('%H:%M', time.localtime(fim))}; sigo no Claude."
    if alvo == "agy":
        import motor_agy
        if not motor_agy.instalado():
            return "O agy não está instalado, continuo no motor atual."
    runtime.definir_motor(alvo)
    nome = {"agy": "o agy", "claude": "o Claude", "auto": "o automático, com a regra de 55% e 90%"}[alvo]
    return f"Pronto, agora uso {nome}. A próxima fala já vai por ele."


def handle(message, runner=None, info=None):
    """info (optional dict): filled by atalhos.route for the turn log (atalho, filtrado, gemini...)."""
    m = _CMD.match(message or "")
    if not m:
        mt = motor_local(message)   # troca de motor: local e imediata, sem gastar um turno do modelo
        if mt:
            return dict(message=None, reply=_motor_resposta(*mt), note="comando")
        reply = atalhos.route(message, info)
        return dict(message=None, reply=reply, note="atalho") if reply else None
    cmd, arg = m.group(1).lower(), m.group(2).strip()
    d = load()

    if cmd in ("help", "commands"):
        return dict(message=None, reply=_help(), note="commands")

    if cmd == "clear":
        return dict(message=None, reply="Display cleared. Claude session unchanged.", note="clear")

    if cmd == "new":
        return dict(fresh=True, message=arg or "Greet me in one short JARVIS line.", note="session reset")

    if cmd == "status":
        import runtime
        return dict(message=None, note="status", reply=(
            f"Claude Code {runtime.version()} · model={runtime.MODEL or 'default'} · "
            f"permission={runtime.PERMISSION} · workdir={runtime.WORKDIR} · "
            f"vault={memory.build_graph()['total']} notes"))

    if cmd in ("graph", "vault"):
        g = memory.build_graph(force=True)
        return dict(message=None, note="vault reloaded", graph=True, reply=(
            f"Vault at {g['vault']} — {g['total']} notes, {len(g['links'])} links. "
            f"Top hub: {g['hubs'][0]['title']} ({g['hubs'][0]['degree']} connections)."))

    if cmd in ("recall", "memory"):
        if not arg:
            return dict(message=None, reply="Give me something to recall.", note="recall")
        hits = memory.search(arg)
        if not hits:
            return dict(message=None, reply=f"Nothing in the vault matches “{arg}”.", note="recall miss")
        body = "\n".join(f"· {h['title']}  [{h['type']}]\n  {h['snippet'][:110].rstrip()}…"
                         for h in hits)
        return dict(message=None, note=f"recall: {arg[:40]}", focus=hits[0]["title"],
                    reply=f"From the vault:\n{body}")

    if cmd == "profile":
        if not arg:
            return dict(message=None, note="profile read",
                        reply="\n".join(f"- {x}" for x in d["profile"][-20:]) or "Nothing saved yet.")
        update_state(lambda s: s["profile"].append(arg))
        return dict(message=f'Saved to profile: "{arg}". Acknowledge in one line.', note="profile saved")

    if cmd == "goal":
        if arg.lower() == "clear":
            update_state(lambda s: s.__setitem__("goal", ""))
            return dict(message=None, reply="Objective cleared.", note="goal cleared")
        if not arg or arg.lower() == "status":
            return dict(message=None, reply=d["goal"] or "No standing objective.", note="goal read")
        update_state(lambda s: s.__setitem__("goal", arg))
        return dict(message=f'Objective set: "{arg}". Acknowledge in one line.', note="goal set")

    if cmd == "personality":
        if not arg:
            return dict(message=None, reply=d["personality"] or "Default JARVIS persona.", note="tone read")
        update_state(lambda s: s.__setitem__("personality", arg))
        return dict(message=f'Tone is now: "{arg}". Reply in one short line using it.', note="tone set")

    if cmd in ("kanban", "mission", "missions"):
        if arg:
            update_state(lambda s: s["tasks"].append({"text": arg, "done": False, "at": time.time()}))
            return dict(message=f'Added to the queue: "{arg}". Confirm briefly.', note="mission added")
        open_t = [t["text"] for t in d["tasks"] if not t.get("done")]
        return dict(message=None, note="queue read",
                    reply=("Mission queue:\n" + "\n".join(f"- {t}" for t in open_t)) if open_t
                    else "Mission queue is empty.")
    return None
