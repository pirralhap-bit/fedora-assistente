"""Gemini API (Google) for the two small jobs that used to run on Ollama: picking a shortcut and tidying the daily note.

Key and models come from .env: GEMINI_API_KEY (required), GEMINI_MODEL (notes), GEMINI_MODEL_RAPIDO (shortcuts).
"""
import http.client
import json
import os
import pathlib
import threading
import time
import urllib.error
import urllib.request

_env = pathlib.Path(__file__).with_name(".env")
if _env.exists():   # server.py already loads it; this covers running a module's self-test directly
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# comma-separated: the next one is tried when the first is overloaded (503/429) or slow
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview,gemini-3.5-flash")
MODEL_RAPIDO = os.environ.get("GEMINI_MODEL_RAPIDO", "gemini-3.1-flash-lite")
URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
# last call, for the orbit/sysinfo: model, seconds, "ok" / "cota" / "erro", when; "disjuntor" while the fast path is off
STATUS = {}
DISJUNTOR_SEG = 300          # after a failure the fast path (atalhos.classify) skips the network for 5 minutes
_DISJUNTOR = {"ate": 0.0}    # monotonic deadline; 0 = closed
_relogio = time.monotonic    # the self-test swaps in a fake clock
_REPETE = (429, 500, 502, 503, 504)   # worth trying the next model; 400/401/403 (bad request, bad key) never are


class LLMError(RuntimeError):
    def __init__(self, msg, tipo="erro"):
        super().__init__(msg)
        self.tipo = tipo     # "chave" | "cota" | "servidor" | "recusa" | "tempo" | "rede" | "formato" | "disjuntor"


def ready():
    return bool(os.environ.get("GEMINI_API_KEY"))


