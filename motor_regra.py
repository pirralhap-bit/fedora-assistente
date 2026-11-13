"""
Regra de troca de motor (qual assinatura responde o turno, e com ela todos os agentes).

Primario: Claude. Segundo: agy (Antigravity/Google). A regra olha so o uso do CLAUDE, em duas janelas que ele mesmo informa
em cada rate_limit_event (fracao de 0 a 1, em runtime.LIMITS):

- semana (`seven_day`): uso MAIOR que 55%;
- sessao (`five_hour`, a janela de 5 h): uso de 90% ou mais.

Qualquer uma das duas vale: o agy assume e FICA ate essa janela do Claude resetar (o instante vem junto do uso; se as duas
estouram, ate o reset da mais longa). Passou o reset, volta para o Claude e o uso antigo e descartado (nao vale mais) ate a
proxima pergunta informar o novo. Com o agy no comando, nada roda no Claude: nem os agentes (agentes.py).

- uso desconhecido (servidor recem-iniciado, ainda sem pergunta ao Claude) ou janela ja resetada: Claude;
- o uso do agy NAO entra na regra (decisao do usuario, 2026-11-09): o agy e so o motor de economia; excecao (2026-11-12):
  o comando "o agy esta sem cota" grava "agy_bloqueado_ate" (bloquear_agy) e, ate esse instante, a regra fica no Claude;
- agy nao instalado: continua no Claude (nunca troca para um motor que nao existe);
- reset invalido (nao numerico ou em milissegundos, > 1e11) segura 12 h; nenhum reset, lido ou gravado, passa de agora + 8 dias.

"No agy ate o reset" fica gravado em arquivo: o dado do Claude so reaparece depois da 1a pergunta ao Claude, e um
reinicio do servidor nao pode devolver a conversa ao Claude no meio da espera.
Motor fixo (forcado()/definir_forcado()): o campo "forcar" do motor.json (comando "troca o motor pro agy" por voz/chat;
"motor automatico" limpa) vence `FEDORA_MOTOR=claude|agy` do .env (padrao de boot), que vence esta regra (`auto`).
"""
import json
import os
import pathlib
import threading
import time

LIMITE = float(os.environ.get("FEDORA_MOTOR_LIMITE", "0.55"))              # semana: "mais de 55%" (estritamente maior)
LIMITE_SESSAO = float(os.environ.get("FEDORA_MOTOR_LIMITE_SESSAO", "0.90"))  # sessao de 5 h: "em 90%" (90% ja conta)
ESPERA_SEM_RESET = 12 * 3600   # uso alto mas sem horario de reset conhecido: segura o agy por 12 h
ESPERA_MAX = 8 * 24 * 3600     # nenhum reset (lido ou gravado) vale mais que agora + 8 dias: um `ate` absurdo nao prende o agy
BLOQUEIO_PADRAO = 2 * 3600     # "o agy esta sem cota" sem reset conhecido: bloqueia o agy por 2 h
RESET_MAX_EPOCH = 1e11        # acima disso e milissegundo (ou lixo), nao segundo: reset invalido

_LOCAL = pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home() / ".local" / "state") / "fedora-assistente"
ARQUIVO = pathlib.Path(os.environ.get("FEDORA_MOTOR_ESTADO") or _LOCAL / "motor.json")
_ARQUIVO_PADRAO = ARQUIVO
ULTIMO = {"motor": "claude", "motivo": "primario"}   # a ultima decisao, para o /api/status


def _quando(t):
    return time.strftime("%d/%m %H:%M", time.localtime(t))


def _num(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError, OverflowError):   # OverflowError: inteiro gigante vindo de um motor.json adulterado
        return None


def _reset_valido(reset):
    """Instante de reset em segundos epoch, ou None se invalido (nao numerico, NaN, ou em milissegundos)."""
    r = _num(reset)
    return None if r is None or r != r or r > RESET_MAX_EPOCH else r


