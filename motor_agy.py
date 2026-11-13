"""
Motor Antigravity (`agy`) da Fedora: a segunda cota (assinatura Google) com o mesmo nivel de autonomia do Claude.

`runtime.run(..., motor="agy")` chega aqui. Este modulo roda `agy` headless (`--output-format stream-json`) e devolve os
MESMOS eventos que o runtime do Claude (status, delta, tool, usage, complete, error), entao server.py, UI, turnolog e retro
nao precisam saber qual motor respondeu.

Seguranca (a parte que importa): o agy NAO tem `--disallowedTools`. As travas vem do `permissions.deny` do settings.json
dele, que vale em todos os modos (documentado e medido: deny vence allow). `regras_deny()` traduz a lista `runtime.DENY`
(a mesma do Claude, fonte unica) para esse formato e `escreve_settings()` grava o arquivo passado em `--settings`.
Se a traducao falhar ou sair vazia, o motor NAO inicia (falha fechada): nunca roda em bypass sem as travas.
Limite conhecido, igual ao do Claude: DENY e freio contra frase mal ouvida, nao sandbox.

Diferencas em relacao ao Claude, todas medidas em 2026-11-09 (agy 1.5.3):
- sem `--append-system-prompt`: o bloco de sistema vai no inicio da mensagem (por stdin, sem o limite de 32k do Windows);
- sem `--tools`/modo leve: o agy sempre carrega o conjunto dele (~11k tokens); no modo leve so desliga skills/slash
  e nao manda o resumo do projeto (com_contexto so quando full nao e False);
- sem a ferramenta `Agent`: a equipe (agentes/, agentes.py) e chamada pelo shell, igual no Claude, entao "equipe" funciona nos dois;
- sem janelas de cota (rate_limit_event): so contamos tokens por turno (STATS), para o HUD; a regra de troca de motor
  (motor_regra.py) olha so o uso do Claude.
"""
import collections
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import time

import runtime

MODELO = os.environ.get("AGY_MODEL", "gemini-3.8-flash-medium").strip()               # modo leve (voz, rapido)
MODELO_COMPLETO = os.environ.get("AGY_MODEL_COMPLETO", "gemini-4-argon-medium").strip()  # "a fundo" / equipe / chat
# bypass = --dangerously-skip-permissions (mesma autonomia do Claude; as regras deny continuam valendo)
# auto = --mode auto (revisao do proprio agy; em headless nao executa comandos que pedem aprovacao)
# plan = so le e planeja
PERMISSAO = os.environ.get("AGY_PERMISSION", "bypass").strip().lower()
STATS = {"turnos": 0, "tokens": 0, "ultimo": 0}   # uso do agy nesta execucao (so informativo: nao entra na regra de troca)

_LOCAL = pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home() / ".local" / "state") / "fedora-assistente"
SETTINGS = pathlib.Path(os.environ.get("AGY_SETTINGS") or _LOCAL / "agy-settings.json")


# ---------------------------------------------------------------- executavel

def _agy_base():
    configurado = os.environ.get("AGY_CMD", "").strip()
    if configurado:
        import shlex
        return shlex.split(configurado)
    exe = shutil.which("agy")
    if exe:
        return [exe]
    for local in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "agy", "bin", "agy.exe"),
                  os.path.expanduser("~/.local/bin/agy")):
        if os.path.exists(local):
            return [local]
    return ["agy"]


def recusa_script_cmd(base):
    """Erro (str) se o executavel resolve para .cmd/.bat: o cmd.exe reinterpreta & e " do argv (a tarefa iria parar no
    shell). None se e um executavel de verdade (.exe ou interpretador)."""
    exe = (shutil.which(base[0]) or base[0]) if base else ""
    if str(exe).lower().endswith((".cmd", ".bat")):
        return f"{exe} e um script .cmd/.bat (o cmd.exe interpretaria a tarefa); aponte para o .exe (AGY_CMD/CLAUDE_CMD)"
    return None


def instalado():
    """O executavel do agy existe (barato, sem subir processo): a regra de troca so escolhe o agy se ele estiver aqui."""
    base = _agy_base()
    return bool(base) and (shutil.which(base[0]) is not None or os.path.exists(base[0]))


