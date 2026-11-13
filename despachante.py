"""
Despachante de backlog do cofre.

Cada atividade e um arquivo `backlog/<projeto>/[<grupo>/]<NNNN>-<slug>.md` no cofre, com `status` no frontmatter. O `grupo`
e a frente de trabalho (ex.: `deploy-guide-primeira-versao`): subpasta com os cards de codigo, docs e release do mesmo trabalho.
A numeracao e do projeto (nao reinicia por grupo). O despachante
vigia a pasta e, a cada mudanca, roda o agente da vez (agentes.py) e grava o proximo estado:

    novo ------techlead: AGENTE: x----> pronto ---x: OK---> em_revisao ---qa: APROVADO---> concluido
    escalado --techlead: AGENTE: y----> pronto              em_revisao ---qa: REPROVADO--> pronto (ate MAX_TENTATIVAS)
    pronto ----x: ESCALAR-------------> escalado            (o senior escalando vai direto para bloqueado)

`bloqueado` e `concluido` sao finais: o despachante nao mexe mais (para destravar, o usuario troca o status na mao).
Vira `bloqueado`: o tech lead devolver ESCALAR, resposta sem a linha do contrato (FALHA), resposta que nao cabe no estado,
`agente` fora de DEVS, MAX_TENTATIVAS reprovacoes ou MAX_RODADAS passos (freio contra laco que gasta cota).
Falha passageira (rede, cota, 529: falha_transitoria) nao bloqueia: o card fica no status e tenta de novo ate MAX_TRANSITORIAS seguidas.

A equipe roda no motor ativo (Claude ou agy) com os agentes especializados da Fedora.
O despachante nao da permissao a ninguem: quem roda e o agentes.py, com as ferramentas de cada funcao e a runtime.DENY.

    <python> despachante.py              vigia o backlog (Ctrl+C para)
    <python> despachante.py --uma-vez    varre ate estabilizar e sai
    <python> despachante.py --teste      autoteste offline
"""
import datetime
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import unicodedata

import frontmatter
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import memory

RAIZ = pathlib.Path(__file__).resolve().parent
AGENTES_PY = RAIZ / "agentes.py"
COFRE = pathlib.Path(os.environ.get("COFRE") or os.environ.get("JARVIS_VAULT") or memory.VAULT).expanduser()
BACKLOG = COFRE / "backlog"

DEVS = {"junior", "writer", "design", "senior"}
# quem roda em cada estado; None = o agente gravado no card (`agente:`), que tem de estar em DEVS
QUEM_RODA = {"novo": "techlead", "escalado": "techlead", "pronto": None, "em_revisao": "qa"}
RETORNO = re.compile(r"^(OK|ESCALAR|APROVADO|REPROVADO|AGENTE):\s*(.*)$")

MAX_TENTATIVAS = 3     # reprovacoes do qa antes de bloquear
MAX_RODADAS = 12       # passos de um card antes de bloquear (laco techlead <-> dev nao gasta cota para sempre)
MAX_CORPO = 16000      # caracteres do card na tarefa (vai por stdin; com cabecalho e rodape cabe no agentes.MAX_TAREFA de 20000)
MAX_DETALHE = 1200     # caracteres da resposta guardados no Historico
MAX_DETALHE_FALHA = 300   # detalhe de falha (FALHA ou agente que nao rodou) no Historico
MAX_TRANSITORIAS = 3   # falhas passageiras seguidas (rede, cota, 529) antes de bloquear
TRANSITORIAS = ("no such host", "dial tcp", "timeout", "timed out", "429", "503", "529", "overloaded", "rate limit",
                "quota", "usage limit", "connection reset", "connection aborted", "winerror 10053", "10054", "10060",
                # o Gemini as vezes tenta um comando fora da lista, e o agy termina sem resposta; repetir costuma passar (0030, 0031, 0039)
                "negou uma acao")
AUTO_FALHA = re.compile(r"^\S+ falhou \((claude|agy)\):")   # ESCALAR gerado pelo agentes.py quando o agente nao rodou
TIMEOUT = int(os.environ.get("FEDORA_AGENTE_TIMEOUT", "300")) + 90   # o agentes.py ja corta o agente em TIMEOUT+30
LOCK_VELHO = TIMEOUT + 300   # trava mais velha que isso e de um despachante que morreu

# o agente roda so os autotestes python de cada modulo (agentes.py AUTOTESTES); PowerShell nao e liberado a ninguem.
# Sem este aviso o card que manda "rodar tools/testa.ps1" volta ESCALAR pedindo aprovacao que ninguem pode dar.
NOTA_TESTES = ("Voce nao roda `tools/testa.ps1` (PowerShell nao e liberado): rode so o autoteste do modulo que alterou "
               "(`python <modulo>.py --teste`) e diga no retorno que o `tools/testa.ps1` completo fica para o usuario.")

PAPEL = {
    "techlead": ("Faca a triagem desta atividade do backlog e escolha o agente executor.",
                 "AGENTE: <junior|writer|design|senior>` ou `ESCALAR: <motivo em uma frase>"),
    "qa": ("Revise o que foi feito para esta atividade do backlog contra o pedido e os criterios de aceite (leia os arquivos com as ferramentas de leitura de arquivo).",
           "APROVADO: <resumo em uma frase>` ou `REPROVADO: <o que corrigir em uma frase>"),
    None: ("Execute esta atividade do backlog. Se o Historico tiver um REPROVADO, corrija o que o qa apontou.",
           "OK: <resumo em uma frase>` ou `ESCALAR: <motivo em uma frase>"),
}


# ---------------------------------------------------------------- utilitarios

def agora():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _int(v):
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def _slug(texto, padrao="atividade"):
    s = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:50].strip("-") or padrao


def ler(arq):
    return frontmatter.loads(pathlib.Path(arq).read_text(encoding="utf-8-sig"))


_PROPRIAS = {}   # caminho -> mtime_ns da ultima gravacao do proprio despachante
_PROPRIAS_LOCK = threading.Lock()


def _marcar_propria(arq):
    try:
        mt = os.stat(arq).st_mtime_ns
    except OSError:
        return
    with _PROPRIAS_LOCK:
        _PROPRIAS[os.path.normcase(os.path.abspath(arq))] = mt


def _gravacao_propria(caminho):
    """True se o arquivo ainda esta como o despachante gravou (evento do watchdog vindo da propria gravacao)."""
    chave = os.path.normcase(os.path.abspath(caminho))
    with _PROPRIAS_LOCK:
        mt = _PROPRIAS.get(chave)
    if mt is None:
        return False
    try:
        return os.stat(caminho).st_mtime_ns == mt
    except OSError:
        return False


def salvar(arq, post):
    """Grava atomico: escreve no .tmp e troca. O leitor (Obsidian, outro despachante) nunca ve o arquivo pela metade."""
    arq = pathlib.Path(arq)
    tmp = arq.with_name(arq.name + ".tmp")
    tmp.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    for i in range(5):   # no Windows a troca falha enquanto outro programa esta com o arquivo aberto
        try:
            os.replace(tmp, arq)
            _marcar_propria(arq)
            return
        except PermissionError:
            if i == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.2)


def ler_retorno(saida):
    """(tipo, msg) da ULTIMA linha do contrato na resposta; sem nenhuma, ("FALHA", motivo)."""
    linhas = (saida or "").strip().splitlines()
    for linha in reversed(linhas):
        m = RETORNO.match(linha.strip().strip("`*").strip())
        if m:
            return m.group(1), m.group(2).strip().strip("`*").strip()
    return "FALHA", ("resposta sem a linha do contrato: " + linhas[-1].strip()[:200]) if linhas else "resposta vazia"