def espera_ativa(estado, agora):
    """Fim da espera no agy gravada em `estado` (limitado a agora + 8 dias), ou None se nao ha espera valida."""
    estado = estado if isinstance(estado, dict) else {}
    ate = estado.get("ate")
    if estado.get("motor") != "agy" or isinstance(ate, bool) or not isinstance(ate, (int, float)) or ate != ate or not agora < ate:
        return None
    return min(ate, agora + ESPERA_MAX)   # sem float(ate): um inteiro de 400 digitos estoura float(); a comparacao int x float e exata


def escolher(usos, agora, estado, agy_ok=True, limite=None, limite_sessao=None):
    """Pura. usos = {"seven_day": (uso, reset), "five_hour": (uso, reset)}; estado = {"motor", "ate"}.
    Devolve (motor, estado novo, motivo). Nenhum `ate` passa de agora + 8 dias (ESPERA_MAX)."""
    limite = LIMITE if limite is None else limite
    limite_sessao = LIMITE_SESSAO if limite_sessao is None else limite_sessao
    claude = {"motor": "claude", "ate": None}
    if not agy_ok:
        return "claude", claude, "agy nao esta instalado: fica no Claude"

    estado = estado or {}
    ate = espera_ativa(estado, agora)
    if ate is not None:
        # o limite vai gravado: sem isso o teto "agora + 8 dias" andaria junto com o relogio e a espera nunca venceria
        novo = estado if ate == estado.get("ate") else dict(estado, ate=ate)
        return "agy", novo, f"limite do Claude estourado: agy ate {_quando(ate)}"

    causas, fim = [], 0.0
    for janela, rotulo, teto, estrito in (("seven_day", "na semana", limite, True), ("five_hour", "da sessao de 5 h", limite_sessao, False)):
        uso, reset = (usos or {}).get(janela, (None, None))
        uso, reset = _num(uso), _reset_valido(reset)
        if uso is None or not (uso > teto if estrito else uso >= teto) or (reset is not None and reset <= agora):
            continue
        causas.append(f"Claude em {round(uso * 100)}% {rotulo} (limite {round(teto * 100)}%)")
        fim = max(fim, min(reset, agora + ESPERA_MAX) if reset is not None else agora + ESPERA_SEM_RESET)
    if causas:
        return "agy", {"motor": "agy", "ate": fim}, f"{' e '.join(causas)}: agy ate {_quando(fim)}"

    if estado.get("motor") == "agy":
        return "claude", claude, "janela do Claude resetou: volta para o Claude"
    return "claude", claude, "primario"


def _le(arquivo):
    try:
        e = json.loads(pathlib.Path(arquivo).read_text(encoding="utf-8"))
        return e if isinstance(e, dict) else {}
    except (OSError, ValueError):
        return {}   # arquivo ausente ou corrompido: como se nao houvesse espera (Claude e o primario)


def _grava(arquivo, estado):
    try:
        arquivo = pathlib.Path(arquivo)
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        tmp = arquivo.with_name(f"{arquivo.name}.{os.getpid()}.{threading.get_ident()}.tmp")   # um temporario por thread: duas gravacoes nao se intercalam
        tmp.write_text(json.dumps(estado), encoding="utf-8")
        os.replace(tmp, arquivo)
    except OSError:
        pass   # perder o arquivo so custa a memoria da espera, nunca a resposta


# Cache do uso do Claude compartilhado entre processos (servidor, agentes.py do despachante, dupla).
# Formato de limites.json (fica ao lado do motor.json; FEDORA_LIMITES troca o caminho):
#   {"em": <epoch s da gravacao>, "janelas": {"seven_day": {"used": 0.0-1.0, "resets": <epoch s>},
#                                             "five_hour": {"used": ..., "resets": ...}}}
# Janela ja resetada nao e apagada aqui: _decidir descarta o uso quando resets <= agora.
JANELAS = ("seven_day", "five_hour")