def disponivel():
    """(ok, versao ou erro): o agy esta instalado e roda."""
    try:
        p = subprocess.run(_agy_base() + ["--version"], cwd=runtime.WORKDIR, text=True, encoding="utf-8", errors="replace",
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        return p.returncode == 0, (p.stdout or p.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return False, str(e)


# ---------------------------------------------------------------- DENY do Claude -> permissions.deny do agy

def _esc(s):
    """Escapa so o que e especial em regex; sem `\\ ` nem `\\_` (o RE2 do agy recusa letras/_ escapados)."""
    return re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", s)


def _glob_rx(pat):
    return ".*".join(_esc(p) for p in pat.split("*"))


def _rx_prefixo(p):
    """`p` como primeira palavra de qualquer subcomando (a;b, a&&b, a|b), com .exe e caminho completo opcionais."""
    caminho = r"([^\s;&|]*[\\/])?" if not re.search(r"[\s\\/]", p) else ""
    return rf"(?i)(.*(;|&&|\|\||\|)\s*)?{caminho}{_esc(p)}(\.exe)?(\s.*)?"


def _norm_caminho(p):
    """Como o agy normaliza no Windows: sem letra de unidade e com /."""
    p = str(p).replace("\\", "/")
    return re.sub(r"^[A-Za-z]:", "", p)


def regras_deny():
    """runtime.DENY (formato Claude) -> lista `permissions.deny` do agy. Retorna (regras, contagem por tipo)."""
    comandos, leitura = [], []
    for rule in runtime.DENY:
        m = re.fullmatch(r"(\w+)\((.*)\)", rule, re.S)
        if not m:
            continue
        tool, pat = m.groups()
        if tool in ("Bash", "PowerShell"):
            rx = _rx_prefixo(pat[:-2]) if pat.endswith(":*") else "(?i)" + _glob_rx(pat)
            comandos.append(f"command(regex:{rx})")
        elif tool == "Read":
            leitura.append(pat)
    out = list(dict.fromkeys(comandos))
    # segredos: .env do projeto e a pasta do token do servidor (deny de leitura implica deny de escrita no agy)
    if any(p.endswith(".env") for p in leitura):
        out += ["read_file(.env)", "write_file(.env)"]
    if any(p.endswith(".token") for p in leitura):
        d = _norm_caminho(_LOCAL)
        out += [f"read_file({d})", f"write_file({d})"]
    return list(dict.fromkeys(out)), {"comandos": len(comandos), "unicas": len(out)}


def traducao_valida(regras, n):
    """A traducao do DENY tem as travas minimas: >= 50 regras de comando e o bloqueio de leitura do .env e do token.
    Fonte unica: o motor (escreve_settings) e os agentes (agentes.settings_agy) usam esta conferencia."""
    return (n.get("comandos", 0) >= 50 and "read_file(.env)" in regras
            and f"read_file({_norm_caminho(_LOCAL)})" in regras)


def escreve_settings(caminho=None):
    """Grava o settings.json do agy so com `permissions.deny`. Falha fechada: sem as travas minimas, levanta erro."""
    regras, n = regras_deny()
    if not traducao_valida(regras, n):
        raise RuntimeError(f"traducao do DENY para o agy saiu incompleta ({n}); motor agy nao inicia sem as travas")
    caminho = pathlib.Path(caminho or SETTINGS)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    corpo = json.dumps({"permissions": {"deny": regras}}, ensure_ascii=False, indent=1)
    tmp = caminho.with_suffix(".tmp")
    tmp.write_text(corpo, encoding="utf-8")
    os.replace(tmp, caminho)
    return caminho


# ---------------------------------------------------------------- comando e eventos

def monta_comando(session_id=None, full=True, settings=None):
    cmd = _agy_base() + ["--input-format", "stream-json", "--output-format", "stream-json",
                         "--model", (MODELO_COMPLETO if full else MODELO) or MODELO,
                         "--settings", str(settings or SETTINGS)]
    if PERMISSAO == "bypass":
        cmd += ["--dangerously-skip-permissions"]
    elif PERMISSAO in ("auto", "plan"):
        cmd += ["--mode", PERMISSAO]
    if not full:
        cmd += ["--disable-slash-commands"]
    if runtime.valid_session(session_id):
        cmd += ["--conversation", str(session_id)]
    return cmd


def monta_entrada(message, system=None):
    """A linha NDJSON do stdin. Sem flag de system prompt no agy: o bloco de sistema abre a mensagem."""
    texto = message if not system else f"<instrucoes_do_sistema>\n{system}\n</instrucoes_do_sistema>\n\n{message}"
    return json.dumps({"event": "user", "message": {"content": texto}}, ensure_ascii=False) + "\n"


_CHAVES = {"CommandLine": "command", "AbsolutePath": "file_path", "TargetFile": "file_path", "Path": "path",
           "Query": "query", "Url": "url", "URL": "url", "Pattern": "pattern"}


def _chaves(parametros):
    """Parametros de ferramenta do agy com os nomes que o log de acoes (runtime._short) ja sabe mostrar."""
    if not isinstance(parametros, dict):
        return parametros
    return {_CHAVES.get(k, k): v for k, v in parametros.items()}


def com_contexto(system, session_id=None):
    """Cache manual (contexto.py): o resumo pronto do projeto abre o bloco de sistema da PRIMEIRA mensagem de uma sessão nova.
    Sessão retomada (--conversation) já o tem no histórico: não repete. FEDORA_CONTEXTO_AGY=0 desliga (para comparar)."""
    if runtime.valid_session(session_id):
        return system
    try:
        import contexto
        ctx = contexto.bloco()
    except Exception:  # noqa: BLE001
        return system
    return f"{ctx}\n\n{system}" if ctx and system else (ctx or system)


# Erro de cota do agy (ele nao informa janela de cota): casa -> motor_regra.bloquear_agy e o runtime refaz no Claude
COTA_RE = re.compile(r"quota|resource[_ ]?exhausted|rate[_ ]?limit|\b429\b|usage limit|\bcota\b|out of credits", re.I)


# stderr traz log solto (nomes de flag, "rate_limit" em config): so frases claras de cota estourada contam
COTA_STDERR_RE = re.compile(r"resource[_ ]?exhausted|quota (?:exceeded|exhausted)|exceeded (?:your |the )?quota|"
                            r"\b429\b.{0,40}too many requests|rate[_ ]?limit(?:ed| exceeded| reached)|"
                            r"usage limit (?:reached|exceeded)|out of credits", re.I)


def erro_cota(texto, estrito=False):
    return bool(texto) and bool((COTA_STDERR_RE if estrito else COTA_RE).search(str(texto)))


def _erro(message, estrito=False):
    """Evento de erro; se for cota, bloqueia o agy (motor_regra) e marca cota=True para o runtime refazer no Claude."""
    ev = dict(t="error", message=message[:500])
    if erro_cota(message, estrito):
        ev["cota"] = True
        try:
            import motor_regra
            motor_regra.bloquear_agy()
        except Exception:  # noqa: BLE001 - falha ao gravar o bloqueio nao derruba o turno
            pass
    return ev


def run(message, session_id=None, system=None, full=True):
    if full is not False:  # turno leve ("que horas sao") nao paga o resumo do projeto; "a fundo", equipe e chat pagam
        system = com_contexto(system, session_id)
    with runtime.EXECUTION_LOCK:
        yield from _run_locked(message, session_id, system, full)


def _run_locked(message, session_id, system, full):
    started = time.monotonic()
    if runtime.CANCEL.is_set():
        yield dict(t="error", message=runtime.CANCELLED, cancelled=True)
        return
    try:
        settings = escreve_settings()
        cmd = monta_comando(session_id, full, settings)
        ruim = recusa_script_cmd(cmd)
        if ruim:
            raise RuntimeError(ruim)
    except (RuntimeError, OSError) as e:
        yield dict(t="error", message=f"motor agy desligado por seguranca: {e}"[:400])
        return
    try:
        proc = subprocess.Popen(cmd, cwd=runtime.WORKDIR, text=True, encoding="utf-8", errors="replace", bufsize=1,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=dict(os.environ, PYTHONUTF8="1", FEDORA_MOTOR_TURNO="agy"))   # agents on both subscriptions follow this
        with runtime._ACTIVE_LOCK:
            runtime._ACTIVE_PROC = proc
    except FileNotFoundError:
        raise RuntimeError("agy nao encontrado. Instale o Antigravity CLI ou defina AGY_CMD.")

    q, lock, done = collections.deque(), threading.Lock(), threading.Event()
    errbuf = collections.deque(maxlen=120)

    def pump_out():
        try:
            for ln in proc.stdout:
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
    try:
        proc.stdin.write(monta_entrada(message, system))
        proc.stdin.close()
    except OSError:
        pass   # o processo ja morreu: o erro real sai do stderr/rc la embaixo

    sessao, conv = session_id, None
    usos = set()
    texto_visto = streamed = finished = False
    primeiro = True
    ultimo = time.monotonic()
    pendentes = set()   # passos de ferramenta ainda sem DONE/ERROR (ex.: o shell esperando agentes.py)
    try:
        while not done.is_set() or q:
            if runtime.CANCEL.is_set():
                runtime.kill_tree(proc)
                yield dict(t="error", message=runtime.CANCELLED, cancelled=True)
                return
            linha = None
            with lock:
                if q:
                    linha = q.popleft()
            if linha is None:
                if proc.poll() is not None and done.is_set():
                    break
                limite = runtime.limite_ocioso(bool(pendentes))
                if time.monotonic() - ultimo > limite:
                    runtime.kill_tree(proc)
                    yield dict(t="error", message=(f"agy went quiet for {limite}s and was stopped. "
                                                   + " | ".join(list(errbuf)[-3:]))[:400])
                    return
                time.sleep(0.04)
                continue
            ultimo = time.monotonic()
            linha = linha.strip()
            if not linha.startswith("{"):
                continue
            try:
                ev = json.loads(linha)
            except json.JSONDecodeError:
                continue
            tipo = ev.get("event")

            if tipo == "init":
                i = ev.get("init") or {}
                conv = ev.get("conversation_id") or conv
                if runtime.valid_session(conv):
                    sessao = conv
                yield dict(t="status", model=i.get("model") or MODELO, tools=len(i.get("tools") or []), mcp=[],
                           permission=i.get("permission_mode") or PERMISSAO, session_id=sessao, runtime="agy")

            elif tipo == "step_update":
                s = ev.get("step_update") or {}
                conv = s.get("conversation_id") or conv
                if runtime.valid_session(conv):
                    sessao = conv
                kind, estado = s.get("step_type"), s.get("state")
                if kind == "agent_response" and s.get("text_delta"):
                    if primeiro:
                        primeiro = False
                        yield dict(t="latency", ms=int((time.monotonic() - started) * 1000))
                    streamed = texto_visto = True
                    yield dict(t="delta", text=s["text_delta"])
                elif kind == "tool":
                    sid = f"{conv}:{s.get('step_index')}"
                    info = s.get("tool_info") or {}
                    nome = s.get("tool_name") or info.get("name") or "tool"
                    if sid not in usos:
                        usos.add(sid)
                        pendentes.add(sid)
                        yield runtime.tool_event(dict(id=sid, name=nome, input=_chaves(info.get("parameters") or {})))
                    if estado not in ("ACTIVE", "PENDING", "RUNNING", "WAITING"):
                        pendentes.discard(sid)   # qualquer estado final (DONE, ERROR, cancelado, pulado) libera o relogio de ociosidade
                    if estado in ("DONE", "ERROR"):
                        res = dict(t="tool", phase="result", id=sid, parent=None, ok=(estado == "DONE" and not info.get("error")),
                                   name=nome)
                        if runtime.escalar_em(info.get("output")):
                            res["escalar"] = True
                        yield res

            elif tipo == "result":
                r = ev.get("result") or {}
                conv = r.get("conversation_id") or conv
                if runtime.valid_session(conv):
                    sessao = conv
                resposta = str(r.get("response") or "")
                if r.get("status") != "SUCCESS" and not texto_visto and not resposta:
                    motivo = r.get("error") or r.get("finished_reason") or r.get("status") or "erro"
                    yield _erro(f"agy: {motivo}"[:400])
                    return
                if not texto_visto and resposta:
                    yield dict(t="delta", text=resposta)
                    texto_visto = True
                if not texto_visto and r.get("denied_actions"):
                    nomes = ", ".join(str(d.get("display_name") or d.get("action")) for d in r["denied_actions"])
                    yield dict(t="error", message=f"agy negou a acao ({nomes}): regra deny ou permissao insuficiente"[:400])
                    return
                u = r.get("usage") or {}
                ent, sai = u.get("input_tokens", 0), u.get("output_tokens", 0)
                STATS.update(turnos=STATS["turnos"] + 1, tokens=STATS["tokens"] + ent + sai, ultimo=ent + sai)
                runtime.LIMITS["last"] = dict(total=ent + sai + (u.get("cache_read_tokens") or 0), out=sai)
                yield dict(t="usage", input_tokens=ent, output_tokens=sai, total_tokens=ent + sai)
                finished = True
                yield dict(t="complete", session_id=sessao, ms=int((time.monotonic() - started) * 1000))
                return

        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            runtime.kill_tree(proc)
            rc = 0 if texto_visto else -1
        if runtime.CANCEL.is_set():
            yield dict(t="error", message=runtime.CANCELLED, cancelled=True)
            return
        if rc != 0:
            yield _erro(" | ".join(list(errbuf)[-6:]) or f"agy exited with code {rc}", estrito=True)
            return
        if not texto_visto:
            yield dict(t="delta", text="O agy terminou sem texto.")
        yield dict(t="complete", session_id=sessao, ms=int((time.monotonic() - started) * 1000))
    finally:
        if proc.poll() is None and finished:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is None:
            runtime.kill_tree(proc)
        with runtime._ACTIVE_LOCK:
            if runtime._ACTIVE_PROC is proc:
                runtime._ACTIVE_PROC = None


# ---------------------------------------------------------------- autoteste

def _casa(regras, comando):
    """Aproximacao local do matcher do agy: regex ancorada na linha inteira; devolve a regra que negou (ou None)."""
    for r in regras:
        m = re.fullmatch(r"command\(regex:(.*)\)", r, re.S)
        if m and re.fullmatch(m.group(1), comando, re.S):
            return r
    return None


def _teste():
    # erro de cota: bloqueia o agy (sem gravar o motor.json real) e marca cota=True; outro erro nao bloqueia
    import motor_regra
    _bq, _chamou = motor_regra.bloquear_agy, []
    motor_regra.bloquear_agy = lambda *a, **k: _chamou.append(1)
    try:
        for msg in ("agy: RESOURCE_EXHAUSTED", "Quota exceeded for model", "HTTP 429 Too Many Requests", "rate limit hit"):
            ev = _erro(msg)
            assert ev.get("cota") and ev["t"] == "error", msg
        assert len(_chamou) == 4
        for msg in ("agy negou a acao (x): regra deny", "agy exited with code 1", "timeout", "porta 14290 ocupada"):
            assert "cota" not in _erro(msg), msg
        assert len(_chamou) == 4
        # stderr: so frase clara de cota; log solto com "quota"/"rate_limit" nao bloqueia
        for msg in ("Error: RESOURCE_EXHAUSTED", "Quota exceeded for model x", "429 Too Many Requests", "rate limit exceeded"):
            assert _erro(msg, estrito=True).get("cota"), msg
        for msg in ("loading quota config", "flag --rate_limit=10", "port 14290 busy", "Error: boom"):
            assert "cota" not in _erro(msg, estrito=True), msg
        assert len(_chamou) == 8
    finally:
        motor_regra.bloquear_agy = _bq
    regras, n = regras_deny()
    cmds = [r for r in regras if r.startswith("command(")]
    assert n["comandos"] >= 50 and len(cmds) >= 50, n
    assert "read_file(.env)" in regras and "write_file(.env)" in regras, regras[-6:]
    assert any("fedora-assistente" in r for r in regras if r.startswith("read_file(")), regras[-6:]
    for r in cmds:   # RE2 nao aceita \_ nem \<espaco-de-letra>: todas precisam compilar e nao ter escape de letra/_
        corpo = re.fullmatch(r"command\(regex:(.*)\)", r, re.S).group(1)
        re.compile(corpo)
        assert not re.search(r"\\[A-Za-z_](?<!\\[sSdDwWbB])", corpo.replace(r"\\", "")), r
    # o que o DENY do Claude barra tem que continuar barrado no agy
    for bloqueado in ("shutdown /s /t 0", "SHUTDOWN.EXE /s", "/c/Windows/System32/shutdown.exe -s", "cmd //c del x.txt",
                      'cmd /c "rd /s /q x"', "powershell -enc ZQBjAGgAbwA=", "pwsh -EncodedCommand ZQBj",
                      'powershell -Command "Remove-Item x"', "Remove-Item x -Recurse", "remove-item x", "echo a && rm -rf x",
                      "Write-Output 1; ri x", "del x", "Restart-Computer", "Stop-Computer -Force",
                      "python -c \"import shutil; shutil.rmtree('x')\"", "py -c \"import os; os.remove('x')\"",
                      "robocopy a b /MIR", "taskkill /F /IM explorer.exe", "git clean -fdx", "cat .env",
                      "Get-Content $env:LOCALAPPDATA\\fedora-assistente\\fedora-assistente.token", "Clear-RecycleBin -Force",
                      "bcdedit /set x", "diskpart", "format C:", "[System.IO.File]::Delete('x')", "rmdir /s x"):
        assert _casa(cmds, bloqueado), f"agy deixaria passar: {bloqueado}"
    import commands
    lixeira = re.search(r'powershell -NoProfile -Command "(.+?)"; para pasta', commands.PC_RULES).group(1)
    permitido = ["git status", "git diff", "dir", "ls .venv", "Get-Content README.md", "Get-Date -Format 'HH:mm'",
                 "Get-Process | Format-Table", "[System.Environment]::GetFolderPath('Desktop')", "cat .env.example",
                 "Get-Content .env.example", "Start-Process explorer C:\\Users\\Pedro", "taskkill /IM chrome.exe",
                 'cmd /c start "" notepad', "explorer C:\\Users\\Pedro", "python -c \"print(1)\"", "hostname",
                 'powershell -NoProfile -Command "' + lixeira.replace("<caminho>", r"C:\Users\Pedro\Documents\lista de compras.txt") + '"']
    for ok in permitido:
        assert not _casa(cmds, ok), f"agy bloquearia por engano: {ok} -> {_casa(cmds, ok)}"

    # settings.json gerado e valido
    tmp = pathlib.Path(tempfile.mkdtemp()) / "s.json"
    escreve_settings(tmp)
    assert json.loads(tmp.read_text(encoding="utf-8"))["permissions"]["deny"] == regras

    # comando: bypass so com settings; sessao so se for UUID; leve desliga slash
    cmd = monta_comando("5e67b166-8b8b-4a3d-807a-52a930995d09", False, tmp)
    assert "--dangerously-skip-permissions" in cmd and cmd[cmd.index("--settings") + 1] == str(tmp), cmd
    assert cmd[cmd.index("--conversation") + 1] == "5e67b166-8b8b-4a3d-807a-52a930995d09" and "--disable-slash-commands" in cmd
    assert "--conversation" not in monta_comando("nao-e-uuid", True, tmp) and "--disable-slash-commands" not in monta_comando(None, True, tmp)
    assert len(subprocess.list2cmdline(monta_comando(None, True, tmp))) < 2000   # a mensagem NAO vai na linha de comando
    assert json.loads(monta_entrada("oi", "SIS"))["message"]["content"].startswith("<instrucoes_do_sistema>\nSIS")
    assert json.loads(monta_entrada("oi"))["message"]["content"] == "oi"

    # cache manual de contexto: so na sessao nova, antes do sistema; retomada nao repete; env desliga
    uuid_ = "5e67b166-8b8b-4a3d-807a-52a930995d09"
    novo = com_contexto("SIS", None)
    assert novo.startswith("<contexto_do_projeto>") and novo.endswith("\n\nSIS"), novo[:80]
    assert com_contexto("SIS", uuid_) == "SIS" and com_contexto(None, uuid_) is None
    assert com_contexto(None, None).startswith("<contexto_do_projeto>")
    os.environ["FEDORA_CONTEXTO_AGY"] = "0"
    try:
        assert com_contexto("SIS", None) == "SIS"
    finally:
        del os.environ["FEDORA_CONTEXTO_AGY"]

    # turno leve nao envia o contexto; completo (a fundo/equipe/chat) envia
    global _run_locked
    real_locked, vistos = _run_locked, []
    def _falso(message, session_id, system, full):
        vistos.append(system)
        yield {"t": "done"}
    _run_locked = _falso
    try:
        list(run("que horas sao", system="SIS", full=False))
        list(run("a fundo", system="SIS", full=True))
    finally:
        _run_locked = real_locked
    assert vistos[0] == "SIS", vistos[0][:80]
    assert vistos[1].startswith("<contexto_do_projeto>") and vistos[1].endswith("\n\nSIS"), vistos[1][:80]

    # falha fechada: sem DENY nao inicia
    real = runtime.DENY
    runtime.DENY = []
    try:
        evs = list(run("x"))
        assert evs and evs[0]["t"] == "error" and "seguranca" in evs[0]["message"], evs
    finally:
        runtime.DENY = real

    # agy falso: eventos reais capturados em 2026-11-09 (texto em pedacos, tool negada, resultado)
    import sys
    pasta = pathlib.Path(tempfile.mkdtemp())
    conv = "5e67b166-8b8b-4a3d-807a-52a930995d09"
    roteiro = [
        {"event": "init", "conversation_id": conv, "init": {"model": "gemini-3.8-flash-medium", "tools": ["a", "b"], "permission_mode": "always-proceed"}},
        {"event": "step_update", "step_update": {"conversation_id": conv, "step_index": 0, "state": "DONE", "step_type": "user_input"}},
        {"event": "step_update", "step_update": {"conversation_id": conv, "step_index": 1, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "Aqui "}},
        {"event": "step_update", "step_update": {"conversation_id": conv, "step_index": 1, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "vai: coração."}},
        {"event": "step_update", "step_update": {"conversation_id": conv, "step_index": 2, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command", "tool_info": {"name": "run_command", "parameters": {"CommandLine": "hostname"}}}},
        {"event": "step_update", "step_update": {"conversation_id": conv, "step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "run_command", "tool_info": {"name": "run_command", "parameters": {"CommandLine": "hostname"}, "output": "PC"}}},
        {"event": "step_update", "step_update": {"conversation_id": conv, "step_index": 3, "state": "ERROR", "step_type": "tool", "tool_name": "run_command", "tool_info": {"name": "run_command", "parameters": {"CommandLine": "rm x"}, "error": {"type": "TOOL_ERROR", "message": "negado"}}}},
        {"event": "step_update", "step_update": {"conversation_id": conv, "step_index": 4, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command", "tool_info": {"name": "run_command", "parameters": {"CommandLine": "python agentes.py qa -"}}}},
        {"event": "result", "result": {"conversation_id": conv, "status": "SUCCESS", "response": "Aqui vai: coração.", "num_turns": 1, "usage": {"input_tokens": 11000, "output_tokens": 40, "total_tokens": 11040}}},
    ]
    falso = pasta / "agy_falso.py"
    falso.write_text("import json, sys\nentrada = json.loads(sys.stdin.readline())\n"
                     "assert 'SIS' in entrada['message']['content'] and entrada['event'] == 'user'\n"
                     f"for e in {roteiro!r}:\n    print(json.dumps(e, ensure_ascii=False), flush=True)\n", encoding="utf-8")
    os.environ["AGY_CMD"] = f'"{sys.executable}" "{falso}"'.replace("\\", "/")
    runtime.novo_turno()
    evs = list(run("oi", None, "SIS", False))
    tipos = [e["t"] for e in evs]
    assert tipos[0] == "status" and evs[0]["runtime"] == "agy" and evs[0]["session_id"] == conv and evs[0]["permission"] == "always-proceed", evs[0]
    assert "".join(e["text"] for e in evs if e["t"] == "delta") == "Aqui vai: coração.", evs
    assert tipos.count("latency") == 1 and tipos.index("latency") < tipos.index("delta")
    usos_ = [e for e in evs if e["t"] == "tool" and e["phase"] == "use"]
    resultados = [e for e in evs if e["t"] == "tool" and e["phase"] == "result"]
    assert [u["input"] for u in usos_] == ["hostname", "rm x", "python agentes.py qa -"] and [r["ok"] for r in resultados] == [True, False], evs
    assert [u.get("agent") for u in usos_] == [None, None, "qa"], usos_   # chamar um agente pelo shell aparece como delegacao
    assert evs[-2]["t"] == "usage" and evs[-2]["total_tokens"] == 11040 and evs[-1] == dict(t="complete", session_id=conv, ms=evs[-1]["ms"]), evs[-2:]
    assert STATS["turnos"] == 1 and STATS["tokens"] == 11040 and runtime._ACTIVE_PROC is None

    # cancelar (Esc): mata o agy falso e a arvore dele
    dorme = pasta / "agy_dorme.py"
    dorme.write_text("import json, sys, time\nsys.stdin.readline()\nprint(json.dumps({'event': 'init', 'conversation_id': None, 'init': {}}), flush=True)\ntime.sleep(60)\n", encoding="utf-8")
    os.environ["AGY_CMD"] = f'"{sys.executable}" "{dorme}"'.replace("\\", "/")
    runtime.novo_turno()
    t0 = time.monotonic()
    out = []
    for e in run("x"):
        out.append(e)
        if e["t"] == "status":
            threading.Timer(0.3, runtime.cancel_active).start()
    assert out[-1] == dict(t="error", message=runtime.CANCELLED, cancelled=True) and time.monotonic() - t0 < 15, out
    assert runtime._ACTIVE_PROC is None
    runtime.novo_turno()

    # erro: processo que sai com codigo != 0
    erro = pasta / "agy_erro.py"
    erro.write_text("import sys\nsys.stdin.readline()\nsys.stderr.write('Error: boom\\n')\nsys.exit(3)\n", encoding="utf-8")
    os.environ["AGY_CMD"] = f'"{sys.executable}" "{erro}"'.replace("\\", "/")
    evs = list(run("x"))
    assert evs[-1]["t"] == "error" and "boom" in evs[-1]["message"], evs

    # validacao unica da traducao: sem o bloqueio de leitura do .env ou do token, nao inicia
    assert traducao_valida(regras, n)
    for tira in ("read_file(.env)", f"read_file({_norm_caminho(_LOCAL)})"):
        sem = [r for r in regras if r != tira]
        assert not traducao_valida(sem, n), tira
        real_rd = regras_deny
        globals()["regras_deny"] = lambda sem=sem: (sem, n)
        try:
            try:
                escreve_settings(tmp)
                raise AssertionError(f"escreve_settings aceitou sem {tira}")
            except RuntimeError:
                pass
        finally:
            globals()["regras_deny"] = real_rd
    assert not traducao_valida(regras, dict(n, comandos=49))

    # .cmd/.bat recusado (o cmd.exe reinterpretaria o argv); .exe e interpretador seguem
    assert recusa_script_cmd([r"C:\x\agy.cmd"]) and recusa_script_cmd(["C:/x/AGY.BAT", "-p"]) and recusa_script_cmd([sys.executable]) is None
    os.environ["AGY_CMD"] = str(pasta / "agy.cmd").replace("\\", "/")
    evs = list(run("x"))
    assert len(evs) == 1 and evs[0]["t"] == "error" and ".cmd" in evs[0]["message"], evs

    # ferramenta em andamento (step tool ACTIVE sem DONE) pausa a contagem de ociosidade ate o teto
    lento = pasta / "agy_lento.py"
    passo = {"event": "step_update", "step_update": {"conversation_id": conv, "step_index": 1, "state": "ACTIVE", "step_type": "tool",
                                                     "tool_name": "run_command", "tool_info": {"name": "run_command", "parameters": {"CommandLine": "x"}}}}
    feito = {"event": "step_update", "step_update": dict(passo["step_update"], state="DONE")}
    fim = {"event": "result", "result": {"conversation_id": conv, "status": "SUCCESS", "response": "ok", "usage": {}}}
    lento.write_text("import json, sys, time\nsys.stdin.readline()\n"
                     f"print(json.dumps({passo!r}), flush=True)\ntime.sleep(float(sys.argv[1]))\n"
                     f"print(json.dumps({feito!r}), flush=True)\nprint(json.dumps({fim!r}), flush=True)\n", encoding="utf-8")
    idle, ag_t = runtime.IDLE_TIMEOUT, runtime.AGENTE_TIMEOUT
    runtime.IDLE_TIMEOUT, runtime.AGENTE_TIMEOUT = 1, -57   # teto = 3 s
    try:
        os.environ["AGY_CMD"] = f'"{sys.executable}" "{lento}" 2'.replace("\\", "/")
        evs = list(run("x"))
        assert evs[-1]["t"] == "complete", evs   # 2 s calado com a ferramenta rodando: passa do IDLE (1 s), nao do teto
        os.environ["AGY_CMD"] = f'"{sys.executable}" "{lento}" 10'.replace("\\", "/")
        t0 = time.monotonic()
        evs = list(run("x"))
        assert evs[-1]["t"] == "error" and "quiet for 3s" in evs[-1]["message"] and time.monotonic() - t0 < 8, evs
    finally:
        runtime.IDLE_TIMEOUT, runtime.AGENTE_TIMEOUT = idle, ag_t
    print("motor_agy ok")


if __name__ == "__main__":
    import sys
    if "--verifica-deny" in sys.argv:
        # Prova ao vivo (gasta 1 chamada do agy): com a flag de bypass LIGADA, o deny gerado barra `hostname`
        # (inofensivo) e o controle deixa passar `whoami`? Nao: so confere o bloqueio. Rode voce mesmo.
        ok, v = disponivel()
        print("agy:", v)
        regras, _ = regras_deny()
        extra = f"command(regex:{_rx_prefixo('hostname')})"
        alvo = pathlib.Path(tempfile.mkdtemp()) / "s.json"
        alvo.write_text(json.dumps({"permissions": {"deny": regras + [extra]}}), encoding="utf-8")
        cmd = _agy_base() + ["-p", "Rode exatamente o comando: hostname. Se for negado ou bloqueado, diga BLOQUEADO.",
                             "--output-format", "json", "--model", MODELO, "--settings", str(alvo), "--dangerously-skip-permissions"]
        r = subprocess.run(cmd, cwd=runtime.WORKDIR, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=180)
        try:
            resp = json.loads(r.stdout)
        except ValueError:
            raise SystemExit(f"saida inesperada: {r.stdout[:300]} {r.stderr[:300]}")
        rodou = os.environ.get("COMPUTERNAME", "?").lower() in str(resp.get("response", "")).lower()
        print("bypass + deny:", "FALHOU - hostname rodou" if rodou else "OK - hostname barrado", "| resposta:", str(resp.get("response"))[:100])
        # o agy ignora em silencio (so loga) entrada deny que nao entende: o log da rodada nao pode ter nenhuma
        logs = sorted((pathlib.Path.home() / ".gemini" / "antigravity-cli" / "log").glob("cli-*.log"), key=lambda p: p.stat().st_mtime)
        ignoradas = [l for l in (logs[-1].read_text(encoding="utf-8", errors="replace").splitlines() if logs else [])
                     if "ignoring invalid deny entry" in l]
        print("entradas deny ignoradas pelo agy:", len(ignoradas), *(l[-160:] for l in ignoradas[:5]), sep="\n  " if ignoradas else " ")
        raise SystemExit(1 if rodou or ignoradas else 0)
    _teste()