def _post(model, body, timeout):
    req = urllib.request.Request(URL.format(model), json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _post_ate(model, body, prazo):
    """_post, but never past the monotonic deadline: the socket timeout alone resets on every byte (a slow drip
    can go on for minutes). Past the deadline the request is abandoned in its daemon thread (it ends at its socket
    timeout) and TimeoutError is raised."""
    resto = prazo - _relogio()
    if resto <= 0:
        raise TimeoutError("prazo esgotado")
    caixa = {}

    def faz():
        try:
            caixa["ok"] = _post(model, body, resto)
        except BaseException as e:  # noqa: BLE001 — re-raised in the caller's thread
            caixa["erro"] = e
    th = threading.Thread(target=faz, daemon=True, name="gemini")
    th.start()
    th.join(resto)
    if th.is_alive():
        raise TimeoutError("prazo esgotado")
    if "erro" in caixa:
        raise caixa["erro"]
    return caixa["ok"]


def _detalhe(e):
    try:
        return e.read().decode(errors="replace")[:200]
    except Exception:  # noqa: BLE001
        return ""


def _one(model, prompt, prazo, temperature, thinking):
    cfg = {"responseMimeType": "application/json", "temperature": temperature, "thinkingConfig": {"thinkingLevel": thinking}}
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": cfg}
    try:
        return _post_ate(model, body, prazo)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        detail = _detalhe(e)
        if "api key" in detail.lower() or "api_key" in detail.lower():   # Gemini answers a bad key with 400: no retry
            raise LLMError(f"Gemini recusou a chave (400): {detail}", "chave") from e
        cfg.pop("thinkingConfig")          # a model without that thinking level: ask again with its default
        return _post_ate(model, body, prazo)


def disjuntor_aberto():
    return _DISJUNTOR["ate"] > _relogio()


def _abre_disjuntor(e):
    _DISJUNTOR["ate"] = _relogio() + DISJUNTOR_SEG
    ate = time.time() + DISJUNTOR_SEG
    STATUS["disjuntor"] = dict(ate=ate, motivo=e.tipo)
    print(f"[gemini] atalho rápido desligado até {time.strftime('%H:%M:%S', time.localtime(ate))} "
          f"({e.tipo}: {str(e)[:120]}); até lá tudo vai direto para o Claude", flush=True)


def _fecha_disjuntor():
    if STATUS.pop("disjuntor", None):
        print("[gemini] atalho rápido religado", flush=True)
    _DISJUNTOR["ate"] = 0.0


def json_call(prompt, model=None, timeout=60, temperature=0.2, thinking="minimal", disjuntor=False):
    """The model's answer parsed as JSON; falls through the comma-separated models; LLMError on anything wrong.
    timeout is the TOTAL time for the call (every model and retry included).
    disjuntor=True (the fast path): while a recent failure has it open, fail at once without touching the network;
    a failure (anything but a malformed answer) opens it for DISJUNTOR_SEG, a success closes it."""
    if disjuntor and disjuntor_aberto():
        raise LLMError("Atalho rápido desligado por alguns minutos (o Gemini falhou há pouco).", "disjuntor")
    t0 = time.time()
    try:
        out = _json_call(prompt, model, _relogio() + timeout, temperature, thinking)
    except LLMError as e:
        STATUS.update(estado="cota" if e.tipo == "cota" else "erro", seg=round(time.time() - t0, 1), at=time.time())
        # decided (QA, lote B): "recusa" (another 400) opens it too; only a malformed answer doesn't
        if disjuntor and e.tipo != "formato":
            _abre_disjuntor(e)
        raise
    STATUS.update(estado="ok", seg=round(time.time() - t0, 1), at=time.time())
    if disjuntor:
        _fecha_disjuntor()
    return out


def _json_call(prompt, model, prazo, temperature, thinking):
    if not ready():
        raise LLMError("Falta a chave do Gemini (GEMINI_API_KEY no .env).", "chave")
    modelos = [m.strip() for m in (model or MODEL).split(",") if m.strip()]
    for i, m in enumerate(modelos):
        last = i == len(modelos) - 1 or prazo - _relogio() <= 0
        try:
            STATUS["modelo"] = m
            out = _one(m, prompt, prazo, temperature, thinking)
            break
        except urllib.error.HTTPError as e:
            if e.code in _REPETE and not last:
                continue
            tipo = "cota" if e.code == 429 else "servidor" if e.code >= 500 else "chave" if e.code in (401, 403) else "recusa"
            raise LLMError(f"Gemini recusou ({e.code}): {_detalhe(e)}", tipo) from e
        except (OSError, http.client.HTTPException) as e:   # timeout, no network, connection cut, IncompleteRead
            if not last:
                continue
            tipo = "tempo" if isinstance(e, TimeoutError) or "timed out" in str(e) else "rede"
            raise LLMError("Sem resposta do Gemini (internet ou serviço lento).", tipo) from e
        except ValueError as e:   # JSONDecodeError: an HTML error page or a cut body instead of JSON
            raise LLMError("O Gemini respondeu fora do formato.", "formato") from e
    try:
        text = "".join(p.get("text", "") for p in out["candidates"][0]["content"]["parts"])
        return json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
        raise LLMError("O Gemini respondeu fora do formato.", "formato") from e


def _autoteste():
    """Offline: _post and the clock are fakes (python llm.py --teste). Without --teste, one real call."""
    import io
    os.environ["GEMINI_API_KEY"] = "x"
    real_post, chamadas = _post, []
    ok_body = {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}

    def herr(code, corpo=b"{}"):
        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(corpo))

    def falso(*erros, dorme=0):
        def post(model, body, timeout):
            chamadas.append(model)
            if dorme:
                time.sleep(dorme)
            if len(chamadas) <= len(erros):   # the n-th call raises the n-th error, later calls succeed
                raise erros[len(chamadas) - 1]
            return ok_body
        chamadas.clear()
        return post

    def falha(tipo, *erros, modelos="a,b", n=None, prazo=2, dorme=0):
        globals()["_post"] = falso(*erros, dorme=dorme)
        try:
            json_call("p", modelos, timeout=prazo)
        except LLMError as e:
            assert e.tipo == tipo, (tipo, e.tipo, e)
            assert n is None or len(chamadas) == n, (tipo, chamadas)
            return
        raise AssertionError(f"{tipo}: no LLMError")

    try:
        # every escape the old code let through (JSONDecodeError, IncompleteRead) is an LLMError now
        falha("formato", json.JSONDecodeError("x", "<html>", 0), n=1)
        falha("rede", http.client.IncompleteRead(b""), http.client.IncompleteRead(b""), n=2)
        falha("rede", http.client.RemoteDisconnected("x"), http.client.RemoteDisconnected("x"), n=2)
        falha("rede", ConnectionResetError("x"), OSError("x"), n=2)
        falha("formato", ValueError("x"), n=1)
        # 400 bad key / 401 / 403: one call, never the next model; 429/5xx: the next model is tried
        falha("chave", herr(400, b'{"error": {"message": "API key not valid", "status": "INVALID_ARGUMENT"}}'), n=1)
        falha("chave", herr(401), n=1)
        falha("chave", herr(403), n=1)
        falha("cota", herr(429), herr(429), n=2)
        falha("servidor", herr(503), herr(500), n=2)
        globals()["_post"] = falso(herr(503))
        assert json_call("p", "a,b", timeout=2) == {"ok": True} and chamadas == ["a", "b"]
        globals()["_post"] = falso(herr(400, b'{"error": {"message": "thinking level not supported"}}'))
        assert json_call("p", "a", timeout=2) == {"ok": True} and chamadas == ["a", "a"]   # retried without thinking
        # TOTAL deadline: a slow server (3 s per call, 2 models) still ends at the 0.5 s budget
        t0 = time.monotonic()
        falha("tempo", modelos="a,b", dorme=3, prazo=0.5)
        assert time.monotonic() - t0 < 1.2, time.monotonic() - t0
        # circuit breaker with a fake clock
        agora = [1000.0]
        globals()["_relogio"] = lambda: agora[0]
        globals()["_post"] = falso(herr(503))
        try:
            json_call("p", "a", timeout=2, disjuntor=True)
        except LLMError as e:
            assert e.tipo == "servidor"
        assert disjuntor_aberto() and STATUS["disjuntor"]["motivo"] == "servidor" and len(chamadas) == 1
        try:
            json_call("p", "a", timeout=2, disjuntor=True)
            raise AssertionError("open breaker must not call")
        except LLMError as e:
            assert e.tipo == "disjuntor" and len(chamadas) == 1, chamadas      # no network while open
        assert json_call("p", "a", timeout=2) == {"ok": True}                  # tidy (no breaker) still calls
        agora[0] += DISJUNTOR_SEG - 1
        assert disjuntor_aberto()
        agora[0] += 2
        assert not disjuntor_aberto()
        globals()["_post"] = falso()
        assert json_call("p", "a", timeout=2, disjuntor=True) == {"ok": True}
        assert "disjuntor" not in STATUS and not disjuntor_aberto() and STATUS["estado"] == "ok"
        globals()["_post"] = falso(json.JSONDecodeError("x", "", 0))           # a malformed answer doesn't trip it
        try:
            json_call("p", "a", timeout=2, disjuntor=True)
        except LLMError:
            pass
        assert not disjuntor_aberto()
    finally:
        globals()["_post"], globals()["_relogio"] = real_post, time.monotonic
        _DISJUNTOR["ate"] = 0.0
        STATUS.clear()
    print("llm ok (offline)")


if __name__ == "__main__":
    import sys
    if "--teste" in sys.argv:
        _autoteste()
    else:
        print(json_call('Responda só JSON: {"ok": true}', MODEL_RAPIDO, timeout=20))
        print("llm ok")
