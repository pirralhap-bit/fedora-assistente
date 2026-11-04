"""Rewrite dictated text into a clean Markdown block with the local Ollama model."""
import json
import os
import re
import urllib.error
import urllib.request

import modelo
import sysinfo

from modelo import MODEL  # noqa: E402  (one model setting for notes and the window hold)
PROMPT = """Você organiza uma nota diária do Obsidian, em português. Abaixo está o corpo da nota inteiro, com as
seções "## ..." e, misturado, texto ditado solto. Separe TUDO em itens curtos e coloque cada um na seção certa:
- "Para hoje": o que precisa ser feito hoje. Formato "- [ ] Fazer Y #to-do"
- "Pendências": o que fica para depois (amanhã, semana que vem, algum dia). Formato "- [ ] Fazer Y #to-do"
- "Resumo do dia": o que já aconteceu ou foi feito. Formato "- Fiz X."
- Itens que já estavam na nota continuam (pode mudar de seção). Tarefa marcada "- [x]" continua marcada.
- Uma frase ditada com várias coisas vira vários itens. Corrija pontuação, junte repetições, não invente nada.
Responda só JSON com uma lista por seção, usando os nomes das seções da nota:
{"Para hoje": ["- [ ] ..."], "Pendências": ["- [ ] ..."], "Resumo do dia": ["- ..."]}

Exemplo
Corpo: "## Para hoje\n- [ ] Ligar pro João #to-do\n## Pendências\n## Resumo do dia\nhoje arrumei a impressora e amanhã preciso trocar o bico"
Saída: {"Para hoje": ["- [ ] Ligar pro João #to-do"], "Pendências": ["- [ ] Trocar o bico da impressora (amanhã) #to-do"], "Resumo do dia": ["- Arrumei a impressora."]}

Corpo da nota:
"""
_FRONT = re.compile(r"\A---\n.*?\n---\n", re.S)


def _itens(md):
    """Normalized text of every list item: what the rewrite is not allowed to lose."""
    out = []
    for line in md.splitlines():
        m = re.match(r"\s*[-*]\s+(\[[ xX]\]\s+)?(.*)", line)
        t = re.sub(r"#to-do|[^\w\s]", " ", (m.group(2) if m else "")).casefold().split()
        if t:
            out.append(t)
    return out


def check(before, after):
    """Raise unless `after` keeps every '#' line and every list item of `before` (the model may only move or polish them)."""
    keep = [l.strip() for l in before.splitlines() if l.strip().startswith("#")]
    lost = [l for l in keep if l not in {x.strip() for x in after.splitlines()}]
    novos = [set(t) for t in _itens(after)]
    lost += [" ".join(t) for t in _itens(before) if not any(len(set(t) & n) >= 0.8 * len(set(t)) for n in novos)]
    if lost:
        raise RuntimeError("O Ollama perdeu parte da nota (" + "; ".join(lost[:3]) + "). Nada foi mudado.")


def _montar(body, secoes):
    """Rebuild the body: what sits above the first '##' as is, then each section in the note's order, then new ones."""
    topo = body.split("\n## ", 1)[0] if not body.lstrip().startswith("## ") else ""
    titulos = re.findall(r"^## (.+?)\s*$", body, flags=re.M)
    titulos += [t for t in secoes if t not in titulos]
    partes = [topo.strip()] if topo.strip() else []
    for t in titulos:
        itens = [str(i).strip() for i in secoes.get(t) or [] if str(i).strip().lstrip("-* ").strip()]
        partes.append(f"## {t}\n" + "\n".join(i if i.startswith(("-", "*")) else "- " + i for i in itens))
    return "\n\n".join(partes) + "\n"


def tidy_note(note):
    """The whole daily note, reorganized. Frontmatter untouched; refuses a rewrite that drops anything."""
    m = _FRONT.match(note)
    head, body = (m.group(0), note[m.end():]) if m else ("", note)
    body = body.strip() + "\n"
    req = urllib.request.Request(sysinfo.OLLAMA_URL + "/api/generate", json.dumps(dict(
        model=MODEL, prompt=PROMPT + body, stream=False, think=False, format="json",  # qwen3 would "think" for seconds first
        keep_alive=modelo.keep_alive(), options=dict(temperature=0.2, num_ctx=8192))).encode(),
        {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            secoes = json.loads(json.load(r).get("response") or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Ollama recusou ({e.code}): o modelo {MODEL} está instalado? "
                           f"Rode: ollama pull {MODEL}") from e
    except OSError as e:
        raise RuntimeError("Ollama não está rodando em " + sysinfo.OLLAMA_URL) from e
    except ValueError as e:
        raise RuntimeError("O Ollama respondeu fora do formato. Nada foi mudado.") from e
    if not isinstance(secoes, dict):
        raise RuntimeError("O Ollama respondeu fora do formato. Nada foi mudado.")
    out = _montar(body, secoes)
    check(body, out)
    return head + "\n" + out


if __name__ == "__main__":
    nota = "---\ndata: 2026-11-04\n---\n\n#Termometro-\n\n## Para hoje\n- [ ] Ligar pro cliente #to-do\n\n## Pendências\n\n## Resumo do dia\n- Troquei o bico.\n"
    check(nota, nota)
    try:
        check(nota, nota.replace("- Troquei o bico.", ""))
        raise SystemExit("check deixou passar um item perdido")
    except RuntimeError:
        pass
    ditado = nota.replace("## Pendências\n", "## Pendências\nhoje eu imprimi o suporte da câmera e amanhã preciso comprar filamento pla preto e semana que vem ver o orçamento do gabinete\n")
    out = tidy_note(ditado)
    print(out)
    assert out.startswith("---\ndata: 2026-11-04\n---") and "#Termometro-" in out and out.count("#to-do") >= 3, out
    print("tidy ok")