def limites_arquivo(arquivo=None):
    if arquivo is None and ARQUIVO == _ARQUIVO_PADRAO and os.environ.get("FEDORA_LIMITES"):
        return pathlib.Path(os.environ["FEDORA_LIMITES"])   # so no caminho real: teste que troca ARQUIVO fica isolado
    return pathlib.Path(arquivo or ARQUIVO).with_name("limites.json")


def carrega_limites(destino, arq=None):
    """Poe em `destino` (dict, ex.: runtime.LIMITS) as janelas gravadas. Devolve quando foram observadas (epoch) ou 0."""
    d = _le(arq or limites_arquivo())
    janelas = d.get("janelas") if isinstance(d.get("janelas"), dict) else {}
    for nome in JANELAS:
        w = janelas.get(nome)
        if isinstance(w, dict):
            destino[nome] = dict(used=w.get("used"), resets=w.get("resets"))
    em = d.get("em")
    return em if isinstance(em, (int, float)) and not isinstance(em, bool) else 0


def grava_limites(limits, arq=None, agora=None):
    """Grava as janelas de `limits` (atomico; falha de disco e ignorada). Nada a gravar: nao toca no arquivo."""
    janelas = {n: dict(used=limits[n].get("used"), resets=limits[n].get("resets"))
               for n in JANELAS if isinstance((limits or {}).get(n), dict)}
    if janelas:
        _grava(arq or limites_arquivo(), {"em": time.time() if agora is None else agora, "janelas": janelas})


_TRAVA = threading.Lock()   # /api/status (thread HTTP) e o turno (outra thread) chamam decidir() ao mesmo tempo


def decidir(limits, agora=None, arquivo=None, limite=None, limite_sessao=None):
    with _TRAVA:
        return _decidir(limits, agora, arquivo, limite, limite_sessao)


def _decidir(limits, agora=None, arquivo=None, limite=None, limite_sessao=None):
    """Motor deste turno ('claude' ou 'agy') pelo uso do Claude em `limits` (runtime.LIMITS); grava a espera no arquivo."""
    agora = time.time() if agora is None else agora
    lim_arq = limites_arquivo(arquivo)
    arquivo = arquivo or ARQUIVO
    limits = dict(limits or {})
    if not any(isinstance(limits.get(j), dict) for j in JANELAS):
        carrega_limites(limits, lim_arq)   # processo novo (agentes.py, servidor reiniciado): ultimo uso gravado
    usos = {}
    for janela in JANELAS:
        w = (limits or {}).get(janela) or {}
        uso, reset = w.get("used"), w.get("resets")
        r = _num(reset)
        if r is not None and r <= agora:
            uso = None   # janela que ja resetou: o numero guardado e de outra janela
        usos[janela] = (uso, reset)
    try:
        import motor_agy
        agy_ok = motor_agy.instalado()
    except Exception:  # noqa: BLE001
        agy_ok = False
    antes = _le(arquivo)
    bloq = agy_bloqueado(antes, agora)
    agy_ok = agy_ok and bloq is None   # agy sem cota (comando "o agy esta sem cota"): a regra fica no Claude ate o prazo
    motor, depois, motivo = escolher(usos, agora, antes, agy_ok, limite, limite_sessao)
    if bloq is not None and motor == "claude":
        motivo = f"agy sem cota ate {_quando(bloq)}: fica no Claude"
    for campo in ("forcar", "agy_bloqueado_ate"):
        if antes.get(campo) is not None:
            depois = dict(depois, **{campo: antes[campo]})   # a espera nunca apaga o motor fixo nem o bloqueio gravados
    if depois != antes and (depois.get("motor") == "agy" or antes):
        _grava(arquivo, depois)
    ULTIMO.update(motor=motor, motivo=motivo)
    return motor


