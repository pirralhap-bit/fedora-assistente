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
- o uso do agy NAO entra na regra (decisao do usuario, 2026-11-09): o agy e so o motor de economia;
- agy nao instalado: continua no Claude (nunca troca para um motor que nao existe);
- reset invalido (nao numerico ou em milissegundos, > 1e11) segura 12 h; nenhum reset, lido ou gravado, passa de agora + 8 dias.

"No agy ate o reset" fica gravado em arquivo: o dado do Claude so reaparece depois da 1a pergunta ao Claude, e um
reinicio do servidor nao pode devolver a conversa ao Claude no meio da espera.
`FEDORA_MOTOR=claude|agy` no .env forca um motor e ignora esta regra; `auto` (padrao) usa a regra.
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
RESET_MAX_EPOCH = 1e11         # acima disso e milissegundo (ou lixo), nao segundo: reset invalido

_LOCAL = pathlib.Path(os.environ.get("LOCALAPPDATA") or pathlib.Path.home() / ".local" / "state") / "fedora-assistente"
ARQUIVO = pathlib.Path(os.environ.get("FEDORA_MOTOR_ESTADO") or _LOCAL / "motor.json")
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


_TRAVA = threading.Lock()   # /api/status (thread HTTP) e o turno (outra thread) chamam decidir() ao mesmo tempo


def decidir(limits, agora=None, arquivo=None, limite=None, limite_sessao=None):
    with _TRAVA:
        return _decidir(limits, agora, arquivo, limite, limite_sessao)


def _decidir(limits, agora=None, arquivo=None, limite=None, limite_sessao=None):
    """Motor deste turno ('claude' ou 'agy') pelo uso do Claude em `limits` (runtime.LIMITS); grava a espera no arquivo."""
    agora = time.time() if agora is None else agora
    arquivo = arquivo or ARQUIVO
    usos = {}
    for janela in ("seven_day", "five_hour"):
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
    motor, depois, motivo = escolher(usos, agora, antes, agy_ok, limite, limite_sessao)
    if depois != antes and (depois.get("motor") == "agy" or antes):
        _grava(arquivo, depois)
    ULTIMO.update(motor=motor, motivo=motivo)
    return motor


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
    finally:
        motor_agy.instalado = real
    print("motor_regra ok")


if __name__ == "__main__":
    _teste()
