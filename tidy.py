"""Rewrite dictated text into a clean Markdown block with the local Ollama model."""
import json
import os
import urllib.error
import urllib.request

import sysinfo

MODEL = os.environ.get("JARVIS_OLLAMA_MODEL", "llama3.2:3b")
PROMPT = """Você organiza anotações ditadas em português para uma nota diária do Obsidian.
Reescreva o texto abaixo em Markdown limpo:
- Corrija pontuação e maiúsculas; não invente nada que não foi dito.
- Agrupe por assunto com títulos "## Assunto" curtos.
- Coisas a fazer viram "- [ ] tarefa #to-do".
- Responda só com o Markdown, sem comentários.

Texto ditado:
"""


def tidy(text):
    body = json.dumps(dict(model=MODEL, prompt=PROMPT + text, stream=False,
                           options=dict(temperature=0.2))).encode()
    req = urllib.request.Request(sysinfo.OLLAMA_URL + "/api/generate", body,
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r).get("response", "").strip()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Ollama recusou ({e.code}): o modelo {MODEL} está instalado? "
                           f"Rode: ollama pull {MODEL}") from e
    except OSError as e:
        raise RuntimeError("Ollama não está rodando em " + sysinfo.OLLAMA_URL) from e
