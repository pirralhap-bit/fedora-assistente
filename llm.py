"""Gemini API (Google) for the two small jobs that used to run on Ollama: picking a shortcut and tidying the daily note.

Key and models come from .env: GEMINI_API_KEY (required), GEMINI_MODEL (notes), GEMINI_MODEL_RAPIDO (shortcuts).
"""
import json
import os
import pathlib
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
# last call, for the orbit: model, seconds, "ok" / "cota" / "erro", when
STATUS = {}


class LLMError(RuntimeError):
    pass


def ready():
    return bool(os.environ.get("GEMINI_API_KEY"))


def _post(model, body, timeout):
    req = urllib.request.Request(URL.format(model), json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _one(model, prompt, timeout, temperature, thinking):
    cfg = {"responseMimeType": "application/json", "temperature": temperature, "thinkingConfig": {"thinkingLevel": thinking}}
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": cfg}
    try:
        return _post(model, body, timeout)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        cfg.pop("thinkingConfig")          # a model without that thinking level: ask again with its default
        return _post(model, body, timeout)


def json_call(prompt, model=None, timeout=60, temperature=0.2, thinking="minimal"):
    """The model's answer parsed as JSON; falls through the comma-separated models; LLMError on anything wrong."""
    t0 = time.time()
    try:
        out = _json_call(prompt, model, timeout, temperature, thinking)
    except LLMError as e:
        STATUS.update(estado="cota" if "(429)" in str(e) else "erro", seg=round(time.time() - t0, 1), at=time.time())
        raise
    STATUS.update(estado="ok", seg=round(time.time() - t0, 1), at=time.time())
    return out


def _json_call(prompt, model, timeout, temperature, thinking):
    if not ready():
        raise LLMError("Falta a chave do Gemini (GEMINI_API_KEY no .env).")
    modelos = [m.strip() for m in (model or MODEL).split(",") if m.strip()]
    for i, m in enumerate(modelos):
        last = i == len(modelos) - 1
        try:
            STATUS["modelo"] = m
            out = _one(m, prompt, timeout, temperature, thinking)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503, 504) and not last:
                continue
            detail = e.read().decode(errors="replace")[:200]
            raise LLMError(f"Gemini recusou ({e.code}): {detail}") from e
        except OSError as e:
            if not last:
                continue
            raise LLMError("Sem resposta do Gemini (internet ou serviço lento).") from e
    try:
        text = "".join(p.get("text", "") for p in out["candidates"][0]["content"]["parts"])
        return json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise LLMError("O Gemini respondeu fora do formato.") from e


if __name__ == "__main__":
    print(json_call('Responda só JSON: {"ok": true}', MODEL_RAPIDO, timeout=20))
    print("llm ok")
