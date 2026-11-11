"""
Despachante de backlog do cofre.

Cada atividade e um arquivo `backlog/<projeto>/<NNNN>-<slug>.md` no cofre, com `status` no frontmatter. O despachante
vigia a pasta e, a cada mudanca, roda o agente da vez (agentes.py) e grava o proximo estado:

    novo ------techlead: AGENTE: x----> pronto ---x: OK---> em_revisao ---qa: APROVADO---> concluido
    escalado --techlead: AGENTE: y----> pronto              em_revisao ---qa: REPROVADO--> pronto (ate MAX_TENTATIVAS)
    pronto ----x: ESCALAR-------------> escalado            (o senior escalando vai direto para bloqueado)

`bloqueado` e `concluido` sao finais: o despachante nao mexe mais (para destravar, o usuario troca o status na mao).
Vira `bloqueado`: o tech lead devolver ESCALAR, resposta sem a linha do contrato (FALHA), resposta que nao cabe no estado,
`agente` fora de DEVS, MAX_TENTATIVAS reprovacoes ou MAX_RODADAS passos (freio contra laco que gasta cota).

A equipe roda com FEDORA_MOTOR_TURNO=agy: fica toda no plano do Gemini e so o senior (fable) roda no Claude.
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
MAX_CORPO = 8000       # caracteres do card que vao na tarefa (ela vai na linha de comando; o arquivo inteiro fica no caminho)
MAX_DETALHE = 1200     # caracteres da resposta guardados no Historico
TIMEOUT = int(os.environ.get("FEDORA_AGENTE_TIMEOUT", "300")) + 90   # o agentes.py ja corta o agente em TIMEOUT+30
LOCK_VELHO = TIMEOUT + 300   # trava mais velha que isso e de um despachante que morreu

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


def salvar(arq, post):
    """Grava atomico: escreve no .tmp e troca. O leitor (Obsidian, outro despachante) nunca ve o arquivo pela metade."""
    arq = pathlib.Path(arq)
    tmp = arq.with_name(arq.name + ".tmp")
    tmp.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    for i in range(5):   # no Windows a troca falha enquanto outro programa esta com o arquivo aberto
        try:
            os.replace(tmp, arq)
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


# ---------------------------------------------------------------- agente

def tarefa_de(agente, arq, post):
    oque, contrato = PAPEL.get(agente, PAPEL[None])
    corpo = (post.content or "").strip()
    if len(corpo) > MAX_CORPO:   # pedido e criterios ficam no comeco; do Historico vale o fim
        corpo = corpo[:MAX_CORPO // 2] + "\n[...]\n" + corpo[-MAX_CORPO // 2:]
    return (f"{oque}\nArquivo da atividade (so leitura; quem grava o estado e o despachante): {pathlib.Path(arq).as_posix()}\n"
            f"Projeto: {post.get('projeto', '')}\nTitulo: {post.get('titulo', '')}\n\n{corpo}\n\n"
            f"Termine com uma unica linha final no formato do contrato: `{contrato}`")


def rodar(agente, arq):
    """Roda o agente pelo agentes.py e devolve o texto que ele imprimiu. Erro ao rodar vira texto sem contrato (FALHA)."""
    tarefa = tarefa_de(agente, arq, ler(arq))
    # FEDORA_MOTOR_TURNO=agy: a equipe fica no plano do Gemini; so o senior (fable, sem substituto) roda no Claude
    env = dict(os.environ, FEDORA_MOTOR_TURNO="agy", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.Popen([sys.executable, str(AGENTES_PY), agente, tarefa], cwd=RAIZ, env=env, text=True,
                                encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, ValueError) as e:
        return f"nao consegui rodar o agente {agente}: {e}"
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
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


def transicionar(post, agente, tipo, msg, detalhe=""):
    """Aplica a resposta do agente ao card (status, agente, tentativas, rodadas, Historico) e devolve o novo status.
    Tudo que nao cabe no estado atual fecha em `bloqueado`: na duvida o card para e espera o usuario."""
    de = post.get("status")
    para, motivo = "bloqueado", ""
    if tipo == "FALHA":
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
        para = transicionar(post, agente, tipo, msg, saida if guarda else "")
        salvar(arq, post)
        print(f"despachante: {arq.name} {tipo} -> {para}", flush=True)
        return True
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


def criar_atividade(projeto, titulo, pedido, criterios=""):
    """Cria backlog/<projeto>/<NNNN>-<slug>.md com status novo; numero = maior da pasta + 1. Devolve o caminho."""
    pasta = BACKLOG / _slug(projeto, "geral")
    pasta.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(
        f"## Pedido\n{str(pedido).strip()}\n\n## Critérios de aceite\n{str(criterios).strip() or '(nao informados)'}\n\n## Histórico",
        status="novo", projeto=pasta.name, titulo=str(titulo).strip(), agente="", tentativas=0, rodadas=0,
        criado=agora(), atualizado=agora())
    for _ in range(50):
        nums = [int(m.group(1)) for p in pasta.iterdir() if (m := re.match(r"(\d+)-", p.name))]
        arq = pasta / f"{max(nums, default=0) + 1:04d}-{_slug(titulo)}.md"
        try:
            os.close(os.open(arq, os.O_CREAT | os.O_EXCL | os.O_WRONLY))   # reserva o numero: dois criadores nao colidem
        except FileExistsError:
            continue
        salvar(arq, post)
        return arq
    raise RuntimeError(f"nao consegui reservar um numero em {pasta}")


# ---------------------------------------------------------------- vigia

class _Vigia(FileSystemEventHandler):
    """So avisa que algo mudou; quem varre e o laco principal (um passo por vez, sem prender a thread do watchdog)."""

    def __init__(self, sinal):
        self.sinal = sinal

    def on_any_event(self, event):
        if event.is_directory:
            return
        if any(str(c or "").endswith(".md") for c in (event.src_path, getattr(event, "dest_path", ""))):
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
        assert criar_atividade("fedora", "Outra", "y").name == "0008-outra.md"
        fora = criar_atividade("../../fora", "???", "z")
        assert fora.parent == BACKLOG / "fora" and fora.name == "0001-atividade.md", fora
        assert not list(BACKLOG.rglob("*.tmp")) and not list(BACKLOG.rglob("*.lock"))
        for arq in cards():
            if arq != a1:
                arq.unlink()

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
            "print(sys.argv[1], '|', os.environ.get('FEDORA_MOTOR_TURNO'), '|', len(sys.argv), '|', 'Arquivo da atividade' in sys.argv[2])\n"
            "print('ESCALAR: fim=' + str(sys.argv[2].endswith('<motivo em uma frase>`')))\n", encoding="utf-8")
        a8 = criar_atividade("fedora", "Rodar", 'pedido com "aspas" & e\nlinha', "criterio")
        saida = rodar("junior", a8)
        assert saida.splitlines()[0] == "junior | agy | 3 | True", saida
        assert ler_retorno(saida) == ("ESCALAR", "fim=True"), saida   # a tarefa chegou inteira, ate a ultima linha
        t = tarefa_de("techlead", a8, ler(a8))
        assert "AGENTE: <junior|writer|design|senior>" in t and a8.as_posix() in t and 'pedido com "aspas" & e' in t
        assert "APROVADO:" in tarefa_de("qa", a8, ler(a8)) and "OK: <resumo" in tarefa_de("senior", a8, ler(a8))
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
    finally:
        BACKLOG, AGENTES_PY, rodar = orig
        shutil.rmtree(tmp, ignore_errors=True)
    print("despachante ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
