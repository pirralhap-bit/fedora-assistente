"""
Claude Code runtime for the JARVIS memory HUD.

The brain is your own `claude` CLI, running headless (`claude -p`) with
`--output-format stream-json`. That means it authenticates the same way your
normal Claude Code sessions do — your subscription, no API key, no per-token
billing — and it keeps the same tools, MCP servers, skills and CLAUDE.md
context that the interactive CLI has.

Never pass --bare: it forces ANTHROPIC_API_KEY auth and would bypass the
subscription this whole dashboard is built around.
"""
import collections
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid

MODEL = os.environ.get("JARVIS_MODEL", "").strip()
WORKDIR = os.path.expanduser(os.environ.get("JARVIS_WORKDIR", os.getcwd()))
# bypassPermissions by default: a headless `claude -p` can't surface a permission
# prompt, so any gated tool (MCP connectors, web search) would silently fail
# otherwise. This also lets JARVIS take unattended actions (send/delete/shell) —
# see the "Segurança" section of README.md. Set JARVIS_PERMISSION=acceptEdits to narrow it.
PERMISSION = os.environ.get("JARVIS_PERMISSION", "bypassPermissions").strip()
# Deny rules still hold under bypassPermissions (tested). The mic is always open, so a
# misheard phrase must never power the PC off or delete for good (trash is undoable).
DENY = ["Bash(shutdown:*)", "Bash(poweroff:*)", "Bash(reboot:*)", "Bash(halt:*)",
        "Bash(systemctl poweroff:*)", "Bash(systemctl reboot:*)", "Bash(systemctl halt:*)",
        "Bash(systemctl suspend:*)", "Bash(systemctl hibernate:*)", "Bash(loginctl poweroff:*)",
        "Bash(loginctl reboot:*)", "Bash(rm:*)", "Bash(shred:*)",
        "Bash(mkfs:*)", "Bash(dd:*)",
        # Windows
        "Bash(shutdown.exe:*)", "Bash(Stop-Computer:*)", "Bash(Restart-Computer:*)", "Bash(rundll32:*)",
        "Bash(del:*)", "Bash(erase:*)", "Bash(rmdir:*)", "Bash(rd:*)", "Bash(Remove-Item:*)",
        "Bash(format:*)", "Bash(diskpart:*)", "Bash(cipher:*)", "Bash(powershell*Stop-Computer*)",
        "Bash(powershell*Restart-Computer*)", "Bash(powershell*Remove-Item*)", "Bash(cmd*shutdown*)"]
# Tools per mode (conversa.completo + server.py): light = LIGHT --tools (Bash, no PowerShell); equipe = light + Agent
# (subagents: Bash at most); full ("a fundo") and the typed chat pass no --tools, so Claude Code's default set is on,
# and on Windows that includes the PowerShell tool, which Bash(...) rules never match: hence the PowerShell(...) rules.
# Measured 2026-11-07 (claude 2.1.319, harmless commands only): PowerShell(x:*) and PowerShell(*x*) ignore case, check
# each subcommand ("a; x") and follow aliases (gci = Get-ChildItem); Bash(x:*) also catches x.exe and /full/path/x and
# each subcommand of "a && x", but is case-sensitive and misses "cmd //c x", so Bash gets *x* globs in several cases.
# Read(...) rules also stop Grep, cat and Get-Content, but not "cmd //c type": the secret globs below cover the rest.
# Every glob keeps the trash command of commands.PC_RULES (DeleteFile ... SendToRecycleBin) allowed: see the self-test.
# NOT covered (known, accepted): DENY is a brake against a misheard phrase, not a sandbox. Mixed case in Bash (ShUtDoWn),
# obfuscation (string concatenation, base64 outside -EncodedCommand, cmd ^-escapes), DeleteFile with a single argument
# (deletes for good by default) and taskkill/Stop-Process by PID on critical processes all get through.
# A dangerous word inside a URL or search text of a Bash command (e.g. "shutdown", ".env") is denied too: false positive on purpose.
def _casos(p):
    """Bash rules are case-sensitive (measured: *hostname* misses HOSTNAME.EXE); PowerShell ones are not."""
    return dict.fromkeys((p, p.lower(), p.upper(), p.title()))


# short commands: prefix only (a glob like *del* would hit "model"); the long names below are globs
_PREFIXO = ["shutdown", "poweroff", "reboot", "halt", "logoff", "rm", "ri", "del", "erase", "rd", "rmdir",
            "unlink", "shred", "format", "reg delete"]