def agy_bloqueado(estado, agora):
    """Fim do bloqueio do agy (campo "agy_bloqueado_ate", limitado a agora + 8 dias) ou None se nao ha bloqueio vigente."""
    b = (estado if isinstance(estado, dict) else {}).get("agy_bloqueado_ate")
    if isinstance(b, bool) or not isinstance(b, (int, float)) or b != b or not agora < b:
        return None
    return min(b, agora + ESPERA_MAX)


def bloquear_agy(ate=None, agora=None, arquivo=None):
    """Marca o agy sem cota ate `ate` (epoch s; None ou invalido = agora + BLOQUEIO_PADRAO; teto agora + 8 dias).
    Enquanto valer, a regra nunca devolve agy. Atomico e sob a mesma trava do decidir(). Devolve o instante gravado."""
    agora = time.time() if agora is None else agora
    fim = _reset_valido(ate)
    fim = agora + BLOQUEIO_PADRAO if fim is None or fim <= agora else min(fim, agora + ESPERA_MAX)
    arquivo = arquivo or ARQUIVO
    with _TRAVA:
        e = _le(arquivo)
        e["agy_bloqueado_ate"] = fim
        e.setdefault("motor", "claude")
        e.setdefault("ate", None)
        _grava(arquivo, e)
    return fim


def forcado(padrao=None, arquivo=None):
    """Motor fixo: 'claude', 'agy' ou '' (automatico). Precedencia: campo "forcar" do motor.json (comando por voz/chat)
    > `padrao` (FEDORA_MOTOR do ambiente, o padrao de boot) > '' (regra). FEDORA_MOTOR invalido cai no Claude."""
    f = _le(arquivo or ARQUIVO).get("forcar")
    if f in ("claude", "agy"):
        return f
    p = (os.environ.get("FEDORA_MOTOR", "auto") if padrao is None else padrao).strip().lower() or "auto"
    return "" if p == "auto" else p if p in ("claude", "agy") else "claude"


def definir_forcado(valor, arquivo=None):
    """Grava o motor fixo no motor.json (claude | agy; auto limpa). Atomico e sob a mesma trava do decidir()."""
    valor = (valor or "").strip().lower()
    if valor not in ("claude", "agy", "auto"):
        raise ValueError(valor)
    arquivo = arquivo or ARQUIVO
    with _TRAVA:
        e = _le(arquivo)
        e["forcar"] = None if valor == "auto" else valor
        e.setdefault("motor", "claude")
        e.setdefault("ate", None)
        _grava(arquivo, e)
    return valor


