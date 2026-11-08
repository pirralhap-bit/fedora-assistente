"""Rewrite dictated text into a clean Markdown block with Gemini (llm.py)."""
import collections
import re
import unicodedata

import llm

PROMPT = """Você organiza uma nota diária do Obsidian, em português. Abaixo está o corpo da nota inteiro, com as
seções "## ..." e, misturado, texto ditado solto. Separe TUDO em itens curtos e coloque cada um na seção certa:
- "Para hoje": o que precisa ser feito hoje. Formato "- [ ] Fazer Y #to-do"
- "Pendências": o que fica para depois (amanhã, semana que vem, algum dia). Formato "- [ ] Fazer Y #to-do"
- "Resumo do dia": o que já aconteceu ou foi feito. Formato "- Fiz X."
- Itens que já estavam na nota continuam (pode mudar de seção). Tarefa marcada "- [x]" continua marcada.
- "#done" fica escrito igual, sem traduzir nem apagar; tarefa feita nunca volta a ficar aberta.
- Uma frase ditada com várias coisas vira vários itens. Corrija pontuação, junte repetições, não invente nada.
Responda só JSON com uma lista por seção, usando os nomes das seções da nota:
{"Para hoje": ["- [ ] ..."], "Pendências": ["- [ ] ..."], "Resumo do dia": ["- ..."]}

Exemplo
Corpo: "## Para hoje\n- [ ] Ligar pro João #to-do\n## Pendências\n## Resumo do dia\nhoje arrumei a impressora e amanhã preciso trocar o bico"
Saída: {"Para hoje": ["- [ ] Ligar pro João #to-do"], "Pendências": ["- [ ] Trocar o bico da impressora (amanhã) #to-do"], "Resumo do dia": ["- Arrumei a impressora."]}

Corpo da nota:
"""
_FRONT = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.S)


# A line of the original "survives" only if an output line has the SAME words in the same order once _norm
# drops what the tidy may change (punctuation, accents, case, list/number/'#'/'>' markers, the '#to-do' tag).
# No fuzzy matching: one changed word ("não", "sem"->"com", a name, 15->5) is a different line. Numbers, dates,
# URLs and [[wikilinks|alias]] are whole words of the line, so they are compared line by line too.
# Task state is separate: [x] must stay [x], [ ] must stay [ ]; a plain line may become "- [ ]" (that's the tidy's
# job) and may gain '#to-do', but a line that had '#to-do' must keep it.
# Output lines with no counterpart in the original that are still tolerated: a section heading the note lacked
# ("## Pendências") and little else. More than this and the model is writing content of its own.
MAX_NOVAS_MIN, MAX_NOVAS_FRAC = 3, 0.10
_MARCA = re.compile(r"^(?:\s*(?:#{1,6}(?=\s|$)|[-*+](?=\s)|\d+[.)](?=\s)|>))+\s*")
_CAIXA = re.compile(r"\[([ xX])\]\s*")
_TODO = re.compile(r"(?<!\S)#to-do(?![\w-])")
_DONE = re.compile(r"(?<!\S)#done(?![\w-])")   # the user's "- #done Fazer X": same state as "- [x] Fazer X"
_PALAVRA = re.compile(r"\[\[[^\]\n]+\]\]|https?://[^\s<>()\[\]]+|\d+(?:[.,:/h-]\d+)*|\w+")


