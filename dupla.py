"""
dupla: um terminal so, com as duas assinaturas (Claude e agy/Antigravity) sincronizadas.

Roda em qualquer pasta (a pasta onde voce abriu o terminal e o projeto). Cada pergunta vai para o motor que a regra
de `motor_regra.py` escolher: Claude e o primario; o agy assume e fica ate a janela do Claude resetar quando o uso
SEMANAL passa de 55% ou a SESSAO de 5 h chega a 90%. Mesmos limites, mesmo `motor.json` e mesmas travas (runtime.DENY)
do assistente de voz: o que um decide vale para o outro.

Sincronia entre os motores (cada um tem sessao propria, entao o contexto viaja por um historico compartilhado):
- cada resposta grava (pergunta, resposta, motor) em %LOCALAPPDATA%\\dupla\\<pasta>\\historico.jsonl;
- ao trocar de motor, o que assume recebe no inicio da mensagem o que perdeu (turnos do outro + `git status`);
- ao voltar para o motor antigo, a sessao dele e retomada e ele recebe so o que aconteceu enquanto esteve parado.

Uso do Claude: cada resposta dele informa as duas janelas; se o dado tem mais de 10 min, uma sonda minima
(`claude -p`, em pasta neutra, sem gravar sessao) atualiza antes de decidir. Nada roda no Claude durante a espera no agy.

    dupla                  # terminal interativo
    dupla "faca X"         # uma pergunta so
    dupla --uso            # mostra as janelas e quem responde agora
    dupla --teste          # autoteste offline
Dentro do terminal: /uso  /motor claude|agy|auto  /novo  /ajuda  /sair
"""
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
import runtime       # noqa: E402  (fixa WORKDIR na pasta onde o terminal foi aberto)
import motor_regra   # noqa: E402

LOCAL = pathlib.Path(os.environ.get("DUPLA_ESTADO") or
                     pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home() / ".local" / "state") / "dupla")
HANDOFF = pathlib.Path(os.environ.get("DUPLA_HANDOFF") or pathlib.Path.home() / ".ai-sync" / "HANDOFF.md")
# fim de toda mensagem: o dupla ja registra o turno no HANDOFF.md, entao o motor nao deve editar (achado 2 do agy: ficava em dobro)
AVISO_HANDOFF = "\n\n(dupla: nao edite o HANDOFF.md; o dupla registra o turno nele)"
IDADE_SONDA = 600      # dado do Claude com mais de 10 min: sonda antes de decidir
MAX_TURNOS = 8         # turnos que o motor que assume recebe de contexto
MAX_RESP = 1500        # caracteres de cada resposta antiga no bloco
MAX_BLOCO = 9000
JANELAS = (("seven_day", "semana"), ("five_hour", "sessao 5h"))


# ---------------------------------------------------------------- estado em disco

def pasta_projeto(cwd=None):
    cwd = os.path.normcase(os.path.abspath(cwd or runtime.WORKDIR))
    return LOCAL / hashlib.sha1(cwd.encode("utf-8")).hexdigest()[:10]