def _teste():
    import tempfile
    h = 3600
    ag = 1_000_000.0
    sem, ses = ag + 3 * 24 * h, ag + 2 * h
    claude = {"motor": "claude", "ate": None}
    U = lambda s=(None, None), c=(None, None): {"seven_day": s, "five_hour": c}   # noqa: E731

    # SEMANA: abaixo e exatamente no limite ficam no Claude ("mais de 55%" e estrito); acima vai para o agy ate o reset
    assert escolher(U((0.30, sem)), ag, claude)[0] == "claude"
    assert escolher(U((0.55, sem)), ag, claude)[0] == "claude"
    m, est, motivo = escolher(U((0.551, sem)), ag, claude)
    assert m == "agy" and est == {"motor": "agy", "ate": sem} and "na semana" in motivo and "55%" in motivo, (m, est, motivo)
    m, est56, motivo = escolher(U((0.56, sem)), ag, claude)   # o caso do usuario: 56% na semana => agy (Gemini no lugar do Claude)
    assert m == "agy" and est56 == {"motor": "agy", "ate": sem} and "56%" in motivo, (m, est56, motivo)
    # SESSAO: 89% fica; 90% ja conta ("em 90%"); vai ate o reset da janela de 5 h
    assert escolher(U(c=(0.89, ses)), ag, claude)[0] == "claude"
    m, est_s, motivo = escolher(U(c=(0.90, ses)), ag, claude)
    assert m == "agy" and est_s == {"motor": "agy", "ate": ses} and "sessao de 5 h" in motivo and "90%" in motivo, (m, est_s, motivo)
    assert escolher(U(c=(0.99, ses)), ag, claude)[0] == "agy"
    # as duas estouradas: uma frase com as duas causas, espera ate o reset da mais longa
    m, est_d, motivo = escolher(U((0.60, sem), (0.95, ses)), ag, claude)
    assert m == "agy" and est_d["ate"] == sem and "na semana" in motivo and "sessao" in motivo, (m, est_d, motivo)
    # semana alta mas sessao baixa (e vice-versa) nao se anulam
    assert escolher(U((0.60, sem), (0.10, ses)), ag, claude)[0] == "agy" and escolher(U((0.10, sem), (0.95, ses)), ag, claude)[0] == "agy"

    # espera: sem dado novo (Claude nao esta sendo usado), segue no agy ate o reset
    assert escolher(U(), ag + 1 * h, est_s)[0] == "agy" and escolher(U(c=(0.2, ses)), ag + 1 * h, est_s)[0] == "agy"
    # passou o reset da sessao: volta ao Claude (a semana segue abaixo do limite)
    m, volta, motivo = escolher(U((0.20, sem)), ses + 1, est_s)
    assert m == "claude" and volta == claude and "resetou" in motivo, (m, volta, motivo)
    assert escolher(U(), ses, est_s)[0] == "claude"   # no instante exato do reset ja volta
    # ...mas se a semana tambem estiver estourada, nao volta
    assert escolher(U((0.70, sem)), ses + 1, est_s)[0] == "agy"
    # passou o reset da semana: volta
    assert escolher(U(), sem + 1, est)[0] == "claude"

    # uso desconhecido, invalido ou de janela ja resetada nunca troca; reset invalido segura 12 h
    assert escolher(U(), ag, claude)[0] == "claude" and escolher(U(("lixo", sem), ("lixo", ses)), ag, claude)[0] == "claude"
    assert escolher(U((0.9, ag - 1), (0.99, ag - 1)), ag, claude)[0] == "claude"
    m, e12, _ = escolher(U((0.9, None)), ag, claude)
    assert m == "agy" and e12["ate"] == ag + ESPERA_SEM_RESET
    assert escolher(U((0.9, "lixo")), ag, claude)[0] == "agy"
    # agy ausente: fica no Claude, mesmo com uso alto e com espera gravada
    assert escolher(U((0.99, sem), (0.99, ses)), ag, claude, agy_ok=False)[0] == "claude" and escolher(U(), ag, est, agy_ok=False)[0] == "claude"
    # limites configuraveis
    assert escolher(U((0.70, sem)), ag, claude, limite=0.80)[0] == "claude" and escolher(U((0.85, sem)), ag, claude, limite=0.80)[0] == "agy"
    assert escolher(U(c=(0.92, ses)), ag, claude, limite_sessao=0.95)[0] == "claude" and escolher(U(c=(0.96, ses)), ag, claude, limite_sessao=0.95)[0] == "agy"

    # reset absurdo nao prende o agy: limitado a agora + 8 dias; reset em milissegundos e invalido (espera de 12 h)
    oito = ag + ESPERA_MAX
    assert escolher(U((0.9, 1e20)), ag, claude)[1]["ate"] == ag + ESPERA_SEM_RESET            # > 1e11: invalido
    assert escolher(U((0.9, 9e10)), ag, claude)[1]["ate"] == oito                              # segundos, mas longe demais
    assert escolher(U(c=(0.95, ag + 30 * 24 * h)), ag, claude)[1]["ate"] == oito
    assert escolher(U((0.9, 1.8e12)), ag, claude)[1]["ate"] == ag + ESPERA_SEM_RESET   # resetsAt em ms
    assert escolher(U((0.9, float("nan"))), ag, claude)[1]["ate"] == ag + ESPERA_SEM_RESET
    # `ate` gravado absurdo: limitado, e o limite fica gravado (nao anda com o relogio)
    m, est_x, _ = escolher(U(), ag, {"motor": "agy", "ate": 1e20})
    assert m == "agy" and est_x == {"motor": "agy", "ate": oito}, est_x
    assert escolher(U(), ag + 5 * 24 * h, est_x) == ("agy", est_x, escolher(U(), ag + 5 * 24 * h, est_x)[2])
    assert escolher(U(), oito + 1, est_x)[0] == "claude"                                  # passou dos 8 dias: volta
    assert _num(10 ** 400) is None and espera_ativa({"motor": "agy", "ate": 10 ** 400}, ag) == ag + ESPERA_MAX   # inteiro gigante nao derruba nada
    for lixo in (float("inf"), float("nan"), True, "1e20", None):
        assert espera_ativa({"motor": "agy", "ate": lixo}, ag) in (None, oito), lixo
    assert espera_ativa({"motor": "agy", "ate": float("inf")}, ag) == oito and espera_ativa({"motor": "agy", "ate": True}, ag) is None
    assert espera_ativa({"motor": "claude", "ate": ag + h}, ag) is None and espera_ativa({"motor": "agy", "ate": ag + h}, ag) == ag + h

    # decidir(): le LIMITS, grava e retoma a espera depois de "reiniciar" (arquivo no disco)
    import motor_agy
    real = motor_agy.instalado
    motor_agy.instalado = lambda: True
    try:
        arq = pathlib.Path(tempfile.mkdtemp()) / "motor.json"
        assert decidir({}, ag, arq) == "claude" and not arq.exists()                         # sem dado: nada gravado
        assert decidir({"seven_day": {"used": 0.40, "resets": sem}, "five_hour": {"used": 0.30, "resets": ses}}, ag, arq) == "claude"
        assert decidir({"seven_day": {"used": 0.40, "resets": sem}, "five_hour": {"used": 0.92, "resets": ses}}, ag + 1, arq) == "agy"
        assert json.loads(arq.read_text(encoding="utf-8")) == {"motor": "agy", "ate": ses}
        assert decidir({}, ag + h, arq) == "agy"                                             # "reiniciou": LIMITS vazio, espera segue
        assert ULTIMO["motor"] == "agy" and "agy ate" in ULTIMO["motivo"]
        # sessao resetou e o numero velho (0.92) ainda esta em LIMITS: descartado, volta ao Claude
        assert decidir({"seven_day": {"used": 0.40, "resets": sem}, "five_hour": {"used": 0.92, "resets": ses}}, ses + 5, arq) == "claude"
        assert json.loads(arq.read_text(encoding="utf-8")) == claude
        # semana estourada: vai e fica ate o reset da semana
        assert decidir({"seven_day": {"used": 0.60, "resets": sem}}, ag + 3 * h, arq) == "agy" and json.loads(arq.read_text(encoding="utf-8"))["ate"] == sem
        arq.write_text("{ isso nao e json", encoding="utf-8")
        assert decidir({}, ag, arq) == "claude"                                              # arquivo corrompido: Claude
        arq.write_text("[1, 2]", encoding="utf-8")
        assert decidir({}, ag, arq) == "claude"
        # inteiro de 400 digitos no arquivo: nao derruba (OverflowError) e o clamp de 8 dias e regravado
        arq.write_text('{"motor": "agy", "ate": ' + "9" * 400 + "}", encoding="utf-8")
        assert decidir({}, ag, arq) == "agy" and json.loads(arq.read_text(encoding="utf-8"))["ate"] <= ag + ESPERA_MAX
        # /api/status e o turno decidem ao mesmo tempo: o arquivo continua JSON valido e nao sobra temporario
        erros = []

        def laco(i):
            try:
                for k in range(30):
                    decidir({"seven_day": {"used": 0.9 if (i + k) % 2 else 0.1, "resets": sem}}, ag + k, arq)
            except Exception as e:  # noqa: BLE001
                erros.append(e)
        ts = [threading.Thread(target=laco, args=(i,)) for i in range(6)]
        for x in ts:
            x.start()
        for x in ts:
            x.join()
        assert not erros and isinstance(json.loads(arq.read_text(encoding="utf-8")), dict) and not list(arq.parent.glob("*.tmp")), erros
        # motor.json com `ate` absurdo: o limite de 8 dias e regravado; resetsAt em ms vira a espera de 12 h
        arq.write_text(json.dumps({"motor": "agy", "ate": 1e20}), encoding="utf-8")
        assert decidir({}, ag, arq) == "agy" and json.loads(arq.read_text(encoding="utf-8"))["ate"] == ag + ESPERA_MAX
        assert decidir({}, ag + ESPERA_MAX + 1, arq) == "claude"
        assert decidir({"seven_day": {"used": 0.70, "resets": 1.8e12}}, ag, arq) == "agy"
        assert json.loads(arq.read_text(encoding="utf-8"))["ate"] == ag + ESPERA_SEM_RESET

        # limites.json compartilhado: processo novo com LIMITS vazio usa o ultimo uso gravado; janela resetada e descartada
        la = arq.parent / "la" / "motor.json"
        lj = limites_arquivo(la)
        assert lj == la.parent / "limites.json"
        grava_limites({}, lj)
        assert not lj.exists()                                                               # nada a gravar: nao cria
        grava_limites({"seven_day": {"used": 0.60, "resets": sem}, "last": {"total": 1}}, lj, agora=ag)
        assert json.loads(lj.read_text(encoding="utf-8")) == {"em": ag, "janelas": {"seven_day": {"used": 0.60, "resets": sem}}}
        d = {}
        assert carrega_limites(d, lj) == ag and d == {"seven_day": {"used": 0.60, "resets": sem}}
        assert decidir({}, ag, la) == "agy" and json.loads(la.read_text(encoding="utf-8"))["ate"] == sem
        la.unlink()
        assert decidir({}, sem + 1, la) == "claude"                                          # semana resetou: dado velho descartado
        assert decidir({"seven_day": {"used": 0.10, "resets": sem}}, ag, la) == "claude"     # dado em memoria vence o arquivo
        lj.write_text('{"em": true, "janelas": [1]}', encoding="utf-8")
        assert carrega_limites({}, lj) == 0 and decidir({}, ag, la) == "claude"              # arquivo lixo: ignorado

        # motor fixo: "forcar" do arquivo > FEDORA_MOTOR (padrao) > regra; auto limpa; arquivo antigo = automatico
        fx = arq.parent / "forcar.json"
        fx.write_text(json.dumps({"motor": "agy", "ate": ag + h}), encoding="utf-8")   # formato antigo, sem "forcar"
        assert forcado("auto", fx) == "" and forcado("agy", fx) == "agy" and forcado("claude", fx) == "claude"
        for ruim in ("gpt", "xyz"):
            assert forcado(ruim, fx) == "claude", ruim                                    # invalido: Claude
        assert definir_forcado(" AGY ", fx) == "agy" and forcado("claude", fx) == "agy" and forcado("auto", fx) == "agy"
        assert json.loads(fx.read_text(encoding="utf-8")) == {"motor": "agy", "ate": ag + h, "forcar": "agy"}   # espera mantida
        assert decidir({}, ag, fx) == "agy" and json.loads(fx.read_text(encoding="utf-8"))["forcar"] == "agy"
        assert decidir({}, ag + 2 * h, fx) == "claude" and json.loads(fx.read_text(encoding="utf-8"))["forcar"] == "agy"   # espera venceu, fixo fica
        assert definir_forcado("claude", fx) == "claude" and forcado("agy", fx) == "claude"
        assert definir_forcado("auto", fx) == "auto" and forcado("auto", fx) == "" and forcado("agy", fx) == "agy"
        assert json.loads(fx.read_text(encoding="utf-8"))["forcar"] is None
        nv = arq.parent / "novo.json"
        assert definir_forcado("agy", nv) and json.loads(nv.read_text(encoding="utf-8")) == {"forcar": "agy", "motor": "claude", "ate": None}
        assert decidir({}, ag, nv) == "claude"   # o fixo nao entra na regra: quem aplica a precedencia e o runtime
        for ruim in ("gpt", "", None):
            try:
                definir_forcado(ruim, fx)
                raise AssertionError(ruim)
            except ValueError:
                pass
        fx.write_text(json.dumps({"forcar": "gpt"}), encoding="utf-8")
        assert forcado("auto", fx) == ""                                                   # "forcar" lixo no arquivo: ignorado

        # agy sem cota: com o bloqueio no futuro a regra nunca devolve agy; vencido, volta ao normal
        alto = {"seven_day": {"used": 0.70, "resets": sem}, "five_hour": {"used": 0.95, "resets": ses}}
        bq = arq.parent / "bloq.json"
        assert bloquear_agy(ag + h, ag, bq) == ag + h
        assert json.loads(bq.read_text(encoding="utf-8")) == {"agy_bloqueado_ate": ag + h, "motor": "claude", "ate": None}
        assert decidir(alto, ag, bq) == "claude" and "sem cota" in ULTIMO["motivo"]
        assert decidir(alto, ag + h - 1, bq) == "claude" and json.loads(bq.read_text(encoding="utf-8"))["agy_bloqueado_ate"] == ag + h
        assert decidir(alto, ag + h, bq) == "agy" and json.loads(bq.read_text(encoding="utf-8"))["agy_bloqueado_ate"] == ag + h
        # bloquear no meio de uma espera no agy: volta ao Claude ja no proximo turno
        assert bloquear_agy(None, ag + h, bq) == ag + h + BLOQUEIO_PADRAO and decidir({}, ag + h + 1, bq) == "claude"
        # reset invalido, passado ou em ms vira o padrao curto; absurdo fica no teto de 8 dias
        for ruim in (None, "lixo", ag - 1, 1.8e12, float("nan")):
            assert bloquear_agy(ruim, ag, bq) == ag + BLOQUEIO_PADRAO, ruim
        assert bloquear_agy(9e10, ag, bq) == ag + ESPERA_MAX
        for lixo in (True, "1e20", None, float("nan"), ag - 1):
            assert agy_bloqueado({"agy_bloqueado_ate": lixo}, ag) is None, lixo
        assert agy_bloqueado({"agy_bloqueado_ate": float("inf")}, ag) == ag + ESPERA_MAX
        # precedencia intacta: o fixo gravado sobrevive ao bloqueio e vice-versa (quem aplica "forcar" e o runtime)
        definir_forcado("agy", bq)
        assert forcado("claude", bq) == "agy" and json.loads(bq.read_text(encoding="utf-8"))["agy_bloqueado_ate"] == ag + ESPERA_MAX
        assert decidir(alto, ag, bq) == "claude" and json.loads(bq.read_text(encoding="utf-8"))["forcar"] == "agy"

        # comando local de voz/chat
        import commands
        for frase in ("o agy está sem cota", "Fedora, o agy ta sem cota", "agy sem cota", "o gemini acabou a cota"):
            assert commands.motor_local(frase) == ("agy", "sem_cota"), (frase, commands.motor_local(frase))
        for frase in ("o claude esta sem cota", "o agy esta sem cota pra revisar isso", "sem cota"):
            assert commands.motor_local(frase) != ("agy", "sem_cota"), frase
        assert commands.motor_local("troca o motor pro agy") == ("agy", "troca")   # comando antigo intacto
    finally:
        motor_agy.instalado = real
    print("motor_regra ok")


if __name__ == "__main__":
    _teste()