_PROCESSOS = ("explorer", "csrss", "winlogon", "wininit", "lsass", "smss", "services", "svchost", "dwm")
_EM_QUALQUER_LUGAR = [
    "shutdown", "Stop-Computer", "Restart-Computer", "SetSuspendState", "rundll32", "logoff", "tsdiscon",
    "Win32_OperatingSystem*Reboot", "MethodName Reboot",
    "Remove-Item", "IO.File]::Delete", "IO.Directory]::Delete", "DeletePermanently", ".Delete(", "Clear-RecycleBin",
    "os.remove(", "os.unlink(", "os.rmdir(", "shutil.rmtree(", ".unlink(", ".rmdir(", "os.system(",
    "reg delete", "reg.exe delete", "wmic*delete", "vssadmin*delete", "wbadmin*delete", "sdelete",
    "bcdedit", "diskpart", "cipher /w", "cipher.exe /w", "format.com", "Format-Volume", "Clear-Disk", "Initialize-Disk", "Remove-Partition",
    "find*-delete", "git clean*-*f", "robocopy*/mir", "robocopy*/MIR", "robocopy*/purge", "robocopy*/PURGE",
    "-EncodedCommand", "/EncodedCommand",
    *[f"cmd*{q}{v} " for v in ("del", "erase", "rd", "rmdir", "format") for q in (" ", '"')],
]
# already in every casing they need (not run through _casos: the Windows command line has a 32k limit)
_FLAGS = ["ec"] + ["EncodedCommand"[:k] for k in range(1, 8)]   # powershell -e / -en / -enc ...: any prefix works
_SO_PS = ([f"*{sh}* -{f} *" for sh in ("powershell", "pwsh") for f in _FLAGS]
          + [p for n in _PROCESSOS for p in (f"*kill*{n}*", f"*Stop-Process*{n}*", f"*{n}*Stop-Process*",
                                             f"*spps*{n}*", f"*{n}*.Kill(*")])
_SO_BASH = ([f"*{sh}* -{c} *" for sh in ("powershell", "pwsh") for f in _FLAGS for c in _casos(f)]
            + [p for n in _PROCESSOS for c in _casos(n) for p in (f"*kill*{c}*", f"*KILL*{c}*", f"*Stop-Process*{c}*")])
# Secrets: this project's .env, the server token (server._token: %LOCALAPPDATA%\fedora-assistente\*.token) and any
# *.token. Read(**/x) only matches under the cwd; //** and ~/** reach the token outside it (measured).
_SEGREDOS = [f"Read({r}{f})" for f in (".env", "*.token") for r in ("**/", "~/**/", "//**/")]
# "*.env" at the end or before a separator: a bare *.env* would also hit [System.Environment] (PowerShell ignores case)
_SEGREDOS_GLOB = [f"*{ext}{fim}" for ext in (".env", ".token") for fim in ("", " *", "'*", '"*', ";*", ")*", "|*", ",*", ">*")]

DENY = list(dict.fromkeys(
    DENY
    + [f"Bash({c}:*)" for p in _PREFIXO for c in _casos(p)]
    + [f"Bash({c})" for p in [f"*{x}*" for x in _EM_QUALQUER_LUGAR] + _SEGREDOS_GLOB for c in _casos(p)]
    + [f"Bash({p})" for p in _SO_BASH]
    + [f"PowerShell({p}:*)" for p in _PREFIXO]
    + [f"PowerShell(*{p}*)" for p in _EM_QUALQUER_LUGAR]
    + [f"PowerShell({p})" for p in _SEGREDOS_GLOB + _SO_PS]
    + _SEGREDOS))
# Light mode (measured 2026-11-05: 26k -> 10.5k tokens of fixed context). ToolSearch must stay:
# without it the MCP tools (Gmail, Drive, Chrome) load in full instead of deferred (17k measured).
LIGHT = ["--disable-slash-commands",
         "--tools", "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,ToolSearch"]
# Team mode needs no extra tool: the agents (agentes/, independent of the engine) are called through the shell with
# `agentes.py <agente> "<tarefa>"`, so the same call works from the Claude engine and from the agy engine.
LIGHT_EQUIPE = LIGHT
LIMITS = {}   # last seen plan usage: {"five_hour": {"used": .29, "resets": epoch}, ...}
RUNTIME = os.environ.get("JARVIS_RUNTIME", "auto").strip().lower()   # auto|claude|mock
# Qual assinatura responde: auto (padrao: Claude, e o agy quando o Claude passa de 55% na semana ou chega a 90% na sessao
# de 5 h, ate aquela janela resetar; ver motor_regra.py), ou claude / agy para forcar um.
# agy = Antigravity/Google (motor_agy.py, mesma autonomia, mesmas travas DENY).
MOTOR = os.environ.get("FEDORA_MOTOR", "auto").strip().lower()
IDLE_TIMEOUT = int(os.environ.get("JARVIS_TIMEOUT", "180"))
# A tool still running (tool_use without its tool_result, e.g. the shell waiting for agentes.py) sends nothing on the
# stream: while one is pending the idle limit grows to this hard ceiling instead (never waits forever).
AGENTE_TIMEOUT = int(os.environ.get("FEDORA_AGENTE_TIMEOUT", "300"))


def limite_ocioso(ferramenta_pendente):
    """Seconds of silence allowed: IDLE_TIMEOUT, or the hard ceiling while a tool is still running."""
    return max(IDLE_TIMEOUT, AGENTE_TIMEOUT + 60) if ferramenta_pendente else IDLE_TIMEOUT
RAW_LOG = os.environ.get("JARVIS_RAW_LOG", "").strip()