def falha_transitoria(texto):
    """True se o texto de uma falha aponta causa passageira (rede, cota, sobrecarga)."""
    t = str(texto or "").lower()
    # codigo numerico so vale isolado (7529 ms ou um id com 429 no meio nao contam)
    return any(re.search(rf"(?<![\w.]){p}(?![\w.])", t) if p.isdigit() else p in t for p in TRANSITORIAS)


def _resumo_json(js):
    """Campo de texto/erro de um JSON do CLI (inteiro ou cortado); '' se nao achar."""
    try:
        import json
        d = json.loads(js)
        if isinstance(d, dict):
            for k in ("result", "error", "short_error", "message", "text"):
                v = d.get(k)
                if isinstance(v, dict):
                    v = v.get("message") or v.get("text")
                if isinstance(v, str) and v.strip():
                    return v
    except ValueError:
        pass
    m = re.search(r'"(?:result|error|short_error|message|text)"\s*:\s*"((?:[^"\\]|\\.)*)"', js)
    return m.group(1) if m and m.group(1).strip() else ""


def resumir(texto, n=MAX_DETALHE_FALHA):
    """Detalhe limpo de falha: JSON do CLI vira o campo de texto dele; senao o fim do texto. No maximo n caracteres."""
    s = str(texto or "").strip()
    m = re.match(r"^((?:[^{\n]{0,120}?:)?)\s*(\{.*)$", s, re.S)   # "senior falhou (claude): {json}" ou so "{json}"
    if m:
        cab, js = m.group(1).strip(), m.group(2)
        corpo = _resumo_json(js) or "(JSON do CLI omitido)"
        s = f"{cab} {corpo}".strip()
        s = " ".join(s.split())
        return s[:n]
    return s if len(s) <= n else s[-n:]


# ---------------------------------------------------------------- agente

AVISO_CARD = "O conteudo do card ja esta abaixo; nao releia o arquivo do card."


def _corpo_triagem(corpo):
    """So o que a triagem precisa: Pedido, Criterios de aceite e a ultima entrada do Historico."""
    secoes, atual = {}, None
    for linha in corpo.splitlines():
        m = re.match(r"^## (.+?)\s*$", linha)   # titulo so no inicio de linha (o Pedido pode citar "## Historico")
        if m:
            atual = m.group(1)
            secoes.setdefault(atual, [])
        elif atual is not None:
            secoes[atual].append(linha)
    if "Pedido" not in secoes:   # card fora do formato: vai inteiro (o corte do MAX_CORPO vale do mesmo jeito)
        return corpo
    partes = [f"## {t}\n" + "\n".join(secoes[t]).strip() for t in ("Pedido", "Critérios de aceite") if t in secoes]
    ultima = next((l.strip() for l in reversed(secoes.get("Histórico", [])) if l.startswith("- ")), "")
    partes.append("## Histórico (ultima linha)\n" + (ultima or "(vazio)"))
    return "\n\n".join(partes)