def _le_json(arq, padrao):
    try:
        return json.loads(pathlib.Path(arq).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return padrao


def _grava_json(arq, dado):
    arq = pathlib.Path(arq)
    arq.parent.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_name(f"{arq.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(dado, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, arq)


def le_historico(pasta):
    out = []
    try:
        for ln in (pasta / "historico.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("motor") in ("claude", "agy"):
                out.append(d)
    except OSError:
        pass
    return out


def anota_historico(pasta, motor, pergunta, resposta):
    pasta.mkdir(parents=True, exist_ok=True)
    with open(pasta / "historico.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(ts=int(time.time()), motor=motor, user=pergunta, resposta=resposta), ensure_ascii=False) + "\n")


def apara_historico(pasta, maximo=200, folga=100):
    """Passou de maximo+folga turnos: guarda so os `maximo` ultimos. Devolve quantos cortou (para ajustar o `visto` das sessoes)."""
    h = le_historico(pasta)
    if len(h) <= maximo + folga:
        return 0
    cortados = len(h) - maximo
    corpo = "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in h[cortados:])
    _grava_texto(pasta / "historico.jsonl", corpo)
    return cortados


def le_sessoes(pasta):
    d = _le_json(pasta / "sessoes.json", {})
    return d if isinstance(d, dict) else {}


def grava_sessoes(pasta, sessoes):
    _grava_json(pasta / "sessoes.json", sessoes)


# ---------------------------------------------------------------- uso do Claude (cache + sonda)

def carrega_limites(arq=None):
    """Poe em runtime.LIMITS o ultimo uso conhecido. Devolve quando foi observado (epoch) ou 0."""
    d = _le_json(arq or LOCAL / "limites.json", {})
    if not isinstance(d, dict):
        return 0
    for nome, _ in JANELAS:
        w = (d.get("janelas") or {}).get(nome)
        if isinstance(w, dict):
            runtime.LIMITS[nome] = dict(used=w.get("used"), resets=w.get("resets"))
    em = d.get("em")
    return em if isinstance(em, (int, float)) and not isinstance(em, bool) else 0


def grava_limites(arq=None):
    janelas = {n: runtime.LIMITS[n] for n, _ in JANELAS if isinstance(runtime.LIMITS.get(n), dict)}
    _grava_json(arq or LOCAL / "limites.json", {"em": time.time(), "janelas": janelas})


def sondar():
    """Pergunta o minimo ao Claude so para ele informar as janelas de uso. True se informou."""
    cmd = runtime._claude_base() + ["-p", "responda apenas: ok", "--output-format", "stream-json", "--verbose",
                                    "--max-turns", "1", "--no-session-persistence"]
    LOCAL.mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.run(cmd, cwd=str(LOCAL), text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=90,
                           env=dict(os.environ, PYTHONUTF8="1", FEDORA_MOTOR_TURNO="claude"))
    except (OSError, subprocess.SubprocessError):
        return False
    visto = False
    for ln in (p.stdout or "").splitlines():
        if '"rate_limit_event"' not in ln:
            continue
        try:
            info = (json.loads(ln).get("rate_limit_info") or {})
        except ValueError:
            continue
        for nome, w in (info.get("unifiedWindows") or {}).items():
            if nome in dict(JANELAS):
                runtime.LIMITS[nome] = dict(used=w.get("utilization"), resets=w.get("resetsAt"))
                visto = True
    if visto:
        grava_limites()
    return visto


def espera_no_agy():
    return motor_regra.espera_ativa(motor_regra._le(motor_regra.ARQUIVO), time.time())


def garantir_uso():
    """Antes de decidir: com dado velho do Claude, sonda. Durante a espera no agy nao toca no Claude."""
    if espera_no_agy() is not None:
        return
    em = carrega_limites()
    if time.time() - em > IDADE_SONDA:
        sondar()


def resumo_uso():
    partes = []
    for nome, rot in JANELAS:
        w = runtime.LIMITS.get(nome) or {}
        u, r = motor_regra._num(w.get("used")), motor_regra._reset_valido(w.get("resets"))
        if u is None or (r is not None and r <= time.time()):
            partes.append(f"{rot} ?")
        else:
            partes.append(f"{rot} {round(u * 100)}%" + (f" (reseta {motor_regra._quando(r)})" if r else ""))
    return " · ".join(partes)


# ---------------------------------------------------------------- contexto entre motores

def _git_status(cwd):
    """`git status --short` (30 linhas no maximo); '' se o repositorio esta limpo; None se a pasta nao e repo git."""
    try:
        p = subprocess.run(["git", "status", "--short"], cwd=cwd, text=True, encoding="utf-8", errors="replace",
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8)
        if p.returncode != 0:
            return None
        linhas = (p.stdout or "").splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    return "\n".join(linhas[:30] + ([f"... (+{len(linhas) - 30})"] if len(linhas) > 30 else []))


def monta_contexto(perdidos, motor, outro, cwd=None):
    """Bloco no inicio da mensagem com o que `motor` nao viu (turnos conduzidos por `outro`). '' se nada a contar."""
    if not perdidos:
        return ""
    pedacos = []
    for t in perdidos[-MAX_TURNOS:]:
        r = str(t.get("resposta") or "")
        r = r if len(r) <= MAX_RESP else r[:MAX_RESP] + " [...]"
        pedacos.append(f"### Pedido ({t['motor']})\n{t.get('user', '')}\n### Resposta ({t['motor']})\n{r}")
    corpo = "\n\n".join(pedacos)
    if len(corpo) > MAX_BLOCO:
        corpo = "[...]\n" + corpo[-MAX_BLOCO:]
    git = _git_status(cwd or runtime.WORKDIR)
    return (f"<contexto_da_dupla>\nEsta conversa e conduzida por duas assinaturas (Claude e agy) que se revezam por limite de "
            f"uso. Voce ({motor}) assumiu agora; os turnos abaixo foram conduzidos por {outro} e voce nao os viu. "
            f"Trate-os como seu proprio trabalho e continue de onde pararam.\n\n{corpo}\n"
            + (f"\nArquivos alterados no projeto (git status --short):\n{git}\n" if git else "")
            + "</contexto_da_dupla>\n\n")


def _uma_linha(s, n):
    return " ".join(str(s).split())[:n]


def atualiza_handoff(motor, pergunta, resposta, cwd, arq=None):
    """Mantem o HANDOFF.md compartilhado (lido tambem pelos terminais nativos claude/agy): troca o 'Estado atual' e poe uma
    linha no topo do 'Historico'. So mexe se o arquivo ja existe com as duas secoes. True se gravou."""
    arq = pathlib.Path(arq or HANDOFF)
    try:
        txt = arq.read_text(encoding="utf-8")
    except OSError:
        return False
    i, j = txt.find("## Estado atual"), txt.find("## Histórico")
    if i < 0 or j < i:
        return False
    bruto = _git_status(cwd)
    arquivos = "n/d (sem git)" if bruto is None else ", ".join([ln[3:] for ln in bruto.splitlines() if ln[3:]][:15]) or "nenhum"
    traco = "—"
    estado = ("## Estado atual\n"
              f"- **Último agente:** {motor} (via dupla, {time.strftime('%d/%m %H:%M')})\n"
              f"- **Tarefa em andamento:** {_uma_linha(pergunta, 200)}\n"
              f"- **Projeto/diretório:** {cwd}\n"
              f"- **Feito:** {_uma_linha(resposta, 400) or traco}\n"
              f"- **Arquivos alterados:** {arquivos}\n"
              "- **Próximos passos:** —\n- **Bloqueios/observações:** —\n\n")
    resto = txt[j:]
    k = resto.find("\n") + 1
    linha = f"- {time.strftime('%Y-%m-%d %H:%M')} [{motor.upper()}] {_uma_linha(pergunta, 100)}\n"
    try:
        _grava_texto(arq, txt[:i] + estado + resto[:k] + linha + resto[k:])
    except OSError:
        return False
    return True


def _grava_texto(arq, texto):
    tmp = arq.with_name(f"{arq.name}.{os.getpid()}.tmp")
    tmp.write_text(texto, encoding="utf-8")
    os.replace(tmp, arq)


# ---------------------------------------------------------------- um turno

class Tela:
    """Saida do terminal (trocavel nos testes)."""
    def __init__(self, cor=True):
        self.cor = cor

    def texto(self, s):
        sys.stdout.write(s)
        sys.stdout.flush()

    def fraco(self, s):
        self.texto(f"\x1b[2m{s}\x1b[0m\n" if self.cor else s + "\n")


def executa(texto, forcado="auto", pasta=None, tela=None):
    """Roda um turno. Devolve dict(motor, resposta, erro). Grava historico e sessao so se der certo."""
    pasta = pasta or pasta_projeto()
    tela = tela or Tela()
    if forcado == "auto":
        garantir_uso()
    else:
        carrega_limites()   # so para o cabecalho mostrar o uso; sem sonda e sem decidir nada
    motor = runtime.motor_atual(forcado)
    outro = "agy" if motor == "claude" else "claude"
    sessoes = le_sessoes(pasta)
    s = sessoes.get(motor) if isinstance(sessoes.get(motor), dict) else {}
    hist = le_historico(pasta)
    visto = s.get("visto") if isinstance(s.get("visto"), int) and not isinstance(s.get("visto"), bool) else 0
    perdidos = [t for t in hist[min(visto, len(hist)):] if t["motor"] != motor]
    msg = monta_contexto(perdidos, motor, outro) + texto + AVISO_HANDOFF
    sid = s.get("sid") if runtime.valid_session(s.get("sid")) else None

    motivo = motor_regra.ULTIMO["motivo"] if forcado == "auto" else f"forcado: {forcado}"
    tela.fraco(f"── {motor} · {resumo_uso()} · {motivo}" + (f" · +{len(perdidos)} turno(s) de contexto do {outro}" if perdidos else ""))
    resposta, erro, novo_sid, claude_falou = [], None, sid, False
    gen = runtime.run(msg, sid, None, True, motor)
    try:
        for ev in gen:
            t = ev.get("t")
            if t == "delta":
                resposta.append(ev["text"])
                tela.texto(ev["text"])
            elif t == "tool" and ev.get("phase") == "use":
                tela.fraco(f"\n  · {ev.get('name')} {ev.get('input') or ''}")
            elif t == "ratelimit":
                claude_falou = True
            elif t == "complete":
                novo_sid = ev.get("session_id") or novo_sid
            elif t == "error":
                erro = ev.get("message") or "erro"
    except KeyboardInterrupt:
        runtime.CANCEL.set()
        erro = "interrompido"
    finally:
        gen.close()
        runtime.CANCEL.clear()
    tela.texto("\n")
    if claude_falou:
        grava_limites()
    if erro:
        tela.fraco(f"✗ {motor}: {erro}")
        return dict(motor=motor, resposta="".join(resposta), erro=erro)
    anota_historico(pasta, motor, texto, "".join(resposta))
    atualiza_handoff(motor, texto, "".join(resposta), runtime.WORKDIR)
    sessoes[motor] = dict(sid=novo_sid, visto=len(hist) + 1)
    cortados = apara_historico(pasta)
    if cortados:   # o historico encolheu pelo comeco: os indices `visto` de todos os motores andam junto
        for m, st in sessoes.items():
            if isinstance(st, dict) and isinstance(st.get("visto"), int):
                st["visto"] = max(0, st["visto"] - cortados)
    grava_sessoes(pasta, sessoes)
    return dict(motor=motor, resposta="".join(resposta), erro=None)


# ---------------------------------------------------------------- terminal

AJUDA = ("/uso            janelas do Claude e quem responde agora\n"
         "/motor X        claude | agy | auto (auto = regra 55% semana / 90% sessao)\n"
         "/novo           esquece historico e sessoes desta pasta\n"
         "/sair           sai (Ctrl+C durante uma resposta so a interrompe)")


def mostra_uso(tela):
    garantir_uso()
    esp = espera_no_agy()
    quem = f"agy ate {motor_regra._quando(esp)}" if esp else "claude (primario)"
    tela.texto(f"Claude: {resumo_uso()}\nLimites: semana > {round(motor_regra.LIMITE * 100)}% ou sessao >= "
               f"{round(motor_regra.LIMITE_SESSAO * 100)}% -> agy\nResponde agora: {quem}\n")


def repl(tela):
    forcado = "auto"
    pasta = pasta_projeto()
    tela.fraco(f"dupla em {runtime.WORKDIR}  ·  /ajuda para os comandos")
    while True:
        try:
            linha = input(f"[{forcado}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            tela.texto("\n")
            return 0
        if not linha:
            continue
        if linha in ("/sair", "/exit", "/quit"):
            return 0
        if linha == "/ajuda":
            tela.texto(AJUDA + "\n")
        elif linha == "/uso":
            mostra_uso(tela)
        elif linha.startswith("/motor"):
            v = linha[6:].strip().lower()
            if v in ("claude", "agy", "auto"):
                forcado = v
                tela.fraco(f"motor: {v}")
            else:
                tela.texto("use /motor claude|agy|auto\n")
        elif linha == "/novo":
            for f in ("historico.jsonl", "sessoes.json"):
                try:
                    (pasta / f).unlink()
                except OSError:
                    pass
            tela.fraco("historico e sessoes desta pasta apagados")
        elif linha.startswith("/"):
            tela.texto("comando desconhecido; /ajuda\n")
        else:
            executa(linha, forcado, pasta, tela)


def main(argv):
    if os.name == "nt":
        os.system("")   # liga as sequencias ANSI do console
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    tela = Tela()
    if argv and argv[0] == "--teste":
        return _teste()
    if argv and argv[0] == "--uso":
        mostra_uso(tela)
        return 0
    if argv and argv[0] == "--motor":   # so decide: imprime claude|agy (para roteadores externos, ex.: ai.ps1)
        garantir_uso()
        print(runtime.motor_atual("auto"))
        sys.stderr.write(motor_regra.ULTIMO["motivo"] + "\n")
        return 0
    if argv:
        r = executa(" ".join(argv), "auto", None, tela)
        return 1 if r["erro"] else 0
    return repl(tela)


# ---------------------------------------------------------------- autoteste (offline)

def _teste():
    global LOCAL, HANDOFF
    import motor_agy
    tmp = pathlib.Path(tempfile.mkdtemp())
    LOCAL = tmp / "dupla"
    HANDOFF = tmp / "nao-existe" / "HANDOFF.md"      # os turnos de teste nao podem escrever no HANDOFF real
    motor_regra.ARQUIVO = tmp / "motor.json"
    motor_agy.instalado = lambda: True
    global garantir_uso
    garantir_uso = lambda: None                      # sem sonda de verdade
    pasta = pasta_projeto(str(tmp / "proj"))
    agora = time.time()
    sem, ses = agora + 3 * 86400, agora + 3 * 3600
    chamadas = []

    def falso(msg, sid, system, full, motor):
        chamadas.append(dict(msg=msg, sid=sid, motor=motor))
        ident = "11111111-1111-4111-8111-111111111111" if motor == "claude" else "22222222-2222-4222-8222-222222222222"
        yield dict(t="status", runtime=motor)
        if motor == "claude":
            yield dict(t="ratelimit")
        yield dict(t="delta", text=f"resposta {len(chamadas)} do {motor}")
        yield dict(t="complete", session_id=ident)
    runtime.run = falso

    class Muda(Tela):
        def texto(self, s): pass
    t = Muda(cor=False)

    # 1) Claude abaixo dos limites: responde, nada de contexto
    runtime.LIMITS.update(seven_day=dict(used=0.30, resets=sem), five_hour=dict(used=0.20, resets=ses))
    r = executa("primeira", "auto", pasta, t)
    assert r["motor"] == "claude" and not r["erro"] and chamadas[-1]["msg"] == "primeira" + AVISO_HANDOFF
    # 2) semana passa de 55%: agy assume e recebe o turno do Claude como contexto
    runtime.LIMITS["seven_day"] = dict(used=0.60, resets=sem)
    r = executa("segunda", "auto", pasta, t)
    assert r["motor"] == "agy" and chamadas[-1]["sid"] is None
    assert "<contexto_da_dupla>" in chamadas[-1]["msg"] and "primeira" in chamadas[-1]["msg"] and "resposta 1 do claude" in chamadas[-1]["msg"]
    assert chamadas[-1]["msg"].endswith("segunda" + AVISO_HANDOFF)
    # 3) segue no agy (espera gravada): sessao do agy retomada, sem repetir contexto
    r = executa("terceira", "auto", pasta, t)
    assert r["motor"] == "agy" and chamadas[-1]["sid"] == "22222222-2222-4222-8222-222222222222"
    assert "<contexto_da_dupla>" not in chamadas[-1]["msg"] and chamadas[-1]["msg"] == "terceira" + AVISO_HANDOFF
    # 4) forcar o Claude: retoma a sessao dele e recebe so o que o agy fez enquanto ele esteve parado
    r = executa("quarta", "claude", pasta, t)
    assert r["motor"] == "claude" and chamadas[-1]["sid"] == "11111111-1111-4111-8111-111111111111"
    m = chamadas[-1]["msg"]
    assert "segunda" in m and "terceira" in m and "primeira" not in m and m.endswith("quarta" + AVISO_HANDOFF), m
    # 5) erro nao grava historico nem sessao
    antes = len(le_historico(pasta))

    def quebra(msg, sid, system, full, motor):
        yield dict(t="error", message="boom")
    runtime.run = quebra
    r = executa("quinta", "agy", pasta, t)
    assert r["erro"] == "boom" and len(le_historico(pasta)) == antes
    runtime.run = falso
    # 6) sessao no 90%: vai para o agy
    motor_regra.ARQUIVO.unlink(missing_ok=True)
    runtime.LIMITS.update(seven_day=dict(used=0.10, resets=sem), five_hour=dict(used=0.91, resets=ses))
    assert executa("sexta", "auto", pasta, t)["motor"] == "agy"
    # 7) contexto: limites de tamanho e sem pedaco quando nada foi perdido
    assert monta_contexto([], "agy", "claude") == ""
    grande = [dict(motor="claude", user=f"p{i}", resposta="x" * 5000) for i in range(30)]
    b = monta_contexto(grande, "agy", "claude")
    assert b.count("### Pedido") <= MAX_TURNOS and len(b) < MAX_BLOCO + 2500, len(b)
    # 8) cache de limites: grava e recarrega; janela resetada e descartada pela regra
    runtime.LIMITS.clear()
    runtime.LIMITS.update(seven_day=dict(used=0.5, resets=sem))
    grava_limites()
    runtime.LIMITS.clear()
    assert carrega_limites() > 0 and runtime.LIMITS["seven_day"]["used"] == 0.5
    # 8b) HANDOFF.md: troca o estado, poe a linha no topo do historico, e nao cria arquivo nem mexe em formato desconhecido
    ho = tmp / "HANDOFF.md"
    assert atualiza_handoff("claude", "x", "y", str(tmp), ho) is False and not ho.exists()
    modelo = "# H\n\n## Estado atual\n- **Último agente:** —\n\n## Histórico (1 linha por sessao)\n- antiga\n"
    ho.write_text(modelo, encoding="utf-8")
    assert atualiza_handoff("claude", "primeira tarefa", "feito tal", str(tmp), ho)
    assert atualiza_handoff("agy", "segunda tarefa", "feito outro", str(tmp), ho)
    h = ho.read_text(encoding="utf-8")
    assert h.count("## Estado atual") == 1 and "agy (via dupla" in h and "segunda tarefa" in h and "feito outro" in h
    assert h.index("[AGY] segunda") < h.index("[CLAUDE] primeira") < h.index("- antiga"), h
    ho.write_text("sem secoes", encoding="utf-8")
    assert atualiza_handoff("claude", "x", "y", str(tmp), ho) is False and ho.read_text(encoding="utf-8") == "sem secoes"
    # 8c) pasta sem git: None (diferente de repo limpo) e o HANDOFF diz "n/d (sem git)"
    assert _git_status(str(tmp)) is None
    ho.write_text(modelo, encoding="utf-8")
    assert atualiza_handoff("claude", "x", "y", str(tmp), ho) and "n/d (sem git)" in ho.read_text(encoding="utf-8")
    # 8d) historico: poda para os ultimos 200 so depois de 300 turnos, e o `visto` das sessoes acompanha (sem repetir contexto)
    pp = tmp / "poda"
    for i in range(300):
        anota_historico(pp, "claude", f"p{i}", "r")
    assert apara_historico(pp) == 0 and len(le_historico(pp)) == 300
    anota_historico(pp, "agy", "p300", "r")
    assert apara_historico(pp) == 101 and len(le_historico(pp)) == 200 and le_historico(pp)[0]["user"] == "p101"
    # 8e) pedido longo vai pelo stdin (a linha de comando ja leva ~14 mil caracteres de DENY); curto continua no argumento
    curto, longo = runtime.build_command("x" * 10), runtime.build_command("x" * 10, via_stdin=True)
    assert "x" * 10 in curto and "x" * 10 not in longo and longo[longo.index("-p") + 1] == "--output-format"
    # 9) estado corrompido nao derruba
    (pasta / "sessoes.json").write_text("{lixo", encoding="utf-8")
    (pasta / "historico.jsonl").write_text("nao json\n[1]\n", encoding="utf-8")
    assert le_sessoes(pasta) == {} and le_historico(pasta) == []
    print("dupla ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