# One Claude subprocess at a time: session state is shared, and the browser
# Escape key must be able to kill real backend work, not just the fetch.
EXECUTION_LOCK = threading.RLock()
_ACTIVE_PROC = None
_ACTIVE_LOCK = threading.Lock()
# Esc for the current turn: set by cancel_active, cleared by novo_turno (server.py, when a turn starts). Checked
# before Popen and in the read loop, so an Esc that arrives before the claude process exists still cancels it.
CANCEL = threading.Event()
CANCELLED = "Interrompido."

# Hook chatter and internal bookkeeping the HUD should never render.
_NOISE_SUBTYPES = {"hook_started", "hook_response", "post_turn_summary",
                   "compact_boundary", "mcp_status"}


def kill_tree(proc):
    """Kill the process AND everything it started. On Windows terminate()/kill() only stops claude.exe and leaves its
    Bash/PowerShell tool commands running. Each node's children are listed before it is killed, so a grandchild
    whose parent already died is still found."""
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is None:
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        fila = [psutil.Process(proc.pid)]
    except psutil.Error:
        return
    vistos = []
    while fila:
        p = fila.pop(0)
        if p in vistos:
            continue
        vistos.append(p)
        try:
            fila += p.children(recursive=True)
        except psutil.Error:
            pass
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(vistos, timeout=3)


def novo_turno():
    CANCEL.clear()


def cancel_active():
    """Esc: cancel the current turn (even before its process exists); True if a running process was stopped."""
    CANCEL.set()
    with _ACTIVE_LOCK:
        proc = _ACTIVE_PROC
    if proc is None or proc.poll() is not None:
        return False
    try:
        kill_tree(proc)
        return True
    except Exception:
        return False


def valid_session(sid):
    """Claude Code session ids are UUIDs."""
    try:
        return bool(sid) and str(uuid.UUID(str(sid))) == str(sid).lower()
    except (ValueError, AttributeError, TypeError):
        return False