def tarefa_de(agente, arq, post):
    oque, contrato = PAPEL.get(agente, PAPEL[None])
    corpo = (post.content or "").strip()
    if agente == "techlead":
        corpo = _corpo_triagem(corpo)
    if len(corpo) > MAX_CORPO:   # so o Historico e aparado (vale o fim dele); o que vem antes fica intacto
        # ultimo titulo no inicio de linha (o Pedido pode citar o texto)
        i = corpo.rfind("\n## Histórico")
        i = i + 1 if i >= 0 else (0 if corpo.startswith("## Histórico") else -1)
        if i >= 0:
            cab, hist = corpo[:i + len("## Histórico")], corpo[i + len("## Histórico"):]
            sobra = max(0, MAX_CORPO - len(cab) - 10)
            linhas, fim = hist.splitlines(), []
            while linhas and len("\n".join([linhas[-1]] + fim)) <= sobra:
                fim.insert(0, linhas.pop())
            if not fim:   # ultima linha enorme: guarda ao menos o final dela
                fim = [hist[-sobra:]] if sobra else []
            corpo = cab + "\n[...]\n" + "\n".join(fim).lstrip("\n")
        else:   # sem Historico: comeco e fim, como antes
            corpo = corpo[:MAX_CORPO // 2] + "\n[...]\n" + corpo[-MAX_CORPO // 2:]
    return (f"{oque}\nArquivo da atividade (so leitura; quem grava o estado e o despachante): {pathlib.Path(arq).as_posix()}\n"
            f"{AVISO_CARD}\nProjeto: {post.get('projeto', '')}\nTitulo: {post.get('titulo', '')}\n\n{corpo}\n\n"
            f"{'' if agente == 'techlead' else NOTA_TESTES + chr(10)}"
            f"Termine com uma unica linha final no formato do contrato: `{contrato}`")


def rodar(agente, arq):
    """Roda o agente pelo agentes.py e devolve o texto que ele imprimiu. Erro ao rodar vira texto sem contrato (FALHA)."""
    tarefa = tarefa_de(agente, arq, ler(arq))
    # FEDORA_MOTOR_TURNO so passa se existir; sem ela o agentes.py le o estado gravado da regra
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:   # a tarefa vai por stdin ("-"): sem limite da linha de comando
        proc = subprocess.Popen([sys.executable, str(AGENTES_PY), agente, "-"], cwd=RAIZ, env=env, text=True,
                                encoding="utf-8", errors="replace", stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, ValueError) as e:
        return f"nao consegui rodar o agente {agente}: {e}"
    try:
        out, err = proc.communicate(input=tarefa, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        import runtime
        runtime.kill_tree(proc)   # mata a arvore inteira (no Windows o kill() deixaria o CLI do agente vivo)
        proc.communicate()
        return f"agente {agente} sem resposta em {TIMEOUT}s"
    return (out or "").strip() or f"agente {agente} saiu com codigo {proc.returncode}: {(err or '').strip()[-300:]}"


# ---------------------------------------------------------------- estados

def _historico(post, agente, tipo, msg, para, detalhe=""):
    corpo = (post.content or "").rstrip()
    if "## Histórico" not in corpo:
        corpo += "\n\n## Histórico"
    corpo += f"\n- {agora()} `{agente}` {tipo}: {' '.join(str(msg).split())[:400]} -> `{para}`"
    detalhe = (detalhe or "").strip()
    if detalhe:
        if len(detalhe) > MAX_DETALHE:
            detalhe = "[...] " + detalhe[-MAX_DETALHE:]
        corpo += "\n" + "\n".join("  > " + linha for linha in detalhe.splitlines())
    post.content = corpo


def transicionar(post, agente, tipo, msg, detalhe="", transitoria=False):
    """Aplica a resposta do agente ao card (status, agente, tentativas, rodadas, Historico) e devolve o novo status.
    Tudo que nao cabe no estado atual fecha em `bloqueado`: na duvida o card para e espera o usuario.
    `transitoria` (falha passageira, so FALHA ou ESCALAR do proprio agentes.py): o card fica onde esta ate MAX_TRANSITORIAS seguidas."""
    de = post.get("status")
    para, motivo = "bloqueado", ""
    transitoria = transitoria and tipo in ("FALHA", "ESCALAR") and de in QUEM_RODA
    if transitoria:
        post["transitorias"] = _int(post.get("transitorias")) + 1
        msg, detalhe = resumir(msg, 200), ""
        if post["transitorias"] >= MAX_TRANSITORIAS:
            motivo = "falha transitória repetida"
        else:
            para = de
    else:
        post["transitorias"] = 0   # passo que nao e transitorio zera a sequencia
    if transitoria or tipo == "FALHA":
        pass
    elif de in ("novo", "escalado"):
        escolhido = msg.strip().strip("`*.").lower()
        if tipo == "AGENTE" and escolhido in DEVS:
            if de == "escalado" and escolhido == post.get("agente"):
                motivo = f"o tech lead devolveu a atividade ao mesmo agente que escalou ({escolhido})"
            else:
                post["agente"], para = escolhido, "pronto"
        elif tipo == "AGENTE":
            motivo = f"o tech lead escolheu um agente que nao executa: {escolhido!r}"
        elif tipo != "ESCALAR":
            motivo = f"resposta {tipo} nao vale na triagem"
    elif de == "pronto":
        if tipo == "OK":
            para = "em_revisao"
        elif tipo == "ESCALAR":
            para = "bloqueado" if agente == "senior" else "escalado"   # acima do senior so o usuario
        else:
            motivo = f"resposta {tipo} nao vale para quem executa"
    elif de == "em_revisao":
        if tipo == "APROVADO":
            para = "concluido"
        elif tipo == "REPROVADO":
            post["tentativas"] = _int(post.get("tentativas")) + 1
            if post["tentativas"] >= MAX_TENTATIVAS:
                motivo = f"reprovado {post['tentativas']} vezes"
            else:
                para = "pronto"
        else:
            motivo = f"resposta {tipo} nao vale na revisao"
    else:
        motivo = f"status {de!r} nao e do despachante"
    post["rodadas"] = _int(post.get("rodadas")) + 1
    if para not in ("concluido", "bloqueado") and post["rodadas"] >= MAX_RODADAS:
        para, motivo = "bloqueado", f"{post['rodadas']} rodadas sem concluir"
    _historico(post, agente, tipo, f"{msg} [{motivo}]" if motivo else msg, para, detalhe)
    post["status"] = para
    post["atualizado"] = agora()
    return para


def _trava(arq):
    """Cria <arquivo>.lock (O_EXCL: so um ganha). False se outro despachante esta com o card."""
    lock = arq.with_name(arq.name + ".lock")
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {agora()}".encode("ascii"))
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime < LOCK_VELHO:
                    return None
                lock.unlink()   # trava de um despachante que morreu no meio
            except OSError:
                return None
        except OSError:
            return None
    return None


def processar(arq):
    """Da UM passo no card. True se o status mudou."""
    arq = pathlib.Path(arq)
    lock = _trava(arq)
    if lock is None:
        return False
    try:
        try:
            post = ler(arq)   # le DEPOIS da trava: o estado e o que esta gravado agora
        except Exception as e:   # frontmatter quebrado, arquivo sumiu: nao e card valido
            print(f"despachante: ignorando {arq.name}: {(str(e).splitlines() or [type(e).__name__])[0][:200]}", flush=True)
            return False
        de = post.get("status")
        if de not in QUEM_RODA:
            return False
        agente = QUEM_RODA[de] or str(post.get("agente") or "").strip().lower()
        if QUEM_RODA[de] is None and agente not in DEVS:
            tipo, msg, saida = "FALHA", f"agente {agente!r} nao e executor (aceitos: {', '.join(sorted(DEVS))})", ""
            agente = "despachante"
        else:
            print(f"despachante: {arq.name} [{de}] -> {agente}", flush=True)
            saida = rodar(agente, arq)
            tipo, msg = ler_retorno(saida)
        post = ler(arq)   # o agente pode ter demorado: nao grava por cima de edicao do usuario no corpo
        if post.get("status") != de:
            print(f"despachante: {arq.name} mudou de status durante a rodada; resposta descartada", flush=True)
            return True
        guarda = tipo in ("FALHA", "ESCALAR", "REPROVADO")   # o porque fica no card para o proximo agente e o usuario
        detalhe = saida if guarda else ""
        # falha de execucao (sem contrato, ou ESCALAR gerado pelo agentes.py): detalhe curto e sem JSON bruto
        auto = tipo == "FALHA" or (tipo == "ESCALAR" and bool(AUTO_FALHA.match(msg)))
        if auto:   # classifica pelo texto resumido: o JSON bruto (ids, custos, tempos) nao conta
            msg, detalhe = resumir(msg), resumir(saida)
        transitoria = auto and falha_transitoria(f"{msg}\n{detalhe}")
        para = transicionar(post, agente, tipo, msg, detalhe, transitoria)
        salvar(arq, post)
        print(f"despachante: {arq.name} {tipo} -> {para}", flush=True)
        # falha transitoria que deixou o card onde estava nao conta como mudanca: a nova tentativa fica para o
        # proximo ciclo do vigiar (60 s), em vez de queimar MAX_TRANSITORIAS em sequencia na mesma varredura
        return not (transitoria and para == de)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def cards():
    return sorted(p for p in BACKLOG.rglob("*.md") if p.is_file()) if BACKLOG.is_dir() else []


def varrer():
    """Avanca todos os cards ate nenhum mudar mais (novo -> pronto -> em_revisao -> concluido sem esperar evento).
    Termina sempre: cada passo soma `rodadas` e MAX_RODADAS bloqueia. Devolve quantos passos deu."""
    passos = 0
    while True:
        mudou = 0
        for arq in cards():
            try:
                mudou += bool(processar(arq))
            except Exception as e:   # um card ruim nao derruba os outros
                print(f"despachante: erro em {arq.name}: {e}", flush=True)
        passos += mudou
        if not mudou:
            return passos


def _bloco_relacionados(links):
    if isinstance(links, (str, pathlib.Path)):
        links = (links,)
    itens = []
    for x in links or ():
        s = pathlib.Path(x).stem if isinstance(x, pathlib.Path) or str(x).endswith(".md") else str(x).strip()
        s = s.strip("[]").strip()
        if s and f"[[{s}]]" not in itens:
            itens.append(f"[[{s}]]")
    return ("\n\n## Relacionados\n" + "\n".join(f"- {w}" for w in itens)) if itens else ""


def _proximo_numero(raiz):
    """Maior NNNN de qualquer card sob `raiz` (inclui os grupos) + 1. A numeracao e do projeto, nao do grupo:
    o nome do arquivo (usado nos wikilinks do Obsidian) continua unico mesmo com o card em outra pasta."""
    nums = [int(m.group(1)) for p in raiz.rglob("*") if p.is_file() and (m := re.match(r"(\d+)-", p.name))]
    return max(nums, default=0) + 1


def criar_atividade(projeto, titulo, pedido, criterios="", relacionados=(), grupo=""):
    """Cria backlog/<projeto>[/<grupo>]/<NNNN>-<slug>.md com status novo; numero = maior do projeto + 1. Devolve o caminho.
    `grupo` e a frente de trabalho a que o card pertence (ex.: "deploy guide primeira versao"): vira subpasta e o campo `grupo`.
    Os cards de um mesmo trabalho (codigo, docs, release) vao no mesmo grupo; sem grupo o card fica na raiz do projeto."""
    raiz = BACKLOG / _slug(projeto, "geral")
    pasta = raiz / _slug(grupo) if str(grupo or "").strip() else raiz
    pasta.mkdir(parents=True, exist_ok=True)
    rel = _bloco_relacionados(relacionados)
    meta = dict(status="novo", projeto=raiz.name)
    if pasta != raiz:
        meta["grupo"] = pasta.name
    post = frontmatter.Post(
        f"## Pedido\n{str(pedido).strip()}\n\n## Critérios de aceite\n{str(criterios).strip() or '(nao informados)'}{rel}\n\n## Histórico",
        titulo=str(titulo).strip(), agente="", tentativas=0, rodadas=0, criado=agora(), atualizado=agora(), **meta)
    for _ in range(50):
        arq = pasta / f"{_proximo_numero(raiz):04d}-{_slug(titulo)}.md"
        try:
            os.close(os.open(arq, os.O_CREAT | os.O_EXCL | os.O_WRONLY))   # reserva o numero: dois criadores nao colidem
        except FileExistsError:
            continue
        salvar(arq, post)
        return arq
    raise RuntimeError(f"nao consegui reservar um numero em {pasta}")


def mover_para_grupo(arq, grupo):
    """Move um card existente para backlog/<projeto>/<grupo>/ e grava o campo `grupo`; o nome do arquivo nao muda,
    entao os wikilinks continuam valendo. Recusa (ValueError) card rodando agora (trava fresca) ou nome ja ocupado no destino.
    Trava velha de despachante morto vai junto com o card. Devolve o novo caminho."""
    arq = pathlib.Path(arq)
    rel = arq.relative_to(BACKLOG)
    raiz = BACKLOG / rel.parts[0]
    destino = raiz / _slug(grupo, "geral")
    novo = destino / arq.name
    if novo == arq:
        return arq
    lock = arq.with_name(arq.name + ".lock")
    try:
        if time.time() - lock.stat().st_mtime < LOCK_VELHO:
            raise ValueError(f"{arq.name} esta rodando agora (trava fresca); tente depois")
    except FileNotFoundError:
        lock = None
    if novo.exists():
        raise ValueError(f"{novo} ja existe")
    destino.mkdir(parents=True, exist_ok=True)
    post = ler(arq)
    post["grupo"] = destino.name
    salvar(arq, post)   # grava o campo antes de mover: o arquivo nunca aparece nos dois lugares
    os.replace(arq, novo)
    if lock is not None:
        os.replace(lock, novo.with_name(novo.name + ".lock"))
    return novo


def conectar_atividades(*arqs, extras=()):
    """Liga os cards de um mesmo trabalho entre si (e a notas extras, ex.: Hub) com wikilinks nativos [[stem]] no Obsidian."""
    if isinstance(extras, (str, pathlib.Path)):
        extras = (extras,)
    caminhos = [pathlib.Path(a) for a in arqs]
    for arq in caminhos:
        post = ler(arq)
        outros = [p.stem for p in caminhos if p != arq] + list(extras or ())
        bloco = _bloco_relacionados(outros)
        if not bloco:
            continue
        corpo = post.content or ""
        if "## Relacionados" in corpo:
            for item in bloco.splitlines()[3:]:
                if item not in corpo:
                    corpo = corpo.replace("## Relacionados", f"## Relacionados\n{item}", 1)
        elif "## Histórico" in corpo:
            corpo = corpo.replace("\n\n## Histórico", f"{bloco}\n\n## Histórico", 1)
        else:
            corpo = corpo.rstrip() + bloco
        post.content = corpo
        salvar(arq, post)


# ---------------------------------------------------------------- resumo para o quadro da UI (so leitura)

# coluna do quadro por status; o que depende do usuario (bloqueado, para_decisao) vai para "voce"
COLUNA = {"adiado": "triagem", "novo": "triagem", "escalado": "triagem", "pronto": "exec", "aguardando_arquiteto": "exec",
          "em_revisao": "qa", "concluido": "feito", "bloqueado": "voce", "para_decisao": "voce"}
ROTULO = {"adiado": "adiado (por último)", "novo": "na fila", "escalado": "escalado", "pronto": "na fila", "aguardando_arquiteto": "na fila",
          "em_revisao": "na fila", "concluido": "concluído", "bloqueado": "bloqueado", "para_decisao": "aguarda você"}


def _epoca(valor, padrao):
    try:
        return datetime.datetime.strptime(str(valor)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return padrao


def resumo_backlog(max_feitos=6):
    """O backlog como o quadro precisa: os cards abertos (todos) e os `max_feitos` concluidos mais recentes.
    `ativo` = ha uma trava fresca (o despachante esta com o card agora); `quem` = o agente da vez. Nao escreve nada."""
    abertos, feitos, total_feitos = [], [], 0
    agora_t = time.time()
    for arq in cards():
        try:
            post = ler(arq)
            mtime = arq.stat().st_mtime
        except Exception:   # frontmatter quebrado ou arquivo sumindo: nao e card para o quadro
            continue
        if not isinstance(post.metadata, dict) or not post.get("status"):
            continue
        status = str(post.get("status")).strip()
        agente = str(post.get("agente") or "").strip().lower()
        try:
            ativo = agora_t - arq.with_name(arq.name + ".lock").stat().st_mtime < LOCK_VELHO
        except OSError:
            ativo = False
        ultimo = next((l.strip() for l in reversed((post.content or "").splitlines()) if l.startswith("- 20")), "")
        partes = arq.relative_to(BACKLOG).parts   # (projeto, [grupo...], arquivo): a pasta e quem manda
        card = dict(
            slug=arq.stem, id=arq.stem.split("-", 1)[0], projeto=partes[0] if len(partes) > 1 else arq.parent.name,
            grupo="/".join(partes[1:-1]), status=status,
            coluna=COLUNA.get(status, "voce"), agente=agente,
            quem=("arquiteto" if status == "aguardando_arquiteto" else QUEM_RODA.get(status) or agente),
            ativo=ativo, rotulo="rodando" if ativo else ROTULO.get(status, status),
            titulo=str(post.get("titulo") or arq.stem), rodadas=_int(post.get("rodadas")),
            at=_epoca(post.get("atualizado"), mtime), ultimo=ultimo[:160])
        if status == "concluido":
            total_feitos += 1
            feitos.append(card)
        else:
            abertos.append(card)
    abertos.sort(key=lambda c: c["at"], reverse=True)
    feitos.sort(key=lambda c: c["at"], reverse=True)
    return dict(cards=abertos + feitos[:max(0, max_feitos)], feitos=total_feitos, abertos=len(abertos))


# ---------------------------------------------------------------- vigia

class _Vigia(FileSystemEventHandler):
    """So avisa que algo mudou; quem varre e o laco principal (um passo por vez, sem prender a thread do watchdog)."""

    def __init__(self, sinal):
        self.sinal = sinal

    def on_any_event(self, event):
        if event.is_directory:
            return
        mds = [str(c) for c in (event.src_path, getattr(event, "dest_path", "")) if str(c or "").endswith(".md")]
        # gravacao do proprio despachante (ex.: contador de falha transitoria) nao acorda a varredura:
        # a nova tentativa espera o ciclo de 60 s
        if mds and not all(_gravacao_propria(c) for c in mds):
            self.sinal.set()


def vigiar():
    BACKLOG.mkdir(parents=True, exist_ok=True)
    sinal = threading.Event()
    obs = Observer()
    obs.schedule(_Vigia(sinal), str(BACKLOG), recursive=True)
    obs.daemon = True
    obs.start()
    print(f"despachante: vigiando {BACKLOG} (Ctrl+C para)", flush=True)
    try:
        varrer()
        while True:
            if sinal.wait(timeout=60):   # sem evento em 60 s varre assim mesmo (rede/sincronia podem engolir evento)
                time.sleep(1.0)          # espera o editor terminar de gravar
            sinal.clear()
            varrer()
    except KeyboardInterrupt:
        print("despachante: parando", flush=True)
    finally:
        obs.stop()
        obs.join(timeout=5)
    return 0


def main(argv):
    if argv and argv[0] == "--teste":
        return _teste()
    if os.environ.get("FEDORA_AGENTE"):   # agente nao chama agente: o despachante tambem nao roda de dentro de um
        print("ESCALAR: o despachante nao roda de dentro de um agente")
        return 3
    if not COFRE.is_dir():
        print(f"despachante: cofre nao encontrado em {COFRE} (defina COFRE ou JARVIS_VAULT)")
        return 2
    if argv and argv[0] == "--uma-vez":
        print(f"despachante: {varrer()} passo(s) em {BACKLOG}")
        return 0
    if argv:
        print("uso: despachante.py [--uma-vez | --teste]")
        return 2
    return vigiar()


# ---------------------------------------------------------------- autoteste

def _teste():
    global BACKLOG, AGENTES_PY, rodar
    import shutil
    import tempfile
    orig = BACKLOG, AGENTES_PY, rodar
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="despachante-teste-"))
    try:
        BACKLOG = tmp / "backlog"

        # contrato
        assert ler_retorno("fiz isso\nOK: pronto") == ("OK", "pronto")
        assert ler_retorno("APROVADO\n- item\n`REPROVADO: falta teste`\n") == ("REPROVADO", "falta teste")
        assert ler_retorno("OK: antigo\ntexto\n**AGENTE: junior**") == ("AGENTE", "junior")
        assert ler_retorno("ESCALAR:sem espaco") == ("ESCALAR", "sem espaco")
        for ruim in ("", "   \n", "tudo certo", "ok: minusculo", "RESULTADO OK: x", "OKAY: x"):
            assert ler_retorno(ruim)[0] == "FALHA", ruim

        # criar_atividade: numero = maior da pasta + 1, slug sem acento, projeto nao sai do backlog
        a1 = criar_atividade("Fedora", "Corrigir ação do botão", "Troque o texto.", "Texto novo aparece.")
        assert a1 == BACKLOG / "fedora" / "0001-corrigir-acao-do-botao.md", a1
        p = ler(a1)
        assert p["status"] == "novo" and p["projeto"] == "fedora" and p["titulo"] == "Corrigir ação do botão" and p["tentativas"] == 0
        assert "## Pedido\nTroque o texto." in p.content and "Texto novo aparece." in p.content and "## Histórico" in p.content
        (BACKLOG / "fedora" / "0007-solto.md").write_text("---\nstatus: concluido\n---\nx\n", encoding="utf-8")
        a_rel = criar_atividade("fedora", "Outra", "y", relacionados=["Fedora — Hub"])
        assert a_rel.name == "0008-outra.md" and "[[Fedora — Hub]]" in ler(a_rel).content
        fora = criar_atividade("../../fora", "???", "z")
        conectar_atividades(a_rel, fora, extras=["Nota Extra"])
        assert "[[0001-atividade]]" in ler(a_rel).content and "[[0008-outra]]" in ler(fora).content

        # resumo_backlog (quadro da UI): coluna por status, trava fresca = rodando, concluidos limitados, so leitura
        for n, st in ((1, "concluido"), (2, "para_decisao")):
            q = ler(a1)
            q["status"] = st
            salvar(BACKLOG / "fedora" / f"{n:04d}-q{n}.md", q)
        trava = a_rel.with_name(a_rel.name + ".lock")
        trava.write_text("1 x", encoding="ascii")
        r = resumo_backlog(max_feitos=1)
        por = {c["slug"]: c for c in r["cards"]}
        assert por["0008-outra"]["coluna"] == "triagem" and por["0008-outra"]["ativo"] and por["0008-outra"]["rotulo"] == "rodando"
        assert por["0008-outra"]["quem"] == "techlead" and por["0002-q2"]["coluna"] == "voce" and por["0002-q2"]["rotulo"] == "aguarda você"
        assert r["feitos"] == 2 and sum(c["coluna"] == "feito" for c in r["cards"]) == 1 and r["abertos"] == len(r["cards"]) - 1
        trava.unlink()
        assert not [c for c in resumo_backlog()["cards"] if c["slug"] == "0008-outra" and c["ativo"]]
        for extra in (BACKLOG / "fedora" / "0001-q1.md", BACKLOG / "fedora" / "0002-q2.md"):
            extra.unlink()
        assert "[[Nota Extra]]" in ler(a_rel).content and "[[Nota Extra]]" in ler(fora).content
        assert fora.parent == BACKLOG / "fora" and fora.name == "0001-atividade.md", fora
        assert not list(BACKLOG.rglob("*.tmp")) and not list(BACKLOG.rglob("*.lock"))
        for arq in cards():
            if arq != a1:
                arq.unlink()

        # grupos: subpasta por frente de trabalho; numero do projeto (nao do grupo); quadro e despachante enxergam a subpasta
        g1 = criar_atividade("fedora", "Pre-voo", "p", grupo="Deploy guide: primeira versão")
        g2 = criar_atividade("fedora", "Changelog", "c", grupo="Deploy guide: primeira versão")
        g3 = criar_atividade("fedora", "Outro assunto", "o", grupo="Fala em etapas")
        assert g1 == BACKLOG / "fedora" / "deploy-guide-primeira-versao" / "0002-pre-voo.md", g1
        assert g2.name == "0003-changelog.md" and g2.parent == g1.parent and g3.name == "0004-outro-assunto.md", (g2, g3)
        assert ler(g1)["grupo"] == "deploy-guide-primeira-versao" and ler(g1)["projeto"] == "fedora" and "grupo" not in ler(a1).metadata
        assert criar_atividade("fedora", "Raiz", "r").name == "0005-raiz.md"   # sem grupo: raiz do projeto, numero segue o projeto
        assert criar_atividade("fedora", "Escapa", "e", grupo="../../fora").parent == BACKLOG / "fedora" / "fora"   # grupo nao sai do projeto
        por = {c["slug"]: c for c in resumo_backlog()["cards"]}
        assert por["0002-pre-voo"]["grupo"] == "deploy-guide-primeira-versao" and por["0002-pre-voo"]["projeto"] == "fedora"
        assert por["0001-corrigir-acao-do-botao"]["grupo"] == "" and por["0005-raiz"]["grupo"] == ""
        assert g1 in cards() and g3 in cards()   # o despachante varre as subpastas

        # mover_para_grupo: leva o card e o campo, mantem o nome, recusa card rodando e colisao
        m = mover_para_grupo(a1, "Fala em etapas")
        assert m == BACKLOG / "fedora" / "fala-em-etapas" / a1.name and not a1.exists() and ler(m)["grupo"] == "fala-em-etapas", m
        assert ler(m)["status"] == "novo" and "## Pedido" in ler(m).content
        assert mover_para_grupo(m, "fala em etapas") == m   # ja esta la
        trava = m.with_name(m.name + ".lock")
        trava.write_text("1 x", encoding="ascii")
        try:
            mover_para_grupo(m, "outro")
            assert False, "moveu card com trava fresca"
        except ValueError:
            assert m.exists()
        velho = time.time() - LOCK_VELHO - 10
        os.utime(trava, (velho, velho))
        m2 = mover_para_grupo(m, "outro")   # trava velha vai junto, sem apagar nada
        assert m2.exists() and m2.with_name(m2.name + ".lock").exists() and not trava.exists()
        m2.with_name(m2.name + ".lock").unlink()
        (BACKLOG / "fedora" / "colide").mkdir()
        (BACKLOG / "fedora" / "colide" / m2.name).write_text("x", encoding="utf-8")
        try:
            mover_para_grupo(m2, "colide")
            assert False, "sobrescreveu card"
        except ValueError:
            assert m2.exists()
        for arq in list(BACKLOG.rglob("*.md")):
            arq.unlink()
        a1 = criar_atividade("fedora", "Corrigir ação do botão", "Troque o texto.", "Texto novo aparece.")
        assert a1.name == "0001-corrigir-acao-do-botao.md", a1

        # fake do agente: respostas por (agente) em fila; registra as chamadas
        chamadas, fila = [], {}

        def fake(agente, arq):
            chamadas.append((agente, ler(arq)["status"]))
            assert (arq.with_name(arq.name + ".lock")).exists(), "rodou sem a trava"
            return fila[agente].pop(0)
        rodar = fake

        # ciclo feliz: uma varredura leva de novo ate concluido
        fila.update(techlead=["Analisei.\nAGENTE: junior"], junior=["Feito.\nOK: texto trocado"], qa=["APROVADO: confere"])
        assert varrer() == 3
        p = ler(a1)
        assert p["status"] == "concluido" and p["agente"] == "junior" and p["rodadas"] == 3 and p["tentativas"] == 0, p.metadata
        assert chamadas == [("techlead", "novo"), ("junior", "pronto"), ("qa", "em_revisao")], chamadas
        assert p.content.count("\n- ") == 3 and "`qa` APROVADO: confere -> `concluido`" in p.content, p.content
        assert varrer() == 0 and len(chamadas) == 3   # final: ninguem roda de novo
        assert not list(BACKLOG.rglob("*.lock")) and not list(BACKLOG.rglob("*.tmp"))

        # REPROVADO volta para pronto ate MAX_TENTATIVAS; ai bloqueia
        a2 = criar_atividade("fedora", "Reprovada", "x")
        del chamadas[:]
        fila.update(techlead=["AGENTE: writer"], writer=["OK: a"] * 3, qa=["REPROVADO\n- README.md:3 errado\nREPROVADO: falta o exemplo"] * 3)
        assert varrer() == 1 + 2 * MAX_TENTATIVAS
        p = ler(a2)
        assert p["status"] == "bloqueado" and p["tentativas"] == MAX_TENTATIVAS == 3, p.metadata
        assert [c[0] for c in chamadas] == ["techlead"] + ["writer", "qa"] * 3, chamadas
        assert "reprovado 3 vezes" in p.content and "  > - README.md:3 errado" in p.content, p.content
        assert varrer() == 0

        # ESCALAR do dev -> escalado -> tech lead sobe para o senior -> concluido
        a3 = criar_atividade("fedora", "Escalada", "x")
        del chamadas[:]
        fila.update(techlead=["AGENTE: junior", "AGENTE: senior"], junior=["ESCALAR: toca o runtime.py"], senior=["OK: feito"], qa=["APROVADO: ok"])
        assert varrer() == 5
        p = ler(a3)
        assert p["status"] == "concluido" and p["agente"] == "senior", p.metadata
        assert chamadas == [("techlead", "novo"), ("junior", "pronto"), ("techlead", "escalado"), ("senior", "pronto"), ("qa", "em_revisao")], chamadas
        assert "`junior` ESCALAR: toca o runtime.py -> `escalado`" in p.content

        # travas da maquina de estados (todas fecham em bloqueado)
        def passo(status, quem, tipo, msg, **campos):
            post = frontmatter.Post("corpo", status=status, **campos)
            return transicionar(post, quem, tipo, msg), post
        assert passo("pronto", "senior", "ESCALAR", "falta decisao")[0] == "bloqueado"        # acima do senior so o usuario
        assert passo("novo", "techlead", "ESCALAR", "falta permissao")[0] == "bloqueado"
        assert passo("escalado", "techlead", "ESCALAR", "x")[0] == "bloqueado"
        assert passo("escalado", "techlead", "AGENTE", "junior", agente="junior")[0] == "bloqueado"   # mesmo agente: sem progresso
        assert passo("escalado", "techlead", "AGENTE", "senior", agente="junior")[0] == "pronto"
        for nao_dev in ("qa", "arquiteto", "techlead", "", "senior e junior", "../x"):
            para, post = passo("novo", "techlead", "AGENTE", nao_dev)
            assert para == "bloqueado" and not post.get("agente"), nao_dev
        assert passo("novo", "techlead", "AGENTE", "`Senior`.")[1]["agente"] == "senior"
        for status, agente, tipo in (("novo", "techlead", "OK"), ("novo", "techlead", "APROVADO"), ("pronto", "junior", "APROVADO"),
                                     ("pronto", "junior", "AGENTE"), ("pronto", "junior", "REPROVADO"), ("em_revisao", "qa", "OK"),
                                     ("em_revisao", "qa", "ESCALAR"), ("em_revisao", "qa", "AGENTE"), ("concluido", "qa", "APROVADO"),
                                     ("qualquer", "junior", "OK"), ("pronto", "junior", "FALHA"), ("novo", "techlead", "FALHA")):
            assert passo(status, agente, tipo, "senior")[0] == "bloqueado", (status, agente, tipo)
        assert passo("pronto", "junior", "OK", "x", rodadas=MAX_RODADAS - 1)[0] == "bloqueado"     # freio de rodadas
        assert passo("em_revisao", "qa", "APROVADO", "x", rodadas=MAX_RODADAS - 1)[0] == "concluido"   # ...mas nao tira um aprovado
        assert passo("em_revisao", "qa", "REPROVADO", "x", tentativas="lixo")[1]["tentativas"] == 1

        # FALHA: resposta sem contrato bloqueia e guarda o que veio
        a4 = criar_atividade("fedora", "Falha", "x")
        del chamadas[:]
        fila.update(techlead=["Traceback (most recent call last):\n  boom"])
        assert varrer() == 1
        p = ler(a4)
        assert p["status"] == "bloqueado" and "FALHA: resposta sem a linha do contrato" in p.content and "  > Traceback" in p.content, p.content

        # falha transitoria: classificacao
        for t in ("dial tcp: lookup api: no such host", "Error 529 Overloaded", "Usage limit reached", "Read timed out",
                  "[WinError 10054] conexao", "rate limit", "QUOTA excedida"):
            assert falha_transitoria(t), t
        assert falha_transitoria("HTTP 529") and falha_transitoria("erro 429: too many") and falha_transitoria("status=503")
        for t in ("", "Traceback: KeyError", "resposta sem a linha do contrato: feito", None,
                  '"duration_api_ms":7529', "id 81442914", "custo 0.5290", "porta 100540"):
            assert not falha_transitoria(t), t

        def pronto_com(titulo, agente="junior"):
            a = criar_atividade("fedora", titulo, "x")
            p_ = ler(a)
            p_["status"], p_["agente"] = "pronto", agente
            salvar(a, p_)
            return a

        # 2 transitorias + OK: card chega ao fim sem bloquear; Historico com linhas curtas, sem detalhe ">"
        a9 = pronto_com("Transitoria ok")
        fila.update(junior=["Error: dial tcp: lookup api.anthropic.com: no such host"] * 2 + ["OK: feito"], qa=["APROVADO: ok"])
        for ciclo in (1, 2):   # cada ciclo tenta UMA vez: nao repete na mesma varredura, o contador cresce por ciclo
            del chamadas[:]
            assert varrer() == 0 and chamadas == [("junior", "pronto")], (ciclo, chamadas)
            assert ler(a9)["status"] == "pronto" and ler(a9)["transitorias"] == ciclo, ler(a9).metadata
        assert varrer() == 2
        p = ler(a9)
        assert p["status"] == "concluido" and p["transitorias"] == 0 and p["rodadas"] == 4, p.metadata
        assert "-> `bloqueado`" not in p.content and "  >" not in p.content and p.content.count("-> `pronto`") == 2, p.content

        # 3 transitorias seguidas: bloqueia com o motivo certo
        a10 = pronto_com("Transitoria tres")
        fila.update(junior=["no such host"] * 3)
        assert varrer() == 0 and varrer() == 0 and varrer() == 1   # a terceira (em outro ciclo) bloqueia
        assert varrer() == 0 and not fila["junior"]
        p = ler(a10)
        assert p["status"] == "bloqueado" and p["transitorias"] == 3 and "falha transitória repetida" in p.content, p.content

        # ESCALAR gerado pelo agentes.py (cota) e transitorio, e o JSON do CLI nao entra no Historico
        a11 = pronto_com("Auto escalar", "senior")
        js = '{"type":"result","is_error":true,"result":"Claude usage limit reached","session_id":"' + "s" * 400 + '"}'
        fila.update(senior=[f"ESCALAR: senior falhou (claude): {js}", "OK: feito"], qa=["APROVADO: ok"])
        assert varrer() == 0 and ler(a11)["transitorias"] == 1 and varrer() == 2
        p = ler(a11)
        assert p["status"] == "concluido" and "usage limit reached" in p.content and '{"' not in p.content, p.content

        # ESCALAR deliberado e REPROVADO com palavra "timeout" seguem o fluxo de antes
        assert passo("pronto", "junior", "ESCALAR", "o timeout do runtime precisa de decisao", transitorias=2)[1]["status"] == "escalado"
        assert passo("pronto", "senior", "ESCALAR", "x", transitorias=2)[1]["transitorias"] == 0
        para, post = passo("em_revisao", "qa", "REPROVADO", "falta tratar timeout")
        assert para == "pronto" and post["tentativas"] == 1 and post["transitorias"] == 0
        assert passo("pronto", "junior", "OK", "x", transitorias=2)[1]["transitorias"] == 0   # passo bom zera

        # falha comum com JSON do CLI: bloqueia, detalhe curto e sem JSON
        a12 = pronto_com("Falha json")
        fila.update(junior=['{"type":"result","is_error":true,"result":"erro interno do parser","x":"' + "y" * 2000 + '"}'])
        assert varrer() == 1
        p = ler(a12)
        hist = p.content.split("## Histórico", 1)[1]
        assert p["status"] == "bloqueado" and "erro interno do parser" in hist and '{"' not in hist, hist

        # JSON comum com 529/429 so dentro de numeros e ids: bloqueia sem contar como transitoria
        a13 = pronto_com("Falha json numeros")
        fila.update(junior=['{"duration_api_ms":7529,"total_cost_usd":0.4291,"session_id":"81442914-10054",'
                            '"result":"erro interno do parser"}'])
        assert varrer() == 1
        p = ler(a13)
        assert p["status"] == "bloqueado" and p["transitorias"] == 0 and '{"' not in p.content, p.metadata
        assert all(len(l) <= MAX_DETALHE_FALHA + 4 for l in hist.splitlines() if l.startswith("  > ")), hist
        assert len(resumir("z" * 5000)) == MAX_DETALHE_FALHA and resumir('{"a": 1') == "(JSON do CLI omitido)"
        # agy: o motivo vem em `short_error` (cota esgotada), e o card tem de mostrar isso, nao "JSON omitido"
        cota = 'ESCALAR: techlead falhou (agy): AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 86h40m52s.","error_code":429}'
        assert falha_transitoria("techlead falhou (agy): o agy negou uma acao (regra deny ou comando nao liberado para esta funcao)")
        assert "quota reached" in resumir(cota) and "omitido" not in resumir(cota) and falha_transitoria(resumir(cota))

        # agente fora de DEVS num card pronto: bloqueia SEM rodar ninguem
        for ruim in ("qa", "techlead", "", "../senior"):
            a5 = criar_atividade("fedora", f"Agente ruim {ruim}", "x")
            p = ler(a5)
            p["status"], p["agente"] = "pronto", ruim
            salvar(a5, p)
            del chamadas[:]
            assert processar(a5) and not chamadas
            p = ler(a5)
            assert p["status"] == "bloqueado" and "nao e executor" in p.content, p.content

        # trava: card travado por outro nao roda; trava velha e retomada; arquivo que nao e card e ignorado
        a6 = criar_atividade("fedora", "Travada", "x")
        lock = a6.with_name(a6.name + ".lock")
        lock.write_text("1", encoding="ascii")
        del chamadas[:]
        assert processar(a6) is False and not chamadas and lock.exists() and ler(a6)["status"] == "novo"
        velho = time.time() - LOCK_VELHO - 10
        os.utime(lock, (velho, velho))
        fila.update(techlead=["ESCALAR: falta decisao do usuario"])
        assert processar(a6) and ler(a6)["status"] == "bloqueado" and not lock.exists()
        nota = BACKLOG / "fedora" / "LEIA.md"
        nota.write_text("so uma nota, sem frontmatter\n", encoding="utf-8")
        quebrado = BACKLOG / "fedora" / "9999-quebrado.md"
        quebrado.write_text("---\nstatus: [novo\n---\nx\n", encoding="utf-8")
        del chamadas[:]
        assert processar(nota) is False and processar(quebrado) is False and not chamadas
        assert nota.read_text(encoding="utf-8") == "so uma nota, sem frontmatter\n"

        # usuario mudou o status enquanto o agente rodava: a resposta nao passa por cima
        a7 = criar_atividade("fedora", "Corrida", "x")

        def fake_corrida(agente, arq):
            p_ = ler(arq)
            p_["status"] = "bloqueado"
            salvar(arq, p_)
            return "AGENTE: junior"
        rodar = fake_corrida
        processar(a7)
        p = ler(a7)
        assert p["status"] == "bloqueado" and not p["agente"] and p["rodadas"] == 0, p.metadata
        rodar = fake

        # salvar: atomico, acento e frontmatter voltam iguais
        p = ler(a1)
        p["titulo"] = "Ação: é “isso”"
        salvar(a1, p)
        assert ler(a1)["titulo"] == "Ação: é “isso”" and ler(a1).content == p.content and not list(BACKLOG.rglob("*.tmp"))

        # rodar de verdade, com um agentes.py falso: comando, tarefa e FEDORA_MOTOR_TURNO=agy chegam no filho
        rodar = orig[2]
        AGENTES_PY = tmp / "agentes_falso.py"
        AGENTES_PY.write_text(
            "import os, sys\n"
            "t = sys.stdin.read()\n"
            "print(sys.argv[1], '|', os.environ.get('FEDORA_MOTOR_TURNO'), '|', sys.argv[2:], '|', 'Arquivo da atividade' in t)\n"
            "print('pedido=' + str('PEDIDO-INICIO ' + 'p' * 5000 + ' PEDIDO-FIM' in t) + ' histfim=' + str('HIST-ULTIMA' in t))\n"
            "print('ESCALAR: fim=' + str(t.strip().endswith('<motivo em uma frase>`')))\n", encoding="utf-8")
        a8 = criar_atividade("fedora", "Rodar", 'pedido com "aspas" & e\nlinha', "criterio")
        _mt = os.environ.get("FEDORA_MOTOR_TURNO")
        try:
            os.environ["FEDORA_MOTOR_TURNO"] = "agy"   # com a variavel: o filho recebe o motor forcado
            saida = rodar("junior", a8)
            assert saida.splitlines()[0] == "junior | agy | ['-'] | True", saida
            assert ler_retorno(saida) == ("ESCALAR", "fim=True"), saida   # a tarefa chegou inteira, ate a ultima linha
            os.environ.pop("FEDORA_MOTOR_TURNO", None)   # sem a variavel: o filho nao a recebe (le o estado da regra)
            saida = rodar("junior", a8)
            assert saida.splitlines()[0] == "junior | None | ['-'] | True", saida
            # card com Historico de ~30000 caracteres: Pedido inteiro e fim do Historico chegam
            p = ler(a8)
            p.content = ("## Pedido\nPEDIDO-INICIO " + "p" * 5000 + " PEDIDO-FIM\n\n## Critérios de aceite\nc\n\n## Histórico\n"
                         + "\n".join(f"- linha {i} " + "h" * 90 for i in range(300)) + "\n- HIST-ULTIMA")
            salvar(a8, p)
            saida = rodar("junior", a8)
            assert "pedido=True histfim=True" in saida and ler_retorno(saida) == ("ESCALAR", "fim=True"), saida
            t = tarefa_de("junior", a8, ler(a8))
            assert "[...]" in t and len(t) < 20000 and "## Critérios de aceite\nc" in t, len(t)
            # Pedido que cita "## Histórico" no meio da linha nao e cortado
            p.content = ("## Pedido\nPEDIDO-INICIO cite `## Histórico` aqui " + "p" * 5000 + " PEDIDO-FIM\n\n## Histórico\n"
                         + "\n".join(f"- linha {i} " + "h" * 90 for i in range(300)) + "\n- HIST-ULTIMA")
            t = tarefa_de("junior", a8, p)
            assert "PEDIDO-FIM" in t and "HIST-ULTIMA" in t and len(t) < 20000, len(t)
            # ultima linha maior que a sobra: o final dela fica
            p.content = "## Pedido\nx\n\n## Histórico\n- " + "z" * 30000 + "ZFIM"
            assert "ZFIM" in tarefa_de("junior", a8, p)
        finally:
            if _mt is None:
                os.environ.pop("FEDORA_MOTOR_TURNO", None)
            else:
                os.environ["FEDORA_MOTOR_TURNO"] = _mt
        p = ler(a8)
        p.content = "## Pedido\npedido com \"aspas\" & e\nlinha\n\n## Histórico"
        salvar(a8, p)
        t = tarefa_de("techlead", a8, ler(a8))
        assert "AGENTE: <junior|writer|design|senior>" in t and a8.as_posix() in t and 'pedido com "aspas" & e' in t
        assert "testa.ps1" in tarefa_de("senior", a8, ler(a8)) and "testa.ps1" not in tarefa_de("techlead", a8, ler(a8))
        assert "APROVADO:" in tarefa_de("qa", a8, ler(a8)) and "OK: <resumo" in tarefa_de("senior", a8, ler(a8))
        # aviso de nao reler o card em toda tarefa; techlead so com Pedido, Criterios e ultima linha do Historico
        tri = frontmatter.Post("## Pedido\nPED-X cita `## Histórico` aqui\n\n## Critérios de aceite\nCRIT-X\n\n## Relacionados\n- REL-X"
                               "\n\n## Histórico\n- 2026 `techlead` HIST-VELHA -> `pronto`\n  > DETALHE-X\n- 2026 `qa` HIST-NOVA -> `pronto`\n  > DET-NOVO")
        for ag in ("techlead", "junior", "qa", "senior"):
            assert AVISO_CARD in tarefa_de(ag, a8, tri), ag
        t = tarefa_de("techlead", a8, tri)
        assert "PED-X" in t and "CRIT-X" in t and "HIST-NOVA" in t, t
        assert "HIST-VELHA" not in t and "DETALHE-X" not in t and "DET-NOVO" not in t and "REL-X" not in t, t
        for ag in ("junior", "qa", "senior"):
            t = tarefa_de(ag, a8, tri)
            assert all(x in t for x in ("HIST-VELHA", "DETALHE-X", "HIST-NOVA", "REL-X", "CRIT-X")), ag
        assert "HIST-ULTIMA" in tarefa_de("techlead", a8, frontmatter.Post(
            "## Pedido\np\n\n## Histórico\n" + "\n".join(f"- l{i} " + "h" * 90 for i in range(300)) + "\n- HIST-ULTIMA"))
        grande = ler(a8)
        grande.content = "inicio " + "x" * 30000 + " fim"
        assert len(tarefa_de("junior", a8, grande)) < MAX_CORPO + 1000 and " fim" in tarefa_de("junior", a8, grande)
        AGENTES_PY = tmp / "nao_existe.py"
        assert ler_retorno(rodar("junior", a8))[0] == "FALHA"   # agentes.py ausente: FALHA, nunca excecao

        # o contrato do despachante bate com a equipe de verdade
        assert orig[1].is_file() and QUEM_RODA["pronto"] is None and set(QUEM_RODA.values()) == {"techlead", "qa", None}
        for nome in DEVS | {"techlead", "qa"}:
            assert (RAIZ / "agentes" / f"{nome}.md").is_file(), nome

        # dentro de um agente o despachante nao roda
        antes = os.environ.get("FEDORA_AGENTE")
        os.environ["FEDORA_AGENTE"] = "senior"
        try:
            assert main(["--uma-vez"]) == 3 and main([]) == 3
        finally:
            if antes is None:
                del os.environ["FEDORA_AGENTE"]
            else:
                os.environ["FEDORA_AGENTE"] = antes

        # gravacao propria (salvar) nao aciona o sinal do vigia; edicao externa aciona
        import types
        sinal = threading.Event()
        vg = _Vigia(sinal)
        card = tmp / "proprio.md"
        salvar(card, frontmatter.Post("x", status="pronto", transitorias=1))
        vg.on_any_event(types.SimpleNamespace(is_directory=False, src_path=str(card) + ".tmp", dest_path=str(card)))
        vg.on_any_event(types.SimpleNamespace(is_directory=False, src_path=str(card)))
        assert not sinal.is_set()
        time.sleep(0.05)
        card.write_text(card.read_text(encoding="utf-8") + "editado\n", encoding="utf-8")
        st = os.stat(card)
        os.utime(card, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        vg.on_any_event(types.SimpleNamespace(is_directory=False, src_path=str(card)))
        assert sinal.is_set()
    finally:
        BACKLOG, AGENTES_PY, rodar = orig
        shutil.rmtree(tmp, ignore_errors=True)
    print("despachante ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
