"""
Agentes da Fedora, independentes do motor.

Cada agente (pasta `agentes/`) tem funcao e MODELO fixos. Quem chama (o motor Claude ou o motor agy) usa o mesmo comando:

    <python> agentes.py <agente> "<tarefa>"        (tarefa "-" le do stdin)
    <python> agentes.py listar

Em qual ASSINATURA o agente roda depende so do modelo dele:
- Gemini (Flash/Argon) so existe no Google e roda la, sempre;
- Haiku, Sonnet e Opus so existem no Claude e rodam la, EXCETO com o agy no comando do turno (FEDORA_MOTOR_TURNO, que o
  runtime coloca no ambiente de quem chama e que vem da regra de troca, motor_regra.py: Claude acima de 55% na semana ou em
  90% na sessao de 5 h passa tudo para o agy): ai nada roda no Claude e eles trocam pelo Gemini de SUBSTITUTO_NO_AGY
  (haiku -> Flash low, sonnet -> Flash high, opus -> Argon high). Fora de um turno, le o estado gravado da regra.

Seguranca: nenhum agente roda com permissao total. Cada um recebe so as ferramentas da funcao dele (edita: sim/nao e uma lista
curta de comandos do catalogo COMANDOS) e a lista runtime.DENY (no agy, traduzida por motor_agy.regras_deny()) vale por cima.
Comando liberado e estreito nos dois backends: python so roda um dos AUTOTESTES (lista fechada = tools/testa.ps1), exato,
git so status/diff/log/show sem --output, --ext-diff, --textconv, --no-index, -c, --exec-path; nenhum comando aceita aspas,
barra invertida, $, { } nem ` (o bash juntaria `--o""utput`); ninguem redireciona saida (> <) nem usa * ? [ ] ( ) ! &. O estreito ISOLA quem nao edita (qa, arquiteto); para quem edita (senior, design, writer,
junior) a DENY e so um freio, nao um sandbox (o senior edita codigo e roda autotestes que o usam; pode mexer ate em .git/config). Agente nao chama agente (FEDORA_AGENTE no ambiente trava a recursao). Falha vira `ESCALAR: ...`.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

import runtime

RAIZ = pathlib.Path(__file__).resolve().parent
PASTA = RAIZ / "agentes"
_LOCAL = pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home() / ".local" / "state") / "fedora-assistente"
LOG = pathlib.Path(os.environ.get("FEDORA_AGENTES_LOG") or _LOCAL / "agentes.jsonl")
TIMEOUT = int(os.environ.get("FEDORA_AGENTE_TIMEOUT", "300"))
MAX_TAREFA = 20000   # caracteres; a tarefa vai na linha de comando (limite de 32k do Windows)

# modelo do agente -> id em cada assinatura que o tem. Mude AQUI para redistribuir.
MODELOS = {
    "fable": {"claude": "fable"},
    "opus": {"claude": "opus"},
    "sonnet": {"claude": "sonnet"},
    "haiku": {"claude": "haiku"},
    "gemini-flash": {"agy": "gemini-3.8-flash-low"},
    "gemini-flash-high": {"agy": "gemini-3.8-flash-high"},
    "gemini-argon": {"agy": "gemini-4-argon-medium"},
    "gemini-argon-high": {"agy": "gemini-4-argon-high"},
}
ESFORCOS = ("", "low", "medium", "high", "xhigh", "max")
# Com o agy no comando (Claude acima de 55% na semana ou em 90% na sessao de 5 h), os modelos em SUBSTITUTO_NO_AGY
# trocam pelo equivalente do Google. Excecao deliberada: `fable` (usado apenas pelo `senior`) nao tem substituto no agy e
# roda sempre no Claude, enquanto todo o restante da equipe fica no plano do Gemini.
SUBSTITUTO_NO_AGY = {"haiku": "gemini-flash", "sonnet": "gemini-flash-high", "opus": "gemini-argon-high"}

# Catalogo do cabecalho `comandos:` (nada fora dele: carrega() recusa). Cada item vira regra estreita nos dois backends.
GIT_SUB = ("status", "diff", "log", "show")
PYTHONS = ("python", ".venv/Scripts/python.exe")
COMANDOS = tuple(f"git {s}" for s in GIT_SUB) + PYTHONS
# python NAO roda qualquer .py: so estes pares (script, argumentos), exatamente os autotestes offline de tools/testa.ps1
# (o --teste confere que as duas listas sao iguais). Fora daqui: pip/cProfile/zipfile pela Lib, retro.py --agora, tidy.py --gemini...
AUTOTESTES = (
    ("conversa.py",), ("runtime.py",), ("motor_agy.py",), ("motor_regra.py",), ("agentes.py", "--teste"),
    ("despachante.py", "--teste"), ("listener.py",), ("vad.py",), ("llm.py", "--teste"), ("notes.py", "--teste"),
    ("tidy.py",), ("turnolog.py", "--teste"), ("retro.py", "--teste"), ("atalhos.py", "--teste-filtro"),
    ("whisper_server.py", "--teste"), ("orb.py", "--teste"), ("memory.py",))
# formas aceitas do executavel de cada item do catalogo. So barra normal: a invertida e proibida nos dois backends (o bash a
# come como escape, entao `.venv\Scripts\python.exe` nem roda no Bash; e `\-\-output` furava o deny do git).
EXES = {"python": ("python",), ".venv/Scripts/python.exe": (".venv/Scripts/python.exe", "./.venv/Scripts/python.exe")}
# agy (regex RE2, ancorada no comando inteiro, sem lookahead): argumento simples = so estes caracteres
# (sem aspas, barra invertida, $ { } ; & | < > `). O Claude espelha isso com _DENY_CHARS_CLAUDE.
_ARG = r"[ \t]+[\w./:=,+@%~^-]+"
_GIT_PROIBIDO = ("--out", "--ext", "--textc", "--no-ind", "--exec-path")   # prefixos: o git aceita opcao longa abreviada
# Claude (glob): python seguido de qualquer opcao antes do script, com aspas e espacos variados; -c global do git
_EXES_PY = ("python", "python3", "pythonw", "python.exe", "python3.exe", "pythonw.exe")
_DENY_PY_CLAUDE = ([f"Bash(*{e}{q1}{sep}{q2}-*)" for e in _EXES_PY for q1 in ("", '"', "'") for sep in (" ", "  ", "\t")
                    for q2 in ("", '"', "'")] + ["Bash(*python*<*)", "Bash(*python*/dev/*)"])
_DENY_GIT_CLAUDE = ([f"Bash(*git*{o}*)" for o in _GIT_PROIBIDO]
                    + [f"Bash(*git{q}{sep}-c*)" for q in ("", '"', "'", ".exe") for sep in (" ", "  ", "\t")])
_DENY_GIT_AGY = [r"command(regex:(?i).*git.*--(out|ext|textc|no-ind|exec-path).*)", r"""command(regex:(?i).*git(\.exe)?["']?\s+-c.*)"""]
_DENY_PY_AGY = [r"""command(regex:(?i).*python[3w]?(\.exe)?["']?\s+["']?-.*)"""]
# Claude: o bash junta aspas/escapes (`--o""utput`, `\-\-output` viram --output) e o glob do deny nao ve isso. Quem tem comandos
# nao usa nenhum destes caracteres (nenhum comando liberado precisa). `*\*` e `*\\*` juntos: cobrem a barra invertida quer o CLI
# trate `\` no padrao como literal, quer como escape. Glob e agrupamento (medido no CLI real em 2026-11-09, Haiku, git status): `*` literal
# se escreve `*\**` (uma barra); `? [ ] ( ) ! &` valem como estao; o `#` nao barra (e comentario de shell, o CLI o ignora). Sem isto o
# bash expande `*` DEPOIS do deny (um arquivo chamado `--output=x` virava argumento) e o mesmo comando dava veredito diferente do agy.
_DENY_CHARS_CLAUDE = ['Bash(*"*)', "Bash(*'*)", "Bash(*\\*)", "Bash(*\\\\*)", "Bash(*$*)", "Bash(*{*)", "Bash(*}*)", "Bash(*`*)",
                      "Bash(*\\**)", "Bash(*?*)", "Bash(*[*)", "Bash(*]*)", "Bash(*(*)", "Bash(*)*)", "Bash(*!*)", "Bash(*&*)"]


# ---------------------------------------------------------------- definicoes

def _front(texto):
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", texto, re.S)
    if not m:
        raise ValueError("sem frontmatter")
    campos = {}
    for linha in m.group(1).splitlines():
        if ":" in linha:
            k, v = linha.split(":", 1)
            campos[k.strip()] = v.strip()
    return campos, m.group(2).strip()


def carrega(nome):
    """Definicao de um agente, ou ValueError. O nome so pode ser [a-z0-9_-] (nada de caminho)."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", str(nome)):
        raise ValueError(f"nome de agente invalido: {nome!r}")
    arq = PASTA / f"{nome}.md"
    if not arq.is_file():
        raise ValueError(f"agente desconhecido: {nome} (use: agentes.py listar)")
    campos, corpo = _front(arq.read_text(encoding="utf-8"))
    if campos.get("name") != nome:
        raise ValueError(f"{arq.name}: name deve ser {nome}")
    modelo = campos.get("modelo", "")
    if modelo not in MODELOS:
        raise ValueError(f"{arq.name}: modelo {modelo!r} nao existe em MODELOS")
    esforco = campos.get("esforco", "").strip().lower()
    if esforco not in ESFORCOS:
        raise ValueError(f"{arq.name}: esforco {esforco!r} invalido (aceitos: {', '.join(ESFORCOS[1:])})")
    comandos = [c.strip() for c in campos.get("comandos", "").split(",") if c.strip()]
    fora = [c for c in comandos if c not in COMANDOS]
    if fora:
        raise ValueError(f"{arq.name}: comandos fora do catalogo {fora} (aceitos: {', '.join(COMANDOS)})")
    return dict(nome=nome, nivel=campos.get("nivel", ""), modelo=modelo, esforco=esforco,
                edita=campos.get("edita", "nao") == "sim", comandos=comandos,
                descricao=campos.get("descricao", ""), instrucoes=corpo)


def todos():
    return [carrega(p.stem) for p in sorted(PASTA.glob("*.md"))]


# ---------------------------------------------------------------- qual assinatura

def _instalado(backend):
    if backend == "agy":
        import motor_agy
        return motor_agy.instalado()
    base = runtime._claude_base()
    return bool(base) and (shutil.which(base[0]) is not None or os.path.exists(base[0]))


def _motor_do_estado():
    """Fora de um turno (CLI na mao): o estado gravado pela regra de troca (com o limite de 8 dias); sem espera, o Claude.
    FEDORA_MOTOR=claude|agy forca o motor e ignora a regra (como no runtime)."""
    if runtime.MOTOR != "auto":   # forcado; valor invalido cai no Claude, como runtime.motor_atual()
        return runtime.MOTOR if runtime.MOTOR in ("claude", "agy") else "claude"
    import motor_regra
    return "agy" if motor_regra.espera_ativa(motor_regra._le(motor_regra.ARQUIVO), time.time()) is not None else "claude"


def rota(ag, turno=None):
    """(assinatura, id do modelo nela, motivo). Levanta RuntimeError se nenhuma que tem o modelo esta instalada."""
    modelo = ag["modelo"]
    pref = (turno or os.environ.get("FEDORA_MOTOR_TURNO") or _motor_do_estado()).strip().lower()
    tem = [b for b in ("claude", "agy") if b in MODELOS[modelo]]
    if tem == ["claude"] and pref == "agy" and modelo in SUBSTITUTO_NO_AGY:
        if not _instalado("agy"):   # nunca cai calado no Claude: com o agy no comando nada roda la
            raise RuntimeError("motor do turno e o agy, mas o agy nao esta instalado; nada roda no Claude nesse estado")
        modelo = SUBSTITUTO_NO_AGY[modelo]
        escolhida, motivo = "agy", f"motor do turno e o agy: {ag['modelo']} vira {modelo}"
    elif len(tem) == 1:
        escolhida, motivo = tem[0], "modelo so existe nessa assinatura"
    else:   # reserva sem uso hoje (nenhum modelo de MODELOS esta nas duas); coberto no _teste
        escolhida = pref if pref in tem else "claude"
        motivo = f"modelo nas duas assinaturas: segue o motor do turno ({escolhida})"
        if not _instalado(escolhida):
            escolhida = next((b for b in tem if b != escolhida and _instalado(b)), escolhida)
            motivo = f"{pref} indisponivel: usando {escolhida}"
    if not _instalado(escolhida):
        raise RuntimeError(f"{escolhida} nao esta instalado")
    return escolhida, MODELOS[modelo][escolhida], motivo


# ---------------------------------------------------------------- comandos de cada assinatura

def _rx(p):
    """Texto -> regex RE2 literal (escapa so os metacaracteres; espaco e / ficam como estao)."""
    return re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", p)


def comandos_python(ag):
    """Os comandos python EXATOS que o agente pode rodar: cada forma do executavel x cada par de AUTOTESTES, um espaco entre."""
    return [f"{exe} {' '.join(par)}" for c in ag["comandos"] if c in PYTHONS for exe in EXES[c] for par in AUTOTESTES]


def regras_agy(ag):
    """(allow, deny extra) do agy para os comandos do agente. Espaco ou tab, nunca \\s: a quebra de linha separa comandos."""
    allow, deny = [], []
    exes = [e for c in ag["comandos"] if c in PYTHONS for e in EXES[c]]
    if exes:   # alternancia fechada: executavel exato + par exato, nada antes nem depois
        pares = "|".join(_rx(" ".join(p)) for p in AUTOTESTES)
        allow.append(f"command(regex:({'|'.join(map(_rx, exes))}) ({pares}))")
    for c in ag["comandos"]:
        if c not in PYTHONS:   # git <sub>: o subcomando logo depois de "git" (git -c / --exec-path antes dele nao casam)
            allow.append(rf"command(regex:git {c.split()[1]}({_ARG})*)")
    if any(c in PYTHONS for c in ag["comandos"]):
        deny += _DENY_PY_AGY
    if any(c.startswith("git ") for c in ag["comandos"]):
        deny += _DENY_GIT_AGY
    if ag["comandos"] and not ag["edita"]:
        deny.append("command(regex:.*>.*)")   # quem so le nao grava nem por redirecionamento
    if not ag["edita"]:
        deny.append("write_file(*)")   # e nao grava arquivo pela ferramenta de escrita (segunda barreira: ver monta_agy)
    return allow, deny


def regras_claude(ag):
    """(allowedTools, disallowedTools extra) do Claude para os comandos do agente; runtime.DENY vai junto em monta_claude."""
    # git: prefixo; python: regra EXATA por comando (sem curinga: `python \-c`, `python ""-c` nao casam com nada)
    allow = [f"Bash({c}:*)" for c in ag["comandos"] if c not in PYTHONS] + [f"Bash({c})" for c in comandos_python(ag)]
    deny = []
    if any(c in PYTHONS for c in ag["comandos"]):
        deny += _DENY_PY_CLAUDE
    if any(c.startswith("git ") for c in ag["comandos"]):
        deny += _DENY_GIT_CLAUDE
    if ag["comandos"]:
        deny += _DENY_CHARS_CLAUDE   # mesmos caracteres que o _ARG do agy recusa (inclui $( e ` de substituicao de comando)
        deny += ["Bash(*>*)", "Bash(*<*)"]   # redirecionamento barrado para TODOS (o _ARG do agy ja recusa < >, inclusive para o senior)
    return allow, deny


def _ferramentas_claude(ag):
    t = ["Read", "Glob", "Grep"] + (["Edit", "Write"] if ag["edita"] else []) + (["Bash"] if ag["comandos"] else [])
    return t


def _pasta_backlog():
    """Pasta `backlog` do cofre, se existir: liberada em --add-dir para leitura das atividades do despachante."""
    for var in ("COFRE", "JARVIS_VAULT"):
        v = os.environ.get(var, "").strip()
        if v:
            p = pathlib.Path(v).expanduser() / "backlog"
            if p.is_dir():
                return p.resolve()
    try:
        import memory
        p = memory.VAULT / "backlog"
        if p.is_dir():
            return p.resolve()
    except Exception:
        pass
    return None


def monta_claude(ag, modelo_id):
    """A tarefa NAO vai aqui: vai por stdin (a lista DENY ja ocupa ~14 mil dos 32 mil caracteres da linha de comando)."""
    cmd = runtime._claude_base() + [
        "-p", "--output-format", "json", "--model", modelo_id,
        *(["--effort", ag["esforco"]] if ag.get("esforco") else []),
        "--append-system-prompt", ag["instrucoes"], "--no-session-persistence", "--disable-slash-commands",
        "--settings", json.dumps({"enabledPlugins": {"ponytail@ponytail": False}}),
        "--tools", ",".join(_ferramentas_claude(ag)),
        "--permission-mode", "acceptEdits" if ag["edita"] else "default"]
    b = _pasta_backlog()
    if b:
        cmd += ["--add-dir", str(b)]
    allow, deny = regras_claude(ag)
    if allow:
        cmd += ["--allowedTools", *allow]
    return cmd + ["--disallowedTools", *runtime.DENY, *deny]


def settings_agy(ag, destino):
    """settings.json do agy deste agente: o deny da Fedora + o deny da funcao + so os comandos que a funcao libera."""
    import motor_agy
    deny, n = motor_agy.regras_deny()
    if not motor_agy.traducao_valida(deny, n):
        raise RuntimeError(f"traducao do DENY incompleta ({n}); agente no agy nao inicia sem as travas")
    allow, extra = regras_agy(ag)
    pathlib.Path(destino).write_text(json.dumps({"permissions": {"allow": allow, "deny": deny + extra}}, ensure_ascii=False),
                                     encoding="utf-8")
    return destino


def aviso_headless_agy(ag):
    """O agy headless ENCERRA o agente, sem resposta, no primeiro comando que a regra nega (o Claude devolve a negacao ao
    modelo e ele segue). Medido em 2026-11-09: o qa morreu em 8 s por causa de um `git status; git diff`. Por isso o pedido
    diz, em texto, exatamente o que pode rodar."""
    if not ag["comandos"]:
        return ("IMPORTANTE (modo headless): nenhum comando de shell esta liberado para voce; um comando negado encerra a tarefa "
                "sem resposta. Use so as ferramentas de ler e buscar em arquivos.")
    py = [" ".join(par) for par in AUTOTESTES] if any(c in PYTHONS for c in ag["comandos"]) else []
    return ("IMPORTANTE (modo headless): um comando de shell fora da lista liberada ENCERRA a tarefa sem resposta. Para ler e procurar em "
            "arquivos use so as ferramentas de leitura e busca de arquivo, nunca ls, cat, grep, find, dir, Get-Content ou Select-String. "
            "Rode um comando por vez, sem aspas e sem ; && | > <. Comandos liberados: "
            + ", ".join(c for c in ag["comandos"] if c not in PYTHONS)
            + (f"; e, com o executavel .venv/Scripts/python.exe, exatamente um destes pares: {'; '.join(py)}" if py else "") + ".")


def monta_agy(ag, tarefa, modelo_id, settings):
    import motor_agy
    cmd = motor_agy._agy_base() + [
        "-p", f"{ag['instrucoes']}\n\n{aviso_headless_agy(ag)}\n\n## Tarefa\n{tarefa}", "--output-format", "json", "--model", modelo_id,
        "--mode", "accept-edits" if ag["edita"] else "plan", "--settings", str(settings), "--print-timeout", f"{TIMEOUT}s"]
    b = _pasta_backlog()
    if b:
        cmd += ["--add-dir", str(b)]
    # MEDIDO (agy 1.5.3, 2026-11-09): com --disable-slash-commands o --mode plan NAO tem efeito (um agente que so le criou arquivo).
    # Quem so le roda SEM a flag (e ainda com o deny write_file(*): medido que nao cria nem edita arquivo existente e termina com
    # resposta). Quem edita mantem a flag: medido que, com ou sem ela, o comando fora da lista e negado e a edicao funciona.
    if ag["edita"]:
        cmd.append("--disable-slash-commands")
    return cmd


# ---------------------------------------------------------------- execucao

def _json_final(saida):
    for linha in reversed((saida or "").strip().splitlines()):
        if linha.startswith("{"):
            try:
                return json.loads(linha)
            except ValueError:
                continue
    return None


def executa(ag, tarefa, turno=None, timeout=None):
    """Roda o agente. Devolve dict(ok, texto, erro, assinatura, modelo, ms, tokens_in, tokens_out)."""
    t0 = time.monotonic()
    res = dict(ok=False, texto="", erro="", assinatura="?", modelo="?", ms=0, tokens_in=0, tokens_out=0)
    pasta = None
    try:
        if len(tarefa) > MAX_TAREFA:
            raise RuntimeError(f"tarefa grande demais ({len(tarefa)} caracteres; maximo {MAX_TAREFA}): quebre em partes")
        assinatura, modelo_id, _ = rota(ag, turno)
        res.update(assinatura=assinatura, modelo=modelo_id)
        import motor_agy
        if assinatura == "claude":
            cmd, entrada = monta_claude(ag, modelo_id), tarefa
        else:
            pasta = tempfile.mkdtemp(prefix="fedora-agente-")
            entrada = None
            cmd = monta_agy(ag, tarefa, modelo_id, settings_agy(ag, os.path.join(pasta, "settings.json")))
        ruim = motor_agy.recusa_script_cmd(cmd)   # .cmd/.bat: o cmd.exe reinterpretaria & e " da tarefa
        if ruim:
            raise RuntimeError(ruim)
        # MAX_TAREFA conta caracteres; o que estoura e a linha depois do escape do Windows (aspas e barras dobram)
        tamanho = len(subprocess.list2cmdline(cmd))
        if tamanho >= 30000:
            raise RuntimeError(f"tarefa grande demais para a linha de comando ({tamanho} caracteres; maximo 30000): quebre em partes")
        proc = subprocess.Popen(cmd, cwd=RAIZ, text=True, encoding="utf-8", errors="replace",
                                stdin=subprocess.PIPE if entrada else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=dict(os.environ, FEDORA_AGENTE=ag["nome"], PYTHONUTF8="1"))
        try:
            out, err = proc.communicate(entrada, timeout=timeout or TIMEOUT + 30)
        except subprocess.TimeoutExpired:
            runtime.kill_tree(proc)
            proc.communicate()
            raise RuntimeError(f"sem resposta em {timeout or TIMEOUT + 30}s")
        j = _json_final(out)
        if proc.returncode != 0 or not j:
            raise RuntimeError((err or out or f"codigo {proc.returncode}").strip().splitlines()[-1][:300])
        u = j.get("usage") or {}
        if assinatura == "claude":
            texto, erro = str(j.get("result") or ""), bool(j.get("is_error"))
            res.update(tokens_in=sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")),
                       tokens_out=u.get("output_tokens") or 0)
        else:
            texto, erro = str(j.get("response") or ""), j.get("status") != "SUCCESS"
            res.update(tokens_in=u.get("input_tokens") or 0, tokens_out=u.get("output_tokens") or 0)
            if not texto.strip() and j.get("denied_actions"):
                raise RuntimeError("o agy negou uma acao (regra deny ou comando nao liberado para esta funcao)")
        if erro or not texto.strip():
            raise RuntimeError((str(j.get("error") or texto) or "resposta vazia").strip()[:300])
        res.update(ok=True, texto=texto.strip())
    except (RuntimeError, ValueError, OSError) as e:
        res["erro"] = str(e)
    finally:
        if pasta:
            shutil.rmtree(pasta, ignore_errors=True)
        res["ms"] = int((time.monotonic() - t0) * 1000)
        _registra(ag, res)
    return res


def _registra(ag, res):
    """Uma linha por chamada (base da regra de cota por agente). Perder o log nunca atrapalha a resposta."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(ts=int(time.time()), agente=ag["nome"], modelo=ag["modelo"], assinatura=res["assinatura"],
                                    ok=res["ok"], ms=res["ms"], tokens_in=res["tokens_in"], tokens_out=res["tokens_out"]),
                               ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- linha de comando