def _norm(line):
    """(words, box, has #to-do). words: the line's words in order — numbers/dates/URLs/wikilinks kept whole —
    lowercase and without accents; "" for a line with no word at all ('-', '#', '---', a code fence, '| --- |'),
    which check() ignores on both sides. box: 'x' ([x] or #done), ' ' or None (no checkbox)."""
    t = _MARCA.sub("", line.strip())
    m = _CAIXA.match(t)
    box = m.group(1).lower() if m else None
    if m:
        t = t[m.end():]
    if _DONE.search(t):
        box, t = "x", _DONE.sub(" ", t)
    todo = bool(_TODO.search(t))
    t = _TODO.sub(" ", t)
    t = unicodedata.normalize("NFKD", t.casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    words = " ".join(p.rstrip(".,;:") if not p[0].isalpha() else p for p in _PALAVRA.findall(t))
    return words, box, todo


# Loose DICTATED prose (a plain paragraph line, >= 4 words, no list/heading/table/code/task marker) may be split into
# items and moved between sections: that is the point of "Organizar". It is not compared line by line but by content:
#  - every sentence keeps its content words somewhere in the lines the output has left over: at most one may be
#    missing per PROSA_FALTA_POR content words (a short sentence loses none: "comprar filamento" -> "comprar tinta"
#    is refused). Words match by their first 4 letters, so "testei"/"testar" and plurals count as the same;
#  - every number, date, URL and [[wikilink]] of the prose appears in the output as many times as before;
#  - negations (não, nunca, sem, nem, jamais, nenhum/a) appear exactly as many times: one dropped or added "não"
#    flips a sentence;
#  - a leftover output line must be grounded in the note (>= GROUND_FRAC of its content words exist in the original),
#    so the model cannot add content of its own. Section headings it adds are free, up to MAX_TITULOS_NOVOS.
# Structured lines (lists, tasks, tables, code, headings) stay word for word, as above.
PROSA_MIN_PALAVRAS, PROSA_FALTA_POR, GROUND_FRAC, MAX_TITULOS_NOVOS = 4, 8, 0.6, 6
_NEGA = frozenset("nao nunca sem nem jamais nenhum nenhuma".split())
_ENCHIMENTO = frozenset((
    "hoje preciso precisa precisamos tambem entao depois ainda quando como mais muito pois porque para isso isto esse "
    "essa esses essas aquele aquela esta estou estava estao foram tenho temos devo deve quero queria vamos"
).split())   # fillers a rewrite may drop ("hoje" is implied by the section)
_FRASES = re.compile(r"[.!?;]+\s+|\n+")


def _conteudo(words):
    """Content words: 4+ letters, no number/URL/link, not a filler."""
    return [w for w in words.split() if len(w) >= 4 and w[0].isalpha() and w not in _ENCHIMENTO]


def _raiz(w):
    return w[:4]


_FICHA = re.compile(r"\[\[[^\]\n]+\]\]|https?://\S+|\d[\d.,:/h-]*")


def _fichas(words):
    """Numbers, dates, URLs and wikilinks of a normalized line (a wikilink has spaces inside, so no split())."""
    return [f.rstrip(".,;:") for f in _FICHA.findall(words)]


def _e_prosa(linha, words, box, todo, em_codigo):
    s = linha.strip()
    return (not em_codigo and box is None and not todo and len(words.split()) >= PROSA_MIN_PALAVRAS
            and not _MARCA.match(s) and not s.startswith(("|", "```", "~~~")) and not linha[:1] in ("\t", " "))


def _cabe(o, s):
    """Can output state s (box, todo) stand for original state o? A done task stays done ('[x] … #to-do' and
    '#done …' stand for each other); an open one stays open; a plain line may become '[ ]'; '#to-do' stays."""
    (ob, ot), (sb, st) = o, s
    if ob == "x":
        return sb == "x"
    return (ob == sb or (ob is None and sb == " ")) and (st or not ot)


def _corta(s, n=60):
    return s if len(s) <= n else s[:n - 1] + "…"


def _lista(xs):
    return "; ".join(f"«{_corta(x)}»" for x in xs[:5]) + ("; …" if len(xs) > 5 else "")


def check(before, after):
    """Raise (RuntimeError, nothing applied) unless `after` keeps the content of `before`: every structured line
    (headings, list items, tables, code, tasks and their state) word for word, in any order (the tidy regroups by
    section; duplicates count one by one); loose dictated prose by content (see PROSA_*); and invents nothing."""
    orig, em_codigo = [], False
    for l in before.splitlines():
        t = l.strip()
        fence = t.startswith(("```", "~~~"))
        n = _norm(l)
        if n[0]:                                       # wordless lines: ignored
            orig.append((n, l.strip(), _e_prosa(l, n[0], n[1], n[2], em_codigo or fence)))
        if fence:
            em_codigo = not em_codigo
    livres = collections.defaultdict(list)          # words -> [[box, todo, as written], ...] not yet used
    for l in after.splitlines():
        w, box, todo = _norm(l)
        if w:
            livres[w].append([box, todo, l.strip()])
    # strictest first ([x], then [ ], then plain; '#to-do' before none), each taking an exact state when it can
    orig.sort(key=lambda x: ({"x": 0, " ": 1, None: 2}[x[0][1]], not x[0][2]))
    perdidas, estado, prosa = [], [], []
    for (w, box, todo), l, e_prosa in orig:
        cands = livres.get(w, [])
        ok = [c for c in cands if _cabe((box, todo), (c[0], c[1]))]
        if ok:
            c = next((c for c in ok if (c[0], c[1]) == (box, todo)), ok[0])
            cands.remove(c)
        elif e_prosa:
            prosa.append((w, l))                     # rewritten dictation: judged by content below
        else:
            (estado if cands else perdidas).append(l)
    sobras = [(w, c[2]) for w, cs in livres.items() for c in cs]          # output lines nobody claimed
    erros = []
    if prosa:
        pool = collections.Counter(w for sw, _ in sobras for w in sw.split())
        raizes = {_raiz(w) for w in pool}
        faltou = []
        for _, l in prosa:                          # sentence by sentence: one dropped idea must show
            for frase in _FRASES.split(l):
                cont = _conteudo(_norm(frase)[0])
                if len(cont) >= 2 and sum(_raiz(w) not in raizes for w in cont) > len(cont) // PROSA_FALTA_POR:
                    faltou.append(frase.strip())
        if faltou:
            erros.append(f"perdeu ideia(s) do texto ditado ({len(faltou)}): " + _lista(faltou))
        pw = collections.Counter(f for ws, _ in prosa for f in _fichas(ws))
        tem = collections.Counter(_fichas(" ".join(sw for sw, _ in sobras)))
        mudou = sorted(f for f in pw if tem[f] < pw[f])
        if mudou:
            erros.append("mudou números/datas/links do texto ditado: " + _lista(mudou))
        neg_antes = sum(c for w, c in collections.Counter(x for pw_, _ in prosa for x in pw_.split()).items() if w in _NEGA)
        neg_depois = sum(c for w, c in pool.items() if w in _NEGA)
        if neg_antes != neg_depois:
            erros.append(f"mudou negações do texto ditado ({neg_antes} -> {neg_depois})")
    if perdidas:
        erros.append(f"perdeu ou mudou {len(perdidas)} linha(s): " + _lista(perdidas))
    if estado:
        erros.append(f"mudou o estado de {len(estado)} tarefa(s) ([x]/[ ]/#to-do): " + _lista(estado))
    # leftover output lines: headings are free (up to a few); the rest must be made of the note's own words
    base = {_raiz(w) for (ws, _, _), _, _ in orig for w in _conteudo(ws)}
    novas, titulos = [], 0
    for sw, linha in sobras:
        if linha.lstrip().startswith("#"):
            titulos += 1
            if titulos > MAX_TITULOS_NOVOS:
                novas.append(linha)
            continue
        cont = _conteudo(sw)
        if len(cont) >= 2 and sum(_raiz(w) in base for w in cont) < GROUND_FRAC * len(cont):
            novas.append(linha)
    if len(novas) > max(MAX_NOVAS_MIN, int(MAX_NOVAS_FRAC * len(orig))):
        erros.append(f"inventou {len(novas)} linha(s): " + _lista(novas))
    if erros:
        raise RuntimeError("O Gemini " + " | ".join(erros) + ". Nada foi mudado.")


def _montar(body, secoes):
    """Rebuild the body: what sits above the first '##' as is, then each section in the note's order, then new ones."""
    topo = body.split("\n## ", 1)[0] if not body.lstrip().startswith("## ") else ""
    titulos = re.findall(r"^## (.+?)\s*$", body, flags=re.M)
    titulos += [t for t in secoes if t not in titulos]
    partes = [topo.strip()] if topo.strip() else []
    # a line the note already had comes back exactly as written (indentation, a loose paragraph, an empty "- ",
    # a '---' rule); only text the model wrote itself gets the "- " of a list item
    ja = {}
    for l in body.splitlines():
        ja.setdefault(l.strip(), l.rstrip())
    for t in titulos:
        itens = [str(i).strip() for i in secoes.get(t) or [] if str(i).strip()]
        # tables, numbered items, quotes and code fences keep their own markup ("- | a |" would break the table)
        partes.append(f"## {t}\n" + "\n".join(ja[i] if i in ja else i if re.match(r"[-*+|>]|```|\d+[.)]\s", i)
                                              else "- " + i for i in itens))
    return "\n\n".join(partes) + "\n"


def tidy_note(note):
    """The whole daily note, reorganized. Frontmatter untouched; refuses a rewrite that drops anything.
    A UTF-8 BOM stays first (before the frontmatter), or Obsidian would no longer see the YAML."""
    bom = "\ufeff" if note.startswith("\ufeff") else ""
    note = note[len(bom):]
    m = _FRONT.match(note)
    head, body = (bom + m.group(0), note[m.end():]) if m else (bom, note)
    body = body.strip() + "\n"
    try:
        secoes = llm.json_call(PROMPT + body, llm.MODEL, timeout=60, thinking="low")
    except llm.LLMError as e:
        raise RuntimeError(str(e) + " Nada foi mudado.") from e
    if not isinstance(secoes, dict):
        raise RuntimeError("O Gemini respondeu fora do formato. Nada foi mudado.")
    out = _montar(body, secoes)
    check(body, out)
    return head + ("\n" if head.lstrip("\ufeff") else "") + out   # no stray blank line when there's no frontmatter


NOTA = """---
data: 2026-11-04
---

#Termometro-

## Para hoje
- [ ] Ligar pro cliente #to-do
- [x] Pagar 15 reais do frete #to-do
1. Separar o PLA preto
Parágrafo solto: conferir [[Projeto Gabinete]] e https://exemplo.com/peca?id=7 antes das 14:30.
| Peça | Gramas |
| --- | --- |
| Suporte | 42 |

## Pendências
```
G28 ; home
```

## Resumo do dia
- Troquei o bico.
- Troquei o bico.
"""


def _falha(antes, depois, trecho=""):
    try:
        check(antes, depois)
    except RuntimeError as e:
        assert "Nada foi mudado" in str(e) and trecho in str(e), e
        return str(e)
    raise AssertionError("check deixou passar:\n" + depois)


def _teste():
    """Offline: check(), _montar() and tidy_note() with a fake llm.json_call. No network."""
    import sys
    sys.stdout.reconfigure(errors="replace")   # cp1252/cp850 console: the error texts have «» and accents
    n = NOTA
    check(n, n)                                                      # same note
    check(n, n.replace("- [x] Pagar", "* [x] pagar").replace("Troquei o bico.\n- Troquei", "troquei o bico\n- Troquei"))
    _falha(n, n.replace("Parágrafo solto: conferir [[Projeto Gabinete]] e https://exemplo.com/peca?id=7 antes das 14:30.\n", ""),
           "Parágrafo solto")
    _falha(n, n.replace("| Peça | Gramas |\n| --- | --- |\n| Suporte | 42 |\n", ""), "linha(s)")
    print("  tabela omitida  ->", _falha(n, n.replace("| Suporte | 42 |\n", ""), "42"))
    print("  15 -> 51        ->", _falha(n, n.replace("15 reais", "51 reais"), "15"))
    _falha(n, n.replace("[[Projeto Gabinete]]", "Projeto Gabinete"), "[[projeto gabinete]]")
    _falha(n, n.replace("https://exemplo.com/peca?id=7", "https://exemplo.com/peca?id=8"), "id=7")
    _falha(n, n.replace("1. Separar o PLA preto\n", ""), "1. Separar o PLA preto")
    _falha(n, n.replace("G28 ; home\n", ""), "G28 ; home")
    _falha(n, n.replace("- Troquei o bico.\n- Troquei o bico.\n", "- Troquei o bico.\n"), "- Troquei o bico.")  # multiplicity
    _falha(n, n.replace("Ligar pro cliente", "Ligar pro fornecedor"), "Ligar pro cliente")
    _falha(n, n + "".join(f"- Item inventado número {chr(97 + i)}\n" for i in range(4)), "inventou 4")
    check(n, n + "- Item inventado a\n")                             # a heading or a split line is tolerated

    # one changed word is a changed line (no fuzzy matching), numbers line by line, task state kept (QA, lote C)
    for antes, depois, trecho in [
            ("- Eu decidi que não vou mais usar o bico de 0.6 nas peças do cliente\n",
             "- Eu decidi que vou mais usar o bico de 0.6 nas peças do cliente\n", "não vou"),
            ("- Nunca desligar a impressora durante a impressão da peça grande\n",
             "- Desligar a impressora durante a impressão da peça grande\n", "Nunca"),
            ("- Imprimir a peça sem suporte para o cliente da semana passada\n",
             "- Imprimir a peça com suporte para o cliente da semana passada\n", "sem suporte"),
            ("- Falar com a Ana sobre o orçamento do gabinete grande\n",
             "- Falar com a Ivo sobre o orçamento do gabinete grande\n", "Ana"),
            ("- Esperar 15 min antes de ligar para o fornecedor\n- Pagar 15 reais\n",
             "- Esperar 5 min antes de ligar para o fornecedor\n- Pagar 15 reais\n", "15 min"),
            ("- Pagar 20 reais ao Pedro\n- Pagar 30 reais ao Pedro\n",
             "- Pagar 30 reais ao Pedro\n- Pagar 30 reais ao Pedro\n", "20 reais"),
            ("- Ver [[Proj|gabinete]] agora\n", "- Ver [[Proj]] agora\n", "[[Proj|gabinete]]"),
            ("- [x] Enviar orçamento\n", "- [ ] Enviar orçamento\n", "estado"),
            ("- [ ] Enviar orçamento\n", "- [x] Enviar orçamento\n", "estado"),
            ("- [x] Enviar orçamento\n", "- Enviar orçamento\n", "estado"),
            ("- [ ] Enviar orçamento #to-do\n", "- [ ] Enviar orçamento\n", "estado"),
            ("- [x] A\n- [x] A\n", "- [x] A\n- [ ] A\n", "estado")]:
        _falha(antes, depois, trecho)
    check("- [x] Enviar Orçamento.\n- Ligar pro Marcos\n", "* [X] enviar orcamento #to-do\n- [ ] Ligar pro Marcos #to-do\n")
    check("- [ ] A\n- [x] A\n", "- [x] A\n- [ ] A\n")                # same states, other order
    # '#done' (the user's real notes) is the same state as [x]; a done task never comes back open
    check("- #done Comentar nos cards\n", "- [x] Comentar nos cards #to-do\n")
    check("- [x] Comentar nos cards #to-do\n", "- #done Comentar nos cards\n")
    _falha("- #done Comentar nos cards\n", "- [ ] Comentar nos cards #to-do\n", "estado")
    _falha("- #done Comentar nos cards\n", "- Comentar nos cards\n", "estado")
    # wordless lines ('- ' of the template, '#', a '---' rule) never make the check refuse, on either side
    check("## Para hoje\n- \n#\n---\n- #done\n- [ ] A\n", "## Para hoje\n- [ ] A #to-do\n")
    check("- A\n", "- A\n-\n---\n")
    _falha("- \n- Nunca desligar\n", "- \n- Desligar\n", "Nunca")       # ...but lines with words stay strict
    # dictated prose (see PROSA_*): rewritten into items = ok; a dropped idea, a changed number or a flipped "não" = refused
    dit = ("## Para hoje\nhoje eu testei o fedora agente e ele travou duas vezes e preciso avaliar o uso do windows "
           "para as ferramentas e não quero manter o dual boot em 2 máquinas\n")
    ok = ("## Para hoje\n- [ ] Testar o fedora agente (travou duas vezes) #to-do\n## Pendências\n"
          "- [ ] Avaliar o uso do windows para as ferramentas #to-do\n- [ ] Não manter o dual boot em 2 máquinas #to-do\n"
          "## Resumo do dia\n")
    check(dit, ok)
    _falha(dit, ok.replace("- [ ] Avaliar o uso do windows para as ferramentas #to-do\n", ""), "ideia")
    _falha(dit, ok.replace("Não manter", "Manter"), "negações")
    _falha(dit, ok.replace("em 2 máquinas", "em 3 máquinas"), "números")
    _falha(dit, ok + "- [ ] Comprar um monitor novo para o escritório #to-do\n- [ ] Contratar um designer freelancer #to-do\n"
           "- [ ] Reformar a sala de reuniões do prédio #to-do\n- [ ] Cancelar a assinatura do streaming #to-do\n", "inventou")
    _falha("Parágrafo ditado: conferir [[Projeto Gabinete]] antes das 14:30 com o cliente\n",
           "- [ ] Conferir Projeto Gabinete antes das 14:30 com o cliente #to-do\n", "[[projeto gabinete]]")
    # _montar gives back the note's own lines as written: empty "- ", '---', a loose paragraph, a tab continuation
    corpo = "## Para hoje\n- \n---\nParágrafo solto sem marcador\n- Item\n\tcontinuação com tab\n"
    out = _montar(corpo, {"Para hoje": ["- ", "---", "Parágrafo solto sem marcador", "- Item", "continuação com tab",
                                        "Fazer algo novo"]})
    assert out == ("## Para hoje\n-\n---\nParágrafo solto sem marcador\n- Item\n\tcontinuação com tab\n"
                   "- Fazer algo novo\n"), repr(out)

    # the tidy regroups by section (order changes) and adds #to-do: still the same content
    secoes = {"Para hoje": ["- [ ] Ligar pro cliente #to-do", "| Peça | Gramas |", "| --- | --- |", "| Suporte | 42 |"],
              "Pendências": ["1. Separar o PLA preto", "```\nG28 ; home\n```",
                             "Parágrafo solto: conferir [[Projeto Gabinete]] e https://exemplo.com/peca?id=7 antes das 14:30"],
              "Resumo do dia": ["- [x] Pagar 15 reais do frete #to-do", "- Troquei o bico.", "- Troquei o bico."]}
    body = n.split("---\n", 2)[2].strip() + "\n"
    out = _montar(body, secoes)
    check(body, out)
    assert "\n| Peça | Gramas |\n" in out and "\n1. Separar" in out and "\n```\nG28" in out, out   # not turned into "- |"
    assert out.index("Separar") > out.index("## Pendências") and "#Termometro-" in out.split("## ")[0]
    check("- [ ] A\n- [x] B\n", "- [ ] A #to-do\n- [x] B #to-do\n")  # tasks kept, tag added

    # tidy_note end to end with a fake model: good answer applied, bad answers refused with nothing changed
    real = llm.json_call
    try:
        llm.json_call = lambda *a, **k: secoes
        t = tidy_note(n)
        assert t.startswith("---\ndata: 2026-11-04\n---\n") and "| Suporte | 42 |" in t, t
        t = tidy_note("\ufeff" + n)                                  # BOM: stays first, frontmatter right after it
        assert t.startswith("\ufeff---\ndata: 2026-11-04\n---\n\n"), repr(t[:40])
        t = tidy_note(n.replace("\n", "\r\n"))                       # CRLF frontmatter still recognized
        assert t.startswith("---\r\ndata: 2026-11-04\r\n---\r\n") and t.count("data:") == 1, repr(t[:40])
        llm.json_call = lambda *a, **k: {"Para hoje": ["- [ ] A #to-do"]}
        assert tidy_note("\ufeff## Para hoje\n- [ ] A\n") == "\ufeff## Para hoje\n- [ ] A #to-do\n"
        llm.json_call = lambda *a, **k: secoes
        llm.json_call = lambda *a, **k: dict(secoes, **{"Resumo do dia": ["- Troquei o bico."]})
        try:
            tidy_note(n)
            raise AssertionError("dropped item applied")
        except RuntimeError as e:
            assert "Pagar 15 reais" in str(e) and "Nada foi mudado" in str(e), e
        llm.json_call = lambda *a, **k: ["nao", "e", "dict"]
        try:
            tidy_note(n)
            raise AssertionError("non-dict applied")
        except RuntimeError as e:
            assert "formato" in str(e)
        # a dictated paragraph REWRITTEN into several items in new sections is accepted (the point of "Organizar")...
        dit = "## Pendências\nhoje imprimi o suporte da câmera e amanhã preciso comprar filamento\n"
        llm.json_call = lambda *a, **k: {"Pendências": ["- [ ] Comprar filamento (amanhã) #to-do"],
                                         "Resumo do dia": ["- Imprimi o suporte da câmera."]}
        assert "Comprar filamento" in tidy_note(dit)
        # ...but not when the model drops an idea, changes a number or flips a negation
        for ruim in ({"Resumo do dia": ["- Imprimi o suporte da câmera."]},
                     {"Pendências": ["- [ ] Comprar tinta (amanhã) #to-do"], "Resumo do dia": ["- Imprimi o suporte da câmera."]}):
            llm.json_call = lambda *a, ruim=ruim, **k: ruim
            try:
                tidy_note(dit)
                raise AssertionError("dropped idea passed")
            except RuntimeError as e:
                assert "Nada foi mudado" in str(e), e
    finally:
        llm.json_call = real
    print("tidy ok (offline)")


if __name__ == "__main__":
    import sys
    if "--gemini" in sys.argv:   # one REAL Gemini call (network, quota): only when asked explicitly
        out = tidy_note(NOTA.replace("## Pendências\n", "## Pendências\nhoje eu imprimi o suporte da câmera e amanhã "
                                     "preciso comprar filamento pla preto e semana que vem ver o orçamento do gabinete\n"))
        print(out)
        assert out.startswith("---\ndata: 2026-11-04\n---") and "#Termometro-" in out, out
        print("tidy ok (gemini)")
    else:   # default and --teste: offline
        _teste()