def _claude_base():
    configured = os.environ.get("CLAUDE_CMD", "").strip()
    if configured:
        return shlex.split(configured)
    exe = shutil.which("claude")
    if exe:
        return [exe]
    for local in (os.path.expanduser("~/.local/bin/claude.exe"), os.path.expanduser("~/.local/bin/claude"),
                  os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd")):
        if os.path.exists(local):
            return [local]
    return ["claude"]


def _can_launch():
    try:
        p = subprocess.run(_claude_base() + ["--version"], cwd=WORKDIR, text=True, encoding="utf-8", errors="replace",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        return p.returncode == 0, (p.stdout or p.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def runtime_kind():
    if RUNTIME in ("mock", "claude"):
        return RUNTIME
    ok, _ = _can_launch()
    return "claude" if ok else "mock"


def version():
    ok, detail = _can_launch()
    return detail if ok else "unavailable"


MSG_ARGV_MAX = 6000   # pedido maior que isso vai pelo stdin: so a DENY ja gasta ~14 mil dos 32.767 caracteres da linha de comando do Windows


def build_command(message, session_id=None, system=None, full=True, via_stdin=False):
    cmd = _claude_base() + [
        "-p", *([] if via_stdin else [message]),   # sem texto depois do -p, o claude le o pedido do stdin
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--exclude-dynamic-system-prompt-sections",   # cwd/date/git out of the system prompt: steadier cache
        # the ponytail coding plugin injects ~1.6k tokens per session and asks for a "what I skipped" line: wrong for voice
        "--settings", json.dumps({"enabledPlugins": {"ponytail@ponytail": False}}),
    ]
    if full == "equipe":
        cmd += LIGHT_EQUIPE
    elif not full:
        cmd += LIGHT
    if valid_session(session_id):
        cmd += ["--resume", str(session_id)]
    if MODEL:
        cmd += ["--model", MODEL]
    if PERMISSION:
        cmd += ["--permission-mode", PERMISSION]
    cmd += ["--disallowedTools", *DENY]
    if system:
        cmd += ["--append-system-prompt", system]
    return cmd


def _short(value, limit=90):
    """Collapse a tool input dict into one readable line for the action log."""
    if isinstance(value, dict):
        for key in ("command", "query", "prompt", "file_path", "pattern", "path", "url"):
            if key in value and isinstance(value[key], str):
                value = value[key]
                break
        else:
            value = json.dumps(value)
    text = " ".join(str(value).split())
    return text[:limit] + ("…" if len(text) > limit else "")


AGENT_TOOLS = ("Task", "Agent")   # Claude Code's delegation tool (name differs between versions)
# "ESCALAR: motivo" at the start of a line is an agent asking for the level above; the word inside a sentence, a file
# name (junior.md) or a grep result is not (QA, lote E)
ESCALAR_RE = re.compile(r"(?m)^\s*ESCALAR\s*:")


def escalar_em(content):
    """True if a tool_result content (str, or a list of {"type": "text", "text": ...}) carries a real ESCALAR line."""
    if isinstance(content, list):
        content = "\n".join(str(c.get("text") or "") if isinstance(c, dict) else str(c) for c in content)
    return bool(ESCALAR_RE.search(str(content or "")))


SHELL_TOOLS = ("Bash", "PowerShell", "run_command")   # Claude Code's shell tools and the agy one
# `<python> <path>/agentes.py <agente> "<tarefa>"`: how any engine calls a team agent. The command must START with a
# Python (python, py, or a path ending in python.exe, quoted or not) followed by agentes.py, and the name must be followed
# by a space or the end ("git diff agentes.py runtime.py" and "...agentes.py qa.md" are not delegations).
_PY_RE = r"""(?i:"(?:[^"]*[\\/])?python3?(?:\.exe)?"|'(?:[^']*[\\/])?python3?(?:\.exe)?'|(?:[^\s"']*[\\/])?python3?(?:\.exe)?|py(?:\.exe)?)"""
_AGENTES_PY_RE = r"""(?:"(?:[^"]*[\\/])?agentes\.py"|'(?:[^']*[\\/])?agentes\.py'|(?:[^\s"']*[\\/])?agentes\.py)"""
AGENTE_CMD_RE = re.compile(rf"""\s*{_PY_RE}\s+{_AGENTES_PY_RE}\s+([a-z][a-z0-9_-]*)(?:\s+(.*))?\s*\Z""", re.S)
AGENTES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agentes")


def _agentes_conhecidos():
    """Names of the team agents (agentes/*.md). Listed here, not imported from agentes.py (runtime must not depend on it)."""
    try:
        return {f[:-3] for f in os.listdir(AGENTES_DIR) if f.endswith(".md")}
    except OSError:
        return set()


def delegacao_em(comando):
    """(agente, tarefa) if a shell command delegates to a team agent through agentes.py, else None."""
    m = AGENTE_CMD_RE.match(str(comando or ""))
    if not m or m.group(1) not in _agentes_conhecidos():
        return None
    return m.group(1), (m.group(2) or "").strip().strip("\"'")


def tool_event(block, parent=None):
    """Action-log event for a tool_use block; a delegation to a team agent also carries who and why (for the timeline)."""
    inp = block.get("input") or {}
    ev = dict(t="tool", phase="use", id=block.get("id"), parent=parent, name=block.get("name", "tool"), input=_short(inp))
    if ev["name"] in AGENT_TOOLS and isinstance(inp, dict):
        ev.update(agent=str(inp.get("subagent_type") or "agente"), desc=_short(inp.get("description") or inp.get("prompt") or "", 80))
    elif ev["name"] in SHELL_TOOLS and isinstance(inp, dict):
        d = delegacao_em(inp.get("command") or inp.get("CommandLine"))
        if d:
            ev.update(agent=d[0], desc=_short(d[1], 80))
    return ev


def run_claude(message, session_id=None, system=None, full=True):
    with EXECUTION_LOCK:
        yield from _run_locked(message, session_id, system, full)


def _run_locked(message, session_id=None, system=None, full=True):
    global _ACTIVE_PROC
    started = time.monotonic()
    via_stdin = len(message) > MSG_ARGV_MAX
    cmd = build_command(message, session_id, system, full, via_stdin)

    env = dict(os.environ)
    home = os.path.expanduser("~")
    extra = [os.path.join(home, ".local", "bin"), os.path.join(home, ".npm-global", "bin")]
    extra += [os.path.join(os.environ.get("APPDATA", ""), "npm")] if os.name == "nt" else         ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    env["PATH"] = os.pathsep.join(dict.fromkeys([env.get("PATH", ""), *extra]))

    if CANCEL.is_set():   # Esc before the process even started
        yield dict(t="error", message=CANCELLED, cancelled=True)
        return
    try:
        env["PYTHONUTF8"] = "1"
        env["FEDORA_MOTOR_TURNO"] = "claude"   # agents that exist on both subscriptions follow the engine of this turn
        proc = subprocess.Popen(cmd, cwd=WORKDIR, text=True, encoding="utf-8", errors="replace", bufsize=1,
                                stdin=subprocess.PIPE if via_stdin else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        with _ACTIVE_LOCK:
            _ACTIVE_PROC = proc
    except FileNotFoundError:
        raise RuntimeError("claude CLI not found. Install Claude Code or set CLAUDE_CMD.")

    q = collections.deque()
    lock = threading.Lock()
    done = threading.Event()
    errbuf = collections.deque(maxlen=120)
    raw = None
    if RAW_LOG:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(os.path.expanduser(RAW_LOG), flags, 0o600)
        raw = os.fdopen(fd, "w", encoding="utf-8", errors="replace")

    def pump_out():
        try:
            for ln in proc.stdout:
                if raw:
                    raw.write(ln); raw.flush()
                with lock:
                    q.append(ln)
        finally:
            done.set()

    def pump_err():
        try:
            for ln in proc.stderr:
                errbuf.append(ln.rstrip())
        except Exception:
            pass

    threading.Thread(target=pump_out, daemon=True).start()
    threading.Thread(target=pump_err, daemon=True).start()
    if via_stdin:   # com as threads de leitura ja rodando: um pedido grande nao trava num pipe cheio
        try:
            proc.stdin.write(message)
            proc.stdin.close()
        except OSError:
            pass   # o processo morreu: o erro real sai do stderr/rc la embaixo

    emitted_session = session_id
    last_usage = {}
    streamed_text = False       # partial deltas arrived -> ignore final text blocks
    text_seen = False
    finished = False
    first = True
    last = time.monotonic()
    pendentes = set()   # tool_use ids still waiting for their tool_result

    try:
        while not done.is_set() or q:
            if CANCEL.is_set():   # Esc between Popen and the registration above, or while reading
                kill_tree(proc)
                yield dict(t="error", message=CANCELLED, cancelled=True)
                return
            line = None
            with lock:
                if q:
                    line = q.popleft()
            if line is None:
                if proc.poll() is not None and done.is_set():
                    break
                limite = limite_ocioso(bool(pendentes))
                if time.monotonic() - last > limite:
                    kill_tree(proc)
                    yield dict(t="error", message=(
                        f"Claude went quiet for {limite}s and was stopped. "
                        + " | ".join(list(errbuf)[-3:]))[:400])
                    return
                time.sleep(0.04)
                continue
            last = time.monotonic()
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            kind = ev.get("type")
            sid = ev.get("session_id")
            if valid_session(sid):
                emitted_session = sid

            if kind == "system":
                if ev.get("subtype") == "init":
                    yield dict(t="status", model=ev.get("model") or MODEL or "Claude default",
                               tools=len(ev.get("tools") or []),
                               mcp=[s.get("name") for s in (ev.get("mcp_servers") or [])],
                               permission=ev.get("permissionMode") or PERMISSION,
                               session_id=sid, runtime="claude")
                continue

            if kind == "rate_limit_event":
                info = ev.get("rate_limit_info") or {}
                # official plan usage (0..1) per window; the HUD reads it from /api/system
                for name, w in (info.get("unifiedWindows") or {}).items():
                    LIMITS[name] = dict(used=w.get("utilization"), resets=w.get("resetsAt"))
                yield dict(t="ratelimit", status=info.get("status"),
                           window=info.get("rateLimitType"), resets=info.get("resetsAt"))
                continue

            if kind == "stream_event":
                inner = ev.get("event") or {}
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        if first:
                            first = False
                            yield dict(t="latency", ms=int((time.monotonic() - started) * 1000))
                        streamed_text = True
                        text_seen = True
                        yield dict(t="delta", text=delta["text"])
                continue

            if kind == "assistant":
                u = (ev.get("message") or {}).get("usage") or {}
                if u:
                    last_usage = u
                for block in (ev.get("message") or {}).get("content") or []:
                    btype = block.get("type")
                    if btype == "text" and not streamed_text and block.get("text"):
                        if first:
                            first = False
                            yield dict(t="latency", ms=int((time.monotonic() - started) * 1000))
                        text_seen = True
                        yield dict(t="delta", text=block["text"])
                    elif btype == "tool_use":
                        if block.get("id"):
                            pendentes.add(block["id"])
                        yield tool_event(block, ev.get("parent_tool_use_id"))
                continue

            if kind == "user":
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_result":
                        pendentes.discard(block.get("tool_use_id"))
                        res = dict(t="tool", phase="result", id=block.get("tool_use_id"),
                                   parent=ev.get("parent_tool_use_id"),
                                   ok=not block.get("is_error"),
                                   name=block.get("name", ""))
                        if escalar_em(block.get("content")):   # an agent asked for the level above (turnolog)
                            res["escalar"] = True
                        yield res
                continue

            if kind == "result":
                usage = ev.get("usage") or last_usage or {}
                if ev.get("subtype") != "success" and not text_seen:
                    yield dict(t="error", message=str(ev.get("result") or "Claude returned an error")[:400])
                    return
                if not text_seen and ev.get("result"):
                    yield dict(t="delta", text=str(ev["result"]))
                    text_seen = True
                # whole context the turn cost, cache included; the HUD shows it as "última pergunta"
                LIMITS["last"] = dict(total=sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                      "cache_creation_input_tokens", "output_tokens")), out=usage.get("output_tokens", 0))
                yield dict(t="usage",
                           input_tokens=usage.get("input_tokens", 0),
                           output_tokens=usage.get("output_tokens", 0),
                           total_tokens=(usage.get("input_tokens", 0) + usage.get("output_tokens", 0)))
                finished = True
                yield dict(t="complete", session_id=emitted_session,
                           ms=ev.get("duration_ms") or int((time.monotonic() - started) * 1000))
                return

        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # stdout closed but the process lingers (cleanup/hooks); don't fail a finished answer
            kill_tree(proc)
            rc = 0 if text_seen else -1
        if CANCEL.is_set():   # Esc killed it after the loop's last check: not "claude exited with code 15"
            yield dict(t="error", message=CANCELLED, cancelled=True)
            return
        if rc != 0:
            yield dict(t="error", message=(" | ".join(list(errbuf)[-6:])
                                           or f"claude exited with code {rc}")[:500])
            return
        if not text_seen:
            yield dict(t="delta", text="Claude completed without textual output.")
        yield dict(t="complete", session_id=emitted_session,
                   ms=int((time.monotonic() - started) * 1000))
    finally:
        try:
            if raw:
                raw.close()
        except Exception:
            pass
        if proc.poll() is None and finished:
            try:   # a finished answer: claude exits by itself in a moment; an app it opened must survive that
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is None:
            kill_tree(proc)
        with _ACTIVE_LOCK:
            if _ACTIVE_PROC is proc:
                _ACTIVE_PROC = None


def run_mock(message, session_id=None, system=None):
    ok, detail = _can_launch()
    yield dict(t="error", message=("Claude Code is not reachable from this process. "
                                   "Install it, or set CLAUDE_CMD to the executable. "
                                   f"Diagnostic: {detail[:200]}"))


def motor_atual(motor=None):
    """Motor deste turno: 'claude' ou 'agy'. 'auto' aplica a regra de uso do Claude (semana > 55% ou sessao de 5 h >= 90%,
    motor_regra); qualquer outro valor cai no Claude (nunca num motor desconhecido)."""
    motor = (motor or MOTOR or "claude").strip().lower()
    if motor == "auto":
        import motor_regra
        return motor_regra.decidir(LIMITS)
    return motor if motor in ("claude", "agy") else "claude"


def run(message, session_id=None, system=None, full=True, motor=None):
    if motor_atual(motor) == "agy":
        try:
            import motor_agy
            yield from motor_agy.run(message, session_id, system, full)
        except Exception as e:  # noqa: BLE001
            yield dict(t="error", message=f"could not start agy core: {e}")
        return
    if runtime_kind() != "claude":
        yield from run_mock(message, session_id, system)
        return
    try:
        yield from run_claude(message, session_id, system, full)
    except Exception as e:  # noqa: BLE001
        yield dict(t="error", message=f"could not start Claude core: {e}")


if __name__ == "__main__":
    e = tool_event(dict(id="t1", name="Task", input=dict(subagent_type="junior", description="ajustar README", prompt="x")))
    assert e["agent"] == "junior" and e["desc"] == "ajustar README" and e["id"] == "t1" and e["parent"] is None, e
    e = tool_event(dict(id="t2", name="Read", input=dict(file_path="a.py")), parent="t1")
    assert "agent" not in e and e["parent"] == "t1" and e["input"] == "a.py", e
    assert tool_event(dict(name="Agent", input={}))["agent"] == "agente"
    # delegation through the shell (any engine): agent and task show up in the timeline; "listar"/--teste/plain shell do not
    e = tool_event(dict(id="s1", name="Bash", input=dict(command='"C:/Users/Pedro/p/.venv/Scripts/python.exe" "C:/Users/Pedro/p/agentes.py" flash "ajuste o README"')))
    assert e["agent"] == "flash" and e["desc"] == "ajuste o README", e
    e = tool_event(dict(id="s2", name="run_command", input=dict(CommandLine="python agentes.py qa -")))
    assert e["agent"] == "qa", e
    for nao in ("python agentes.py listar", "python agentes.py --teste", "git status", "python agentes.py", "cat docs/agentes.py.md",
                "git diff agentes.py runtime.py", "git add agentes.py docs", "python agentes.py runtime.py", "python agentes.py qa.md x",
                "python agentes.py inexistente x", "echo python agentes.py qa x", "cat agentes.py qa", "pythonx agentes.py qa x",
                "python meuagentes.py qa x", "python agentes.pyc qa x"):
        assert "agent" not in tool_event(dict(name="Bash", input=dict(command=nao))), nao
        assert delegacao_em(nao) is None, nao
    for sim, esperado in (('"C:/Users/Pedro/p/.venv/Scripts/python.exe" "C:/Users/Pedro/p/agentes.py" qa "revise"', ("qa", "revise")),
                          (r'"C:\Users\Pedro\p\.venv\Scripts\python.exe" "C:\Users\Pedro\p\agentes.py" senior "x y"', ("senior", "x y")),
                          (r".venv\Scripts\python.exe agentes.py junior 'ajuste'", ("junior", "ajuste")),
                          ("'/c/Program Files/Python/python.exe' '/c/My Docs/agentes.py' flash ajuste", ("flash", "ajuste")),
                          ("py agentes.py argon -", ("argon", "-")), ("python ./agentes.py qa -", ("qa", "-")),
                          ("Python.EXE agentes.py qa", ("qa", "")), ("  python agentes.py qa  x\n", ("qa", "x"))):
        assert delegacao_em(sim) == esperado, (sim, delegacao_em(sim))
    assert motor_atual("agy") == "agy" and motor_atual(" AGY ") == "agy" and motor_atual("claude") == "claude"
    assert motor_atual("gpt") == "claude" and motor_atual("") == motor_atual(None)   # desconhecido cai no Claude, nunca num motor solto
    assert escalar_em("ESCALAR: mexe em DENY") and escalar_em([{"type": "text", "text": "Feito.\n  ESCALAR : precisa do senior"}])
    for falso in ("leia .claude/agents/junior.md (responda ESCALAR se não souber)", "grep ESCALAR docs/EQUIPE.md",
                  "docs/EQUIPE.md:11:Se um agente responder ESCALAR, repasse", [{"type": "text", "text": "sem ESCALAR aqui"}], None):
        assert not escalar_em(falso), falso

    # DENY: a local approximation of the CLI matcher (glob * over the whole command, x:* = first word of a
    # subcommand; PowerShell ignores case). The real CLI was checked with harmless commands (see the DENY notes).
    import re

    def negado(tool, command):
        for rule in DENY:
            m = re.fullmatch(r"(\w+)\((.*)\)", rule, re.S)
            if not m or m.group(1) != tool:
                continue
            flags = re.I if tool == "PowerShell" else 0
            pat = m.group(2)
            if pat.endswith(":*"):
                partes = re.split(r"\s*(?:;|&&|\|\||\|)\s*", command.strip())
                if any(re.match(re.escape(pat[:-2]) + r"(\.exe)?(\s|$)", p, flags) for p in partes):
                    return rule
            elif re.fullmatch(".*".join(map(re.escape, pat.split("*"))), command, flags | re.S):
                return rule
        return None

    for rule in ("PowerShell(*Remove-Item*)", "PowerShell(ri:*)", "PowerShell(rm:*)", "PowerShell(*shutdown*)", "PowerShell(del:*)",
                 "PowerShell(*Stop-Computer*)", "PowerShell(*IO.File]::Delete*)", "PowerShell(*IO.Directory]::Delete*)",
                 "PowerShell(*git clean*-*f*)", "PowerShell(*robocopy*/mir*)", "PowerShell(*kill*explorer*)",
                 "PowerShell(*.env)", "Bash(*shutdown*)", "Bash(*SHUTDOWN*)", "Bash(*cmd* del *)", "Bash(*pwsh* -enc *)",
                 "Bash(*-EncodedCommand*)", "Bash(*shutil.rmtree(*)", "Bash(*os.system(*)", "Bash(RM:*)",
                 "Read(//**/*.token)", "Read(**/.env)", "Read(~/**/.env)"):
        assert rule in DENY, rule
    import commands
    lixeira = re.search(r'powershell -NoProfile -Command "(.+?)"; para pasta', commands.PC_RULES).group(1)
    assert "SendToRecycleBin" in lixeira, lixeira
    for alvo in (r"C:\Users\Pedro\Documents\lista de compras.txt", r"C:\Users\Pedro\Downloads\foto 1.png"):
        uma = lixeira.replace("<caminho>", alvo)
        pasta = uma.replace("DeleteFile", "DeleteDirectory")
        for tool, cmdline in (("PowerShell", uma), ("PowerShell", pasta),
                              ("Bash", f'powershell -NoProfile -Command "{uma}"'), ("Bash", f'powershell -NoProfile -Command "{pasta}"')):
            assert not negado(tool, cmdline), (cmdline, negado(tool, cmdline))
    for tool, cmdline in (("Bash", "git diff"), ("Bash", "git status"), ("PowerShell", "dir"), ("Bash", "cmd //c dir"),
                          ("PowerShell", "Get-Content README.md"), ("PowerShell", "Get-Date -Format 'HH:mm'"),
                          ("PowerShell", "Get-Process | Format-Table"), ("PowerShell", "[System.Environment]::GetFolderPath('Desktop')"),
                          ("Bash", "taskkill //IM chrome.exe"), ("Bash", 'cmd //c start "" notepad'), ("Bash", "explorer C:\\Users\\Pedro"),
                          ("PowerShell", "Start-Process explorer C:\\Users\\Pedro"), ("Bash", "cat .env.example"),
                          ("PowerShell", "Get-Content .env.example"), ("Bash", "ls .venv")):
        assert not negado(tool, cmdline), (cmdline, negado(tool, cmdline))
    for tool, cmdline in (("Bash", "shutdown /s /t 0"), ("Bash", "SHUTDOWN.EXE /s"), ("Bash", "/c/Windows/System32/shutdown.exe -s"),
                          ("Bash", "cmd //c del x.txt"), ("Bash", 'cmd /c "rd /s /q x"'), ("Bash", "powershell -enc ZQBjAGgAbwA="),
                          ("Bash", "pwsh -EncodedCommand ZQBj"), ("Bash", 'powershell -Command "Remove-Item x"'),
                          ("Bash", "python -c \"import shutil; shutil.rmtree('x')\""), ("Bash", "py -c \"import os; os.remove('x')\""),
                          ("Bash", "robocopy a b /MIR"), ("Bash", "taskkill //F //IM explorer.exe"), ("Bash", "git clean -fdx"),
                          ("Bash", "cat .env"), ("Bash", "cmd //c type C:\\Users\\Pedro\\AppData\\Local\\fedora-assistente\\fedora-assistente.token"),
                          ("Bash", "echo a && rm -rf x"), ("PowerShell", "Write-Output 1; ri x"), ("PowerShell", "del x"),
                          ("PowerShell", "Restart-Computer"), ("PowerShell", "(Get-WmiObject Win32_OperatingSystem).Win32Shutdown(1)"),
                          ("PowerShell", "[System.IO.File]::Delete('x')"), ("PowerShell", "[IO.Directory]::Delete('x', $true)"),
                          ("PowerShell", "Get-Process explorer | Stop-Process"), ("PowerShell", "cmd /c del x"),
                          ("PowerShell", "Get-Content $env:LOCALAPPDATA\\fedora-assistente\\fedora-assistente.token"),
                          ("PowerShell", "Clear-RecycleBin -Force"), ("PowerShell", "bcdedit /set x"),
                          ("PowerShell", "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile('x','OnlyErrorDialogs','DeletePermanently')")):
        assert negado(tool, cmdline), cmdline
    assert len(subprocess.list2cmdline(build_command("x" * 4000, system="y" * 4000))) < 28000   # Windows: 32k max

    # cancel / timeout / early close: a fake claude (prints init, starts a sleeping "tool" grandchild, sleeps)
    # must die WITH that grandchild (terminate() alone left it running on Windows)
    import sys
    import tempfile
    import psutil
    tmp = tempfile.mkdtemp()
    fake, pidf = os.path.join(tmp, "fake_claude.py"), os.path.join(tmp, "neto.pid")
    with open(fake, "w", encoding="utf-8") as fh:
        fh.write("import json, subprocess, sys, time\n"
                 "neto = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                 f"open({pidf!r}, 'w').write(str(neto.pid))\n"
                 "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': None}), flush=True)\n"
                 "time.sleep(60)\n")
    os.environ["CLAUDE_CMD"] = f'"{sys.executable}" "{fake}"'.replace("\\", "/")

    def neto_morto():
        pid = int(open(pidf).read())
        for _ in range(50):
            if not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
            time.sleep(0.1)
        psutil.Process(pid).kill()
        return False

    def evs(gen, ao_iniciar=None):
        out = []
        for ev in gen:
            out.append(ev)
            if ev.get("t") == "status" and ao_iniciar:
                ao_iniciar()
        return out

    novo_turno()
    t0 = time.monotonic()
    out = evs(run_claude("x"), lambda: threading.Timer(0.3, cancel_active).start())   # Esc while it runs
    assert out[-1] == dict(t="error", message=CANCELLED, cancelled=True) and time.monotonic() - t0 < 15, out
    assert neto_morto(), "cancel_active left the grandchild running"
    assert _ACTIVE_PROC is None

    novo_turno()
    IDLE_TIMEOUT = 1
    out = evs(run_claude("x"))
    assert out[-1]["t"] == "error" and "quiet" in out[-1]["message"], out
    assert neto_morto(), "IDLE_TIMEOUT left the grandchild running"

    # a tool still running (tool_use without tool_result) pauses the idle count up to the hard ceiling
    assert limite_ocioso(False) == 1 and limite_ocioso(True) == max(1, AGENTE_TIMEOUT + 60)
    AGENTE_TIMEOUT = -57   # ceiling = max(IDLE_TIMEOUT=1, 3) = 3 s
    lenta = os.path.join(tmp, "fake_tool.py")
    uso = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "x"}}]}}
    resultado = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "ok"}]}}
    fim = {"type": "result", "subtype": "success", "result": "feito", "usage": {}}
    with open(lenta, "w", encoding="utf-8") as fh:
        fh.write("import json, sys, time\n"
                 f"print(json.dumps({uso!r}), flush=True)\n"
                 "time.sleep(float(sys.argv[-1]))\n"
                 f"print(json.dumps({resultado!r}), flush=True)\n"
                 "time.sleep(float(sys.argv[-2]))\n"
                 f"print(json.dumps({fim!r}), flush=True)\n")
    real_build = build_command
    try:
        for pausa_tool, pausa_depois, ok_esperado in (("2", "0", True), ("10", "0", False), ("0", "2", False)):
            build_command = lambda *a, **k: [sys.executable, lenta, pausa_depois, pausa_tool]   # noqa: E731
            t0 = time.monotonic()
            out = list(run_claude("x"))
            if ok_esperado:   # 2 s quiet with the tool running: over IDLE_TIMEOUT (1 s) but under the ceiling (3 s)
                assert out[-1]["t"] == "complete" and any(e.get("t") == "delta" and e["text"] == "feito" for e in out), out
            elif pausa_tool == "10":   # tool never answers: stopped at the ceiling, not at 10 s
                assert out[-1]["t"] == "error" and "quiet for 3s" in out[-1]["message"] and time.monotonic() - t0 < 8, out
            else:   # tool done, then quiet: the normal IDLE_TIMEOUT is back
                assert out[-1]["t"] == "error" and "quiet for 1s" in out[-1]["message"], out
    finally:
        build_command = real_build
    AGENTE_TIMEOUT = 300
    IDLE_TIMEOUT = 180

    gen = run_claude("x")
    assert next(gen)["t"] == "status"
    gen.close()   # the page went away mid-turn: the finally of _run_locked
    assert neto_morto(), "the finally of _run_locked left the grandchild running"

    cancel_active()   # Esc that arrives before Popen: no process is started at all
    real_popen, subprocess.Popen = subprocess.Popen, None
    try:
        assert list(run_claude("x")) == [dict(t="error", message=CANCELLED, cancelled=True)]
    finally:
        subprocess.Popen = real_popen
    novo_turno()
    assert not CANCEL.is_set()
    print("runtime ok")