def listar():
    for ag in todos():
        try:
            onde = rota(ag)[0]
        except RuntimeError as e:
            onde = f"indisponivel ({e})"
        print(f"{ag['nome']:8s} {ag['nivel']:8s} {ag['modelo']:13s} {'edita' if ag['edita'] else 'le':6s} -> {onde}")


def main(argv):
    if argv and argv[0] == "--teste":
        return _teste()
    if not argv or argv[0] in ("listar", "-l", "--listar"):
        listar()
        return 0
    if os.environ.get("FEDORA_AGENTE"):
        print("ESCALAR: agente nao chama agente; devolva o pedido ao tech lead")
        return 3
    nome, tarefa = argv[0], " ".join(argv[1:]).strip()
    if tarefa == "-":
        tarefa = sys.stdin.read().strip()
    if not tarefa:
        print("uso: agentes.py <agente> \"<tarefa>\"   (ou: agentes.py listar)")
        return 2
    try:
        ag = carrega(nome)
    except ValueError as e:
        print(f"ESCALAR: {e}")
        return 2
    r = executa(ag, tarefa)
    if r["ok"]:
        print(r["texto"])
        return 0
    print(f"ESCALAR: {nome} falhou ({r['assinatura']}): {r['erro']}")
    return 1


# ---------------------------------------------------------------- autoteste

