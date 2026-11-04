"""
Command matrix for Fedora: slash commands and shortcuts answer locally; everything else
goes to Claude Code with vault context attached.
"""
import json
import os
import pathlib
import re
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


def context_block(message=""):
    """Dashboard state + the vault notes relevant to this turn."""
    d = load()
    # saves a tool round trip (date) on "que horas são?" and lets "amanhã"/"sexta" resolve without one
    out = [time.strftime("## Agora\n%A, %d/%m/%Y %H:%M (horário local)")]
    tok = _tokens_block()
    if tok:
        out.append(tok)
    if d["profile"]:
        out.append("## Operator profile\n" + "\n".join(f"- {x}" for x in d["profile"][-40:]))
    if d["goal"]:
        out.append(f"## Standing objective\n{d['goal']}")
    if d["personality"]:
        out.append(f"## Tone overlay\n{d['personality']}")
    open_tasks = [t for t in d["tasks"] if not t.get("done")]
    if open_tasks:
        out.append("## Mission queue\n" + "\n".join(f"- {t['text']}" for t in open_tasks[:20]))
    out.append(VOICE_DIRECTION)
    recall = memory.context_for(message)
    if recall:
        out.append(recall)
    return "\n\n".join(out)


VOICE_DIRECTION = """## Como responder
Você é a Fedora, assistente de voz do usuário no PC dele (Fedora Linux, KDE Plasma, Wayland), em português do Brasil.
Responda como uma assistente de voz tipo Alexa: simpática, natural, resposta direta primeiro, uma ou duas frases.
Pergunta de conhecimento geral: responda do que você sabe. Coisa atual (clima, notícia, preço, placar): pesquise na web.
A resposta é lida em voz alta: sem markdown, sem listas, sem emoji, sem caminhos longos nem comandos na fala.

## Você controla o PC
Você tem acesso total ao computador do usuário e deve FAZER o que ele pede, não explicar como fazer:
abrir app (gtk-launch <id.desktop> ou o comando do app, em segundo plano com setsid), fechar app (pkill, ou kill pelo pid),
abrir pasta (xdg-open), criar e escrever arquivos, buscar arquivos, pesquisar na web.
Ao terminar, confirme em poucas palavras ("Pronto, abri o Chrome.").
Regras fixas:
- Apagar é SEMPRE mandar para a Lixeira com gio trash, nunca rm. Antes de apagar, diga o que vai apagar e pergunte "confirma?"; só apague depois do sim.
- Antes de fechar um app que pode ter trabalho não salvo (editor, documento, jogo), pergunte "confirma?".
- "Descarrega o modelo": ollama stop qwen3:8b. "Desliga o Ollama": systemctl --user stop ollama (volta sozinho quando a janela da Fedora abrir de novo).
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


def handle(message, runner=None):
    m = _CMD.match(message or "")
    if not m:
        reply = atalhos.route(message)
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