def _teste():
    motor_orig = runtime.MOTOR   # o teste mexe em runtime.MOTOR: devolve o valor de quem rodou
    try:
        return _teste_corpo()
    finally:
        runtime.MOTOR = motor_orig


def _teste_corpo():
    global LOG, TIMEOUT
    import motor_agy
    import motor_regra

    # definicoes: todas carregam, modelos existem, EQUIPE.md cita cada uma com o modelo certo
    ags = {a["nome"]: a for a in todos()}
    assert set(ags) >= {"junior", "writer", "design", "arquiteto", "senior", "qa", "techlead"}, ags.keys()
    assert ags["qa"]["modelo"] == "opus" and not ags["qa"]["edita"]
    assert ags["senior"]["modelo"] == "fable" and ags["senior"]["esforco"] == "medium" and ags["senior"]["edita"]
    assert ags["techlead"]["modelo"] == "gemini-argon-high" and not ags["techlead"]["edita"]
    assert ags["writer"]["modelo"] == "gemini-flash" and ags["junior"]["modelo"] == "gemini-flash"
    assert ags["design"]["modelo"] == "sonnet" and ags["design"]["edita"]
    assert ags["arquiteto"]["modelo"] == "gemini-argon" and not ags["arquiteto"]["edita"]
    equipe = (RAIZ / "docs" / "EQUIPE.md").read_text(encoding="utf-8")
    for a in ags.values():
        assert f"`{a['nome']}`" in equipe, f"EQUIPE.md nao cita {a['nome']}"
    for ruim in ("../x", "..\\x", "Qa", "", "a b", "x" * 40, "qa.md"):
        try:
            carrega(ruim)
            raise AssertionError(f"aceitou nome ruim {ruim!r}")
        except ValueError:
            pass
    try:
        carrega("inexistente")
        raise AssertionError("aceitou agente inexistente")
    except ValueError:
        pass

    # fakes dos dois CLIs: gravam o argv e respondem JSON
    tmp = pathlib.Path(tempfile.mkdtemp())
    grava = tmp / "argv.json"
    fake = tmp / "fake_cli.py"
    fake.write_text(
        "import json, os, sys, time\n"
        f"open({str(grava)!r}, 'w', encoding='utf-8').write(json.dumps({{'argv': sys.argv[1:], 'agente': os.environ.get('FEDORA_AGENTE')}}))\n"
        "modo = os.environ.get('FAKE_MODO', 'ok')\n"
        "if sys.argv[1:] == ['--version']: print('9.9'); sys.exit(0)\n"
        "if modo == 'dorme': time.sleep(60)\n"
        "if modo == 'sai1': sys.stderr.write('Error: boom\\n'); sys.exit(1)\n"
        "if '--dangerously-skip-permissions' in sys.argv or 'bypassPermissions' in sys.argv: sys.exit(9)\n"
        "if os.environ.get('FAKE_TIPO') == 'agy':\n"
        "    print(json.dumps({'status': 'ERROR' if modo == 'erro' else 'SUCCESS', 'response': '' if modo == 'erro' else 'feito pelo agy', 'usage': {'input_tokens': 100, 'output_tokens': 7}}))\n"
        "else:\n"
        "    print(json.dumps({'type': 'result', 'is_error': modo == 'erro', 'result': 'falhou' if modo == 'erro' else 'feito pelo claude', 'usage': {'input_tokens': 50, 'cache_read_input_tokens': 10, 'output_tokens': 3}}))\n",
        encoding="utf-8")
    py = str(sys.executable).replace("\\", "/")
    os.environ["CLAUDE_CMD"] = f'"{py}" "{str(fake).replace(chr(92), "/")}"'
    os.environ["AGY_CMD"] = os.environ["CLAUDE_CMD"]
    LOG = tmp / "agentes.jsonl"
    motor_regra.ARQUIVO = tmp / "motor.json"

    # roteamento
    runtime.MOTOR = "auto"   # o teste nao depende do FEDORA_MOTOR de quem roda
    assert rota(ags["design"], "claude")[:2] == ("claude", "sonnet")    # Sonnet so existe no Claude
    assert rota(ags["design"], "agy")[:2] == ("agy", "gemini-3.8-flash-high")   # ...mas com o agy no comando nada roda no Claude (exceto senior/fable)
    assert rota(ags["writer"], "claude")[0] == "agy" and rota(ags["junior"], "claude")[0] == "agy"   # Gemini so existe no Google, mesmo com o turno no Claude
    for nome_ in ags:                                         # turno no agy: apenas o senior (fable) roda no Claude; o resto fica no agy
        esperado_ = "claude" if nome_ == "senior" else "agy"
        assert rota(ags[nome_], "agy")[0] == esperado_, (nome_, rota(ags[nome_], "agy"))
    assert rota(ags["senior"], "agy")[:2] == ("claude", "fable") and rota(ags["senior"], "claude")[:2] == ("claude", "fable")
    assert rota(ags["techlead"], "agy")[:2] == ("agy", "gemini-4-argon-high") and rota(ags["techlead"], "claude")[:2] == ("agy", "gemini-4-argon-high")
    assert rota(ags["arquiteto"])[1] == "gemini-4-argon-medium"
    assert rota(ags["qa"], "claude")[:2] == ("claude", "opus") and rota(ags["qa"], "agy")[:2] == ("agy", "gemini-4-argon-high")
    os.environ["FEDORA_MOTOR_TURNO"] = "agy"
    assert rota(ags["senior"])[:2] == ("claude", "fable")     # turno no agy pelo ambiente: senior mantem Claude Fable; qa vai pro agy
    assert rota(ags["qa"])[0] == "agy"
    del os.environ["FEDORA_MOTOR_TURNO"]
    assert rota(ags["qa"])[0] == "claude"                     # sem turno e sem espera gravada: Claude
    motor_regra._grava(motor_regra.ARQUIVO, {"motor": "agy", "ate": time.time() + 3600})
    assert rota(ags["qa"])[0] == "agy"                        # fora de um turno: segue a espera gravada da regra
    runtime.MOTOR = "claude"
    assert rota(ags["qa"])[:2] == ("claude", "opus")          # FEDORA_MOTOR=claude forca e ignora a espera gravada
    runtime.MOTOR = "xyz"
    assert _motor_do_estado() == "claude" == runtime.motor_atual()   # FEDORA_MOTOR invalido: Claude, igual ao runtime
    runtime.MOTOR = "auto"
    motor_regra._grava(motor_regra.ARQUIVO, {"motor": "agy", "ate": time.time() - 1})
    assert rota(ags["qa"])[0] == "claude"                     # espera vencida

    # todo modelo so do Claude (exceto fable, exclusivo do senior no Claude) tem substituto no agy, e o substituto existe no Google
    for m_, ids_ in MODELOS.items():
        if list(ids_) == ["claude"] and m_ != "fable":
            sub_ = SUBSTITUTO_NO_AGY.get(m_)
            assert sub_ in MODELOS and "agy" in MODELOS[sub_], (m_, sub_)
    # agy no turno (ex.: Claude em 56% na semana): haiku/sonnet/opus viram Gemini; agy ausente => erro, nunca o Claude
    inst_real = _instalado
    try:
        globals()["_instalado"] = lambda b: True
        for m_, id_ in (("haiku", "gemini-3.8-flash-low"), ("sonnet", "gemini-3.8-flash-high"), ("opus", "gemini-4-argon-high")):
            assert rota(dict(ags["qa"], modelo=m_), "agy")[:2] == ("agy", id_), m_
        globals()["_instalado"] = lambda b: b == "claude"
        for m_ in ("haiku", "sonnet", "opus"):
            try:
                rota(dict(ags["qa"], modelo=m_), "agy")
                raise AssertionError(f"{m_} caiu no Claude com o agy no turno e sem agy")
            except RuntimeError as e:
                assert "agy nao esta instalado" in str(e) and "nada roda no Claude" in str(e), e
        assert rota(ags["qa"], "claude")[:2] == ("claude", "opus")   # FEDORA_MOTOR=claude / turno no Claude: nada muda
        r = executa(ags["qa"], "revise", "agy")                     # o chamador devolve o erro legivel, sem quebrar
        assert not r["ok"] and "agy nao esta instalado" in r["erro"], r
        # ramo reserva (modelo nas duas assinaturas): segue o turno; cai na outra se a do turno faltar
        MODELOS["_duplo"] = {"claude": "x-claude", "agy": "x-agy"}
        duplo = dict(ags["qa"], modelo="_duplo")
        globals()["_instalado"] = lambda b: True
        assert rota(duplo, "agy")[:2] == ("agy", "x-agy") and rota(duplo, "claude")[:2] == ("claude", "x-claude")
        globals()["_instalado"] = lambda b: b == "claude"
        assert rota(duplo, "agy")[:2] == ("claude", "x-claude")
    finally:
        globals()["_instalado"] = inst_real
        MODELOS.pop("_duplo", None)

    # comando do Claude: ferramentas e permissoes da funcao, nunca bypass, DENY junto, cabe nos 32k do Windows
    c = monta_claude(ags["qa"], "opus")
    assert c[c.index("--tools") + 1] == "Read,Glob,Grep,Bash" and c[c.index("--permission-mode") + 1] == "default"
    assert "Bash(git diff:*)" in c and "Bash(python runtime.py)" in c and "--disallowedTools" in c and "Edit" not in c[c.index("--tools") + 1]
    assert not [r for r in regras_claude(ags["qa"])[0] if "python" in r and "*" in r], "python no Claude so por regra exata"
    # deny de caracteres e so Bash(...): se um agente ganhar a ferramenta PowerShell, crie o espelho PowerShell(...) antes
    assert all("PowerShell" not in _ferramentas_claude(a) for a in ags.values())
    # AUTOTESTES == pares de tools/testa.ps1 (uma mudanca la sem mudar aqui, ou vice-versa, derruba o teste)
    ps = (RAIZ / "tools" / "testa.ps1").read_text(encoding="utf-8")
    pares_ps = [tuple(re.findall(r'"([^"]*)"', m)) for m in re.findall(r'@\("[^"]*",\s*@\(([^()]*)\)\)', ps)]
    assert len(pares_ps) >= 16 and set(pares_ps) == set(AUTOTESTES) and len(set(AUTOTESTES)) == len(AUTOTESTES), \
        (set(pares_ps) ^ set(AUTOTESTES))
    j = monta_claude(ags["junior"], "haiku")
    assert j[j.index("--tools") + 1] == "Read,Glob,Grep,Edit,Write" and j[j.index("--permission-mode") + 1] == "acceptEdits" and "--allowedTools" not in j
    s_cmd = monta_claude(ags["senior"], "fable")
    assert s_cmd[s_cmd.index("--model") + 1] == "fable" and s_cmd[s_cmd.index("--effort") + 1] == "medium"
    for cmd in (c, j, s_cmd):
        assert "bypassPermissions" not in cmd and "--dangerously-skip-permissions" not in cmd
        assert len(subprocess.list2cmdline(cmd)) < 30000, len(subprocess.list2cmdline(cmd))
        assert "x" * 50 not in " ".join(cmd)   # a tarefa nao esta na linha de comando

    # o que cada backend deixa rodar. Claude: APROXIMACAO LOCAL do matcher do CLI (nao e o CLI real, nao interpreta aspas nem
    # normaliza espacos): deny por glob no comando inteiro ou x:* em cada subcomando; allow exigido em TODO subcomando
    # (headless, sem allow = negado), x:* por prefixo e regra sem :* por igualdade exata.
    def glob_rx(pat):
        """glob do Claude: `*` e curinga; `\\*` e um asterisco literal (medido no CLI real: `Bash(*\\**)` barra so comando com `*`)."""
        out, i = [], 0
        while i < len(pat):
            if pat[i] == chr(92) and i + 1 < len(pat) and pat[i + 1] in (chr(92), "*"):
                out.append(re.escape(pat[i + 1]))
                i += 2
            else:
                out.append(".*" if pat[i] == "*" else re.escape(pat[i]))
                i += 1
        return "".join(out)

    def claude_ok(ag_, cmd_):
        allow_, extra_ = regras_claude(ag_)
        partes = re.split(r"\s*(?:;|&&|\|\||\||\n)\s*", cmd_.strip())
        for rule in runtime.DENY + extra_:
            m_ = re.fullmatch(r"Bash\((.*)\)", rule, re.S)
            if not m_:
                continue
            pat = m_.group(1)
            if pat.endswith(":*"):
                if any(re.match(re.escape(pat[:-2]) + r"(\.exe)?(\s|$)", p) for p in partes):
                    return False
            elif re.fullmatch(glob_rx(pat), cmd_, re.S):
                return False
        def casa(a, p):
            corpo = a[5:-1]
            return re.match(re.escape(corpo[:-2]) + r"(\s|$)", p) is not None if corpo.endswith(":*") else p == corpo
        return all(any(casa(a, p) for a in allow_) for p in partes)

    sj = settings_agy(ags["qa"], tmp / "s_qa.json")
    cfg = json.loads(pathlib.Path(sj).read_text(encoding="utf-8"))["permissions"]

    def agy_ok(ag_, cmd_):
        allow_, extra_ = regras_agy(ag_)
        rx = lambda r: re.fullmatch(r"command\(regex:(.*)\)", r, re.S)   # noqa: E731
        negado = any(rx(r) and re.fullmatch(rx(r).group(1), cmd_, re.S) for r in motor_agy.regras_deny()[0] + extra_)
        return not negado and any(re.fullmatch(rx(a).group(1), cmd_, re.S) for a in allow_)

    assert all(r in cfg["deny"] for r in regras_agy(ags["qa"])[1]) and cfg["allow"] == regras_agy(ags["qa"])[0]
    for r in cfg["allow"] + regras_agy(ags["qa"])[1]:   # RE2 do agy: tudo compila, sem lookahead nem backreference
        if r.startswith("write_file("):   # regra de arquivo, nao de comando: nao tem regex
            assert r == "write_file(*)", r
            continue
        corpo = re.fullmatch(r"command\(regex:(.*)\)", r, re.S).group(1)
        re.compile(corpo)
        assert "(?=" not in corpo and "(?!" not in corpo and not re.search(r"\\[1-9]", corpo), r
    qa_, sr = ags["qa"], ags["senior"]
    # UMA lista de entradas; o veredito do Claude tem de ser igual ao do agy e ao esperado
    permitidos = ("git diff", "git diff --stat HEAD~1", "git status --short", "git diff --stat", "git log --oneline -5",
                  "git log -5 --oneline", "git show HEAD", "git diff -c HEAD", "git diff --stat\tHEAD",
                  "python runtime.py", "python agentes.py --teste", ".venv/Scripts/python.exe runtime.py",
                  ".venv/Scripts/python.exe agentes.py --teste", "./.venv/Scripts/python.exe llm.py --teste",
                  "python atalhos.py --teste-filtro", "python memory.py")
    furos = (r'python \-c "x"', 'python ""-c "x"', "python $'-c' x", "python {-c,} x", 'git diff --o""utput=x',
             r"git diff \-\-output=x", "git diff --output=x", "python .venv/Lib/site-packages/pip/__main__.py install x",
             "python C:/Python312/Lib/cProfile.py -m x", "python ../outro/runtime.py", "python retro.py --agora",
             "python tidy.py --gemini", "python runtime.py extra", "python runtime.py; hostname", "PYTHON runtime.py",
             "python  runtime.py", "python ./runtime.py", r".venv\Scripts\python.exe llm.py --teste",
             r".\.venv\Scripts\python.exe runtime.py", '"python" agentes.py --teste', '".venv/Scripts/python.exe" runtime.py',
             "python tools/x.py --a=1 b", "python agentes.py", "python agentes.py --teste extra", "python.exe runtime.py",
             "python runtime.py\nhostname", "python atalhos.py",
             "python -m zipfile -c x.zip y", "python C:/Python312/Lib/zipfile/__main__.py -c x.zip y", "python /x/runtime.py",
             ".venv/Lib/python.exe runtime.py", "../.venv/Scripts/python.exe runtime.py", "python agentes.py qa x",
             r"git diff a\b", "git diff ${x}", "git diff {a,b}", "git log '--output=x'")
    for cmd_, esperado in [(p, True) for p in permitidos] + [(f, False) for f in furos]:
        cl, ag_ = claude_ok(qa_, cmd_), agy_ok(qa_, cmd_)
        assert cl == ag_ == esperado, (cmd_, "claude", cl, "agy", ag_, "esperado", esperado)
    barrados = ('python -c "print(1)"', "python -m x", "python -m pip install y", "python - <<EOF\nprint(1)\nEOF", "python -",
                "python -X dev x.py", "python -I x.py", "python -W ignore x.py", "python -i x.py", "python -x x.py", "python -Ic x",
                ".venv/Scripts/python.exe -c x", '"python" -c x', "python  -c x", "python\t-c x", "python '-c' x", "python3 -c x",
                "py -c x", "python /dev/stdin", "python < x.py", "pythonx foo", "FEDORA_AGENTE= python x.py",
                "python x.py > out.txt", 'python x.py "$(whoami)"', "python x.py `whoami`",
                "git diff --output=x", "git diff --output x", "git log --output=x", "git show --out=x", "git -c core.pager=x diff",
                "git diff --ext-diff", "git diff --textconv", "git diff --no-index a b", "git --exec-path=x diff",
                "git diff > f.txt", "git push", "git commit -m x", "git add .", "git diff; hostname", "git status && del x",
                "python x.py | more", "git log $(whoami)", "git diff\nhostname", "curl http://x")
    for barrado in barrados:
        assert not claude_ok(qa_, barrado), ("claude deixaria", barrado)
        assert not agy_ok(qa_, barrado), ("agy deixaria", barrado)
    # glob e agrupamento (QA, 3a rodada): o mesmo veredito nos dois backends, para quem le (qa) E para quem edita (senior)
    glob_e_grupo = ("git diff -- *.py", "git diff *", "git log --pretty=format:(%h)", "git status &", "git status --short ?x",
                    "git diff [a]", "git diff !x", "git diff a)", "git diff > f.txt", "git diff < f", "git log 2>&1", "git status --short #x")
    for ag_nome in (qa_, sr):
        for cmd_ in glob_e_grupo[:-1]:   # o ultimo (`#x`, comentario de shell) o CLI ignora: so o agy barra, e e inofensivo
            assert not claude_ok(ag_nome, cmd_) and not agy_ok(ag_nome, cmd_), (ag_nome["nome"], cmd_)
        for cmd_ in ("git diff --stat", "git log --oneline -5", "git status --short", "python runtime.py", ".venv/Scripts/python.exe llm.py --teste"):
            assert claude_ok(ag_nome, cmd_) and agy_ok(ag_nome, cmd_), (ag_nome["nome"], cmd_)
        for cmd_ in permitidos + furos:   # a lista inteira, igual nos dois e igual entre qa e senior (menos o que so o qa tem: git show)
            if cmd_ == "git show HEAD":
                continue
            assert claude_ok(ag_nome, cmd_) == agy_ok(ag_nome, cmd_), (ag_nome["nome"], cmd_, claude_ok(ag_nome, cmd_), agy_ok(ag_nome, cmd_))
    assert "Bash(*\\**)" in regras_claude(qa_)[1] and "Bash(*\\**)" in regras_claude(sr)[1] and "Bash(*>*)" in regras_claude(sr)[1]
    for par in AUTOTESTES:   # todo autoteste roda nos dois, pelas 3 formas do executavel
        for exe in ("python", ".venv/Scripts/python.exe", "./.venv/Scripts/python.exe"):
            assert claude_ok(qa_, f"{exe} {' '.join(par)}") and agy_ok(qa_, f"{exe} {' '.join(par)}"), (exe, par)
    for barrado in ('python -c "import os; os.environ.pop(\'FEDORA_AGENTE\')"', "python -m x", "git diff --output=x", "git show HEAD"):
        assert not claude_ok(sr, barrado) and not agy_ok(sr, barrado), ("senior", barrado)   # senior: mesmas regras (sem git show)
    assert claude_ok(sr, "python runtime.py") and agy_ok(sr, "git log -3")
    assert len(cfg["deny"]) >= 50 and not json.loads(pathlib.Path(settings_agy(ags["arquiteto"], tmp / "s_ar.json")).read_text(encoding="utf-8"))["permissions"]["allow"]
    assert regras_agy(ags["arquiteto"]) == ([], ["write_file(*)"]) and regras_claude(ags["junior"]) == ([], [])   # arquiteto: sem comando, mas so le

    # cabecalho `comandos:` fora do catalogo: o agente nao carrega
    pasta_real = PASTA
    globals()["PASTA"] = tmp / "agentes_ruins"
    PASTA.mkdir()
    try:
        for ruim in ("python -c", "git push", "bash", "git diff --output=x"):
            (PASTA / "ruim.md").write_text(f"---\nname: ruim\nmodelo: opus\nedita: nao\ncomandos: git diff, {ruim}\n---\nx\n", encoding="utf-8")
            try:
                carrega("ruim")
                raise AssertionError(f"aceitou comando fora do catalogo: {ruim}")
            except ValueError as e:
                assert "catalogo" in str(e), e
    finally:
        globals()["PASTA"] = pasta_real

    # traducao do DENY sem o bloqueio de leitura do .env/token derruba o agente no agy (mesma conferencia do motor)
    real_rd = motor_agy.regras_deny
    for tira in ("read_file(.env)", "read_file(" + motor_agy._norm_caminho(motor_agy._LOCAL) + ")"):
        motor_agy.regras_deny = lambda tira=tira: ([r for r in real_rd()[0] if r != tira], real_rd()[1])
        try:
            settings_agy(ags["qa"], tmp / "s_x.json")
            raise AssertionError(f"settings_agy aceitou sem {tira}")
        except RuntimeError:
            pass
        finally:
            motor_agy.regras_deny = real_rd
    assert len(subprocess.list2cmdline(monta_agy(ags["senior"], "x" * MAX_TAREFA, "gemini-4-argon-high", sj))) < 30000   # tarefa maxima no agy cabe
    a = monta_agy(ags["qa"], "revise", "gemini-4-argon-high", sj)
    assert "--disable-slash-commands" not in a, "qa no agy: com a flag o --mode plan nao vale (medido) e o agente grava arquivo"
    assert "--disable-slash-commands" not in monta_agy(ags["arquiteto"], "x", "gemini-4-argon-medium", sj)
    assert "--disable-slash-commands" in monta_agy(ags["writer"], "x", "gemini-3.8-flash-low", sj)   # quem edita mantem (accept-edits)
    for nome_, quer in (("qa", True), ("arquiteto", True), ("writer", False), ("design", False), ("senior", False), ("junior", False)):
        deny_ = json.loads(pathlib.Path(settings_agy(ags[nome_], tmp / f"s_{nome_}.json")).read_text(encoding="utf-8"))["permissions"]["deny"]
        assert ("write_file(*)" in deny_) == quer, (nome_, quer)   # so quem nao edita ganha o deny de escrita
    aviso_qa, aviso_ar = aviso_headless_agy(ags["qa"]), aviso_headless_agy(ags["arquiteto"])
    assert "ENCERRA" in aviso_qa and "git diff" in aviso_qa and "runtime.py" in aviso_qa and "llm.py --teste" in aviso_qa, aviso_qa
    assert "nenhum comando de shell" in aviso_ar and aviso_qa in a[a.index("-p") + 1]
    assert a[a.index("--mode") + 1] == "plan" and a[a.index("--model") + 1] == "gemini-4-argon-high" and "--dangerously-skip-permissions" not in a
    assert monta_agy(ags["writer"], "ajuste", "gemini-3.8-flash-low", sj)[monta_agy(ags["writer"], "x", "m", sj).index("--mode") + 1] == "accept-edits"

    # execucao com os fakes
    r = executa(ags["design"], "ajuste", "claude")
    assert r["ok"] and r["texto"] == "feito pelo claude" and r["assinatura"] == "claude" and r["tokens_in"] == 60 and r["tokens_out"] == 3, r
    assert json.loads(grava.read_text(encoding="utf-8"))["agente"] == "design"       # FEDORA_AGENTE chega no filho (trava a recursao)
    os.environ["FAKE_TIPO"] = "agy"
    r = executa(ags["writer"], "ajuste", "claude")
    assert r["ok"] and r["texto"] == "feito pelo agy" and r["assinatura"] == "agy" and r["tokens_in"] == 100, r
    r = executa(ags["qa"], "revise", "agy")
    assert r["ok"] and r["assinatura"] == "agy" and r["modelo"] == "gemini-4-argon-high", r
    for modo, esperado in (("erro", "SUCCESS"), ("sai1", "boom")):
        os.environ["FAKE_MODO"] = modo
        r = executa(ags["writer"], "x", "agy")
        assert not r["ok"] and r["erro"], (modo, r)
    os.environ["FAKE_TIPO"] = "claude"
    os.environ["FAKE_MODO"] = "erro"
    r = executa(ags["design"], "x")
    assert not r["ok"] and "falhou" in r["erro"], r
    os.environ["FAKE_MODO"] = "dorme"
    t0 = time.monotonic()
    r = executa(ags["design"], "x", timeout=2)
    assert not r["ok"] and "sem resposta" in r["erro"] and time.monotonic() - t0 < 20, r
    os.environ["FAKE_MODO"] = "ok"
    r = executa(ags["design"], "x" * (MAX_TAREFA + 1))
    assert not r["ok"] and "grande demais" in r["erro"], r
    # dentro do MAX_TAREFA, mas a linha estoura depois do escape do Windows (cada " vira \"): recusa antes de lancar o agy
    r = executa(ags["writer"], '"' * 15000, "agy")
    assert not r["ok"] and "grande demais" in r["erro"] and "linha de comando" in r["erro"], r
    # agy/claude que resolve para .cmd/.bat: recusa (o cmd.exe reinterpretaria & e " da tarefa)
    cmd_falso = tmp / "agy.cmd"
    cmd_falso.write_text("@echo off\n", encoding="utf-8")
    agy_antes = os.environ["AGY_CMD"]
    os.environ["AGY_CMD"] = str(cmd_falso).replace("\\", "/")
    r = executa(ags["writer"], "ajuste & del x", "agy")
    assert not r["ok"] and ".cmd" in r["erro"], r
    os.environ["CLAUDE_CMD"], claude_antes = os.environ["AGY_CMD"], os.environ["CLAUDE_CMD"]
    r = executa(ags["design"], "ajuste", "claude")
    assert not r["ok"] and ".cmd" in r["erro"], r
    os.environ["AGY_CMD"], os.environ["CLAUDE_CMD"] = agy_antes, claude_antes
    linhas = [json.loads(x) for x in LOG.read_text(encoding="utf-8").splitlines()]
    assert len(linhas) >= 7 and {x["agente"] for x in linhas} >= {"design", "writer", "qa"} and all("ts" in x and "ok" in x for x in linhas), linhas

    # recursao: dentro de um agente nao chama outro; CLI ruim e tratada
    os.environ["FEDORA_AGENTE"] = "senior"
    assert main(["junior", "x"]) == 3
    del os.environ["FEDORA_AGENTE"]
    assert main(["inexistente", "x"]) == 2 and main(["junior"]) == 2

    # o prompt do modo equipe sai com o comando real, e esse comando e reconhecido como delegacao pelos dois motores
    import commands
    bloco = commands.system_block(equipe=True)
    assert "{AGENTES}" not in bloco and "agentes.py" in bloco and "{AGENTES}" not in commands.system_block()
    assert "agentes.py" not in commands.system_block(), "fora do modo equipe o prompt nao fala de agentes"
    assert runtime.delegacao_em(f'{commands.comando_agentes()} writer "ajuste"')[0] == "writer"
    print("agentes ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
