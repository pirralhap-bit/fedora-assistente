"""
Weekly retrospective in the vault inbox, written by the Fedora from turnos.jsonl (turnolog.py).

One note per ISO week:  <vault>/00-INB*/_a-processar/Fedora - Retrospectiva AAAA-Wss.md
One block per COMPLETE day (before today) that has turns: numbers computed here + a short reading by Haiku
(only when the day has >= MIN_TURNOS_HAIKU turns). Only ever appends at the end; never rewrites a block.

    python retro.py --agora          run now (real log, real vault, real Haiku)
    python retro.py --teste          offline (fake Haiku, temporary folders)
    python retro.py --haiku-real D   ONE real Haiku call over synthetic data in folder D (quota!)
"""
import collections
import datetime
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import time

import memory
import runtime
import turnolog

NOME = "Fedora - Retrospectiva {}.md"              # {} = "2026-W45"
MARCA_FIM = "<!-- fedora-retro:{} -->"             # day done (numbers + reading, or reading not needed / given up)
MARCA_NUM = "<!-- fedora-retro-numeros:{} -->"     # numbers block written; the reading may still be pending
MIN_TURNOS_HAIKU = 5          # fewer turns than this: numbers only, no Haiku call
MAX_TENTATIVAS = 3            # Haiku failures per day before giving up on its reading
DIAS_ATRAS = 14               # older days are not (re)written: the user may have processed that note already
TIMEOUT_HAIKU = 120
MAX_PEDIDOS = 80              # request lines sent to Haiku (flagged ones first)
MAX_RESUMO = 6000             # characters of the request list in the prompt
MAX_DIAS_POR_EXECUCAO = 2     # Haiku calls per run; the other days wait for the next run (6 h later)
PAUSA_HAIKU = 5.0             # seconds between two Haiku calls of the same run
MAX_ANALISE = 2500            # characters of Haiku's reading kept in the note
LOCK_VELHO = 15 * 60          # a retro.lock older than this is from a run that died: removed
# a plan/rate limit is not the day's fault: no attempt is counted and the whole run stops (next run tries again)
_COTA = re.compile(r"(?i)limit|\b429\b|usage limit|resets?\b|rate.?limit|overloaded")
TITULOS = ["O que funcionou bem", "O que teve mais incidência", "Onde a Fedora errou ou demorou", "Jeito de o usuário conversar"]
_DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
_LOCK = threading.Lock()
_GANCHO_TESTE = None          # the self-test changes the note between "read" and "replace" through this

PROMPT = """Você analisa um dia de uso da Fedora, a assistente de voz local do Pedro (Windows, português).
Os pedidos chegam por voz (transcritos pelo Whisper, que erra nomes próprios e palavras em inglês) ou digitados no chat.
Rotas: atalho = resposta rápida pelo código; claude = Claude Code; filtro_local→claude = o filtro local achou que não era
atalho e mandou direto ao Claude; comando = /comando; erro = o turno falhou.
Abaixo estão os números do dia (já calculados: não recalcule) e a lista de pedidos (truncados em 60 caracteres, exceto os
que têm sinais entre «»: repeticao, erro, cancelado, escalar, resposta_vazia, atalho_barrado_e_repetido).
Responda em português, NO MÁXIMO 200 palavras, em markdown, exatamente com estes 4 subtítulos e nada antes ou depois:
#### O que funcionou bem
#### O que teve mais incidência
#### Onde a Fedora errou ou demorou
#### Jeito de o usuário conversar
No último: tom, frases típicas, vocabulário e nomes que o Whisper parece ter errado. Seja concreto e cite pedidos.
Não invente nada que não esteja nos dados; se faltar dado para um subtítulo, escreva "sem dado".

"""


def _log(msg):
    print(f"[retro] {msg}", flush=True)


def _curto(s, n=80):
    s = " ".join(str(s or "").split())
    return s[:n] + ("…" if len(s) > n else "")


def inbox(vault):
    """<vault>/00-INB*/_a-processar, or None (then nothing is created)."""
    try:
        for d in sorted(pathlib.Path(vault).glob("00-INB*")):
            if (d / "_a-processar").is_dir():
                return d / "_a-processar"
    except OSError:
        pass
    return None


# ── numbers (code, not the model) ──
def _seg(ms):
    return f"{ms / 1000:.1f}".replace(".", ",") + " s"


def _p(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def _mil(n):
    return f"{n / 1000:.1f}".replace(".", ",") + " mil" if n >= 1000 else str(int(n))


def numeros(turnos):
    lat = collections.defaultdict(list)
    for t in turnos:
        if isinstance(t.get("ms"), (int, float)):
            lat[t.get("rota") or "?"].append(t["ms"])
    tokens = [t["tokens_total"] for t in turnos if isinstance(t.get("tokens_total"), (int, float)) and t["tokens_total"] > 0]
    sinais = lambda s: sum(s in (t.get("sinais") or []) for t in turnos)   # noqa: E731
    return dict(
        total=len(turnos),
        canais=collections.Counter(t.get("canal") or "?" for t in turnos),
        rotas=collections.Counter(t.get("rota") or "?" for t in turnos),
        modos=collections.Counter(t["modo"] for t in turnos if t.get("modo")),
        latencia={r: (sum(v) / len(v), _p(v, .5), _p(v, .9)) for r, v in lat.items()},
        tokens=sum(tokens), tokens_media=(sum(tokens) / len(tokens) if tokens else 0), turnos_com_tokens=len(tokens),
        erros=[_curto(t["erro"]) for t in turnos if t.get("erro")],
        cancelados=sum(bool(t.get("cancelado")) for t in turnos),
        repeticoes=sinais("repeticao"), barrados_repetidos=sinais("atalho_barrado_e_repetido"),
        escalar=sinais("escalar"), vazias=sinais("resposta_vazia"),
        atalhos=collections.Counter(t["atalho"] for t in turnos if t.get("atalho")),
        agentes=collections.Counter(a for t in turnos for a in (t.get("agentes") or [])),
        disjuntor=sum(bool(t.get("disjuntor_abriu")) for t in turnos),
        filtro=sum(bool(t.get("filtrado")) or t.get("rota") == "filtro_local→claude" for t in turnos),
    )


def _cont(c):
    return " · ".join(f"{k} {v}" for k, v in c.most_common()) or "—"


def bloco_numeros(dia, n):
    d = datetime.date.fromisoformat(dia)
    lat = "; ".join(f"{r} {_seg(m)} / {_seg(p5)} / {_seg(p9)}" for r, (m, p5, p9) in sorted(n["latencia"].items())) or "—"
    erros = "".join(f"\n  - {e}" for e in n["erros"][:5]) + (f"\n  - … e mais {len(n['erros']) - 5}" if len(n["erros"]) > 5 else "")
    return "\n".join([
        f"## {dia} ({_DIAS[d.weekday()]})",
        MARCA_NUM.format(dia),
        f"- Turnos: {n['total']} ({_cont(n['canais'])})",
        f"- Rotas: {_cont(n['rotas'])}",
        f"- Modos: {_cont(n['modos'])}",
        f"- Latência média / p50 / p90: {lat}",
        f"- Tokens: {_mil(n['tokens'])} no total, {_mil(n['tokens_media'])} por turno com Claude ({n['turnos_com_tokens']})",
        f"- Erros: {len(n['erros'])}{erros}",
        f"- Cancelamentos: {n['cancelados']} · repetições: {n['repeticoes']} (filtro barrou e repetiu: {n['barrados_repetidos']})"
        f" · ESCALAR: {n['escalar']} · respostas vazias: {n['vazias']}",
        f"- Atalhos mais usados: {_cont(n['atalhos'])}",
        f"- Agentes acionados: {_cont(n['agentes'])}",
        f"- Gemini: disjuntor desligou {n['disjuntor']} vez(es) · filtro local barrou {n['filtro']} frase(s)",
    ])


def resumo(dia, n, turnos):
    """Compact day summary for Haiku: the numbers + the requests (flagged ones whole, the rest as logged)."""
    linhas = []
    for t in turnos:
        s = t.get("sinais") or []
        rota = (t.get("rota") or "?") + (f"/{t['atalho']}" if t.get("atalho") else "")
        ln = f"- {str(t.get('ts', ''))[11:16]} {t.get('canal', '?')} [{rota}] {_curto(t.get('pedido'), 400 if s else 70)}"
        if s:
            ln += f" «{', '.join(s)}»"
        if t.get("erro"):
            ln += f" (erro: {_curto(t['erro'], 80)})"
        if isinstance(t.get("ms"), (int, float)) and t["ms"] >= 15000:
            ln += f" ({_seg(t['ms'])})"
        linhas.append((not s, len(linhas), ln))
    escolhidas = sorted(sorted(linhas)[:MAX_PEDIDOS], key=lambda x: x[1])   # flagged first, then back in time order
    corpo, total = [], 0
    for _, _, ln in escolhidas:
        if total + len(ln) > MAX_RESUMO:
            corpo.append(f"- … (+{len(escolhidas) - len(corpo)} pedidos omitidos)")
            break
        corpo.append(ln)
        total += len(ln) + 1
    numeros_txt = bloco_numeros(dia, n).split("\n", 2)[2]                  # without heading and marker
    return f"Dia {dia}\n\nNúmeros:\n{numeros_txt}\n\nPedidos:\n" + "\n".join(corpo)


def prompt_do_dia(dia, n, turnos):
    return PROMPT + resumo(dia, n, turnos)


# ── Haiku ──
class CotaError(RuntimeError):
    """The plan/rate limit answered: not counted as an attempt; the run stops."""


def e_cota(e):
    return isinstance(e, CotaError) or bool(_COTA.search(str(e)))


def comando_haiku(prompt):
    """argv of the Haiku call: no tools, no MCP servers, no slash commands/skills, never asks for permission."""
    return runtime._claude_base() + [
        "-p", prompt, "--model", "haiku", "--effort", "low", "--output-format", "json",
        "--tools", "", "--disable-slash-commands", "--strict-mcp-config", "--permission-mode", "dontAsk",
        "--settings", json.dumps({"enabledPlugins": {"ponytail@ponytail": False}})]


def haiku(prompt, timeout=TIMEOUT_HAIKU):
    """(text, usage) of one `claude -p` with Haiku, run in an empty temporary folder (no project CLAUDE.md).
    RuntimeError(short reason) on any failure; on timeout the whole process tree is killed."""
    cmd = comando_haiku(prompt)
    cwd = tempfile.mkdtemp(prefix="fedora-retro-")
    try:
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONUTF8="1"),
                                    creationflags=0x08000000 if os.name == "nt" else 0)   # CREATE_NO_WINDOW
        except OSError as e:
            raise RuntimeError(f"claude não iniciou ({type(e).__name__})") from e
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            runtime.kill_tree(proc)
            try:
                proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"tempo esgotado ({timeout} s)") from None
        try:
            d = json.loads(out)
        except ValueError:
            motivo = _curto(err or out, 120) or f"saída inválida (código {proc.returncode})"
            raise (CotaError if _COTA.search(motivo) else RuntimeError)(motivo) from None
        if not isinstance(d, dict) or d.get("is_error") or proc.returncode:
            motivo = _curto((d.get("result") if isinstance(d, dict) else None) or err or f"código {proc.returncode}", 120)
            raise (CotaError if _COTA.search(motivo) else RuntimeError)(motivo)
        return str(d.get("result") or "").strip(), d.get("usage") or {}
    finally:
        shutil.rmtree(cwd, ignore_errors=True)


def _chave(s):
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFD", s.casefold()) if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


_TITULO = {_chave(t): t for t in TITULOS}


def _linha_segura(ln):
    """One line of plain text/list: nothing Obsidian would turn into a link, embed, tag, task, heading or markup."""
    ln = ln.replace("<", "&lt;")                                          # HTML, comments, fake markers
    ln = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", ln)                          # remote images
    ln = re.sub(r"!\[\[[^\]]*\]\]", "", ln)                               # embeds
    ln = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", ln)                      # [text](url) -> text
    ln = ln.replace("[[", "").replace("]]", "")                           # wikilinks -> plain text
    ln = re.sub(r"(?i)\b(?:https?|ftp)://", "", ln)                       # bare URLs no longer clickable
    ln = re.sub(r"^\s*#+\s*", "", ln)                                     # any heading -> text
    ln = re.sub(r"^(\s*(?:[-*+]|\d+[.)])\s+)\[[ xX]?\]", r"\1( )", ln)    # "- [ ] x" is not a task
    ln = re.sub(r"(?<![\\\w])#(?=\S)", r"\\#", ln)                       # "#tag", "#to-do" -> literal
    ln = ln.replace("`", "'")
    return ln.rstrip()


def _limpa(texto):
    """Haiku's markdown made safe to append, or ValueError (then: "análise indisponível").
    Only the 4 expected '####' subtitles, each followed by list lines or paragraphs; everything before the first one
    (frontmatter, a fake title) and every code block is dropped; at most MAX_ANALISE characters."""
    out, vistos, dentro, fence = [], set(), False, False
    for bruta in str(texto or "").replace("\r\n", "\n").split("\n"):
        s = bruta.strip()
        if s.startswith("```") or s.startswith("~~~"):
            fence = not fence
            continue
        if fence:
            continue                                                      # code is never expected here
        m = re.match(r"^#{1,6}\s*(.+?)\s*#*$", s)
        if m and _chave(m.group(1).strip("*_ :")) in _TITULO:
            t = _TITULO[_chave(m.group(1).strip("*_ :"))]
            if t in vistos:
                dentro = False
                continue
            vistos.add(t)
            dentro = True
            out.append(f"#### {t}")
            continue
        if not dentro or re.fullmatch(r"[-*_=]{3,}", s):                 # before the 1st subtitle; --- / *** alone
            continue
        ln = _linha_segura(bruta)[:600]
        if ln.strip() or (out and out[-1].strip()):
            out.append(ln)
    if len(vistos) < len(TITULOS):
        raise ValueError(f"análise fora do formato ({len(vistos)} de {len(TITULOS)} subtítulos)")
    t, total = [], 0
    for ln in out:
        if total + len(ln) + 1 > MAX_ANALISE:
            t.append("(…)")
            break
        t.append(ln)
        total += len(ln) + 1
    return "\n".join(t).strip()


# ── the note ──
def cabecalho(semana, hoje):
    return (f"---\ntags:\n  - Assistente\n  - Retrospectiva\n  - PRJ-AI-07\ncriado: {hoje.isoformat()}\nstatus: bruto\n"
            f"fedora-log: true\n---\n\n# Fedora - Retrospectiva {semana}\n\n"
            "Escrita pela Fedora (retro.py) a partir do registro de turnos: um bloco por dia completo, sempre acrescentado "
            "no fim. Pode editar à vontade; ela não reescreve nada do que já está aqui.\n")


def _ler(nota):
    try:
        return nota.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def acrescentar(nota, bloco, cab, proibidos=()):
    """Append `bloco` at the end of the note (create it with `cab` if missing), keeping its line ending and everything
    in it. Atomic (temp file + os.replace); the mtime is checked right before replacing: if the note changed in the
    meantime it is read again and redone once. `proibidos`: markers that, if already in the note as just read (another
    writer got there first), mean nothing is written. True if written."""
    tmp = nota.with_name("." + nota.name + ".fedora-tmp")    # dot file: Obsidian does not list it
    for _ in range(2):
        try:
            try:
                mt = nota.stat().st_mtime_ns
                antes = nota.read_bytes()
            except FileNotFoundError:
                mt, antes = None, None
            ja = [p for p in proibidos if antes is not None and p.encode("utf-8") in antes]
            if ja:
                _log(f"{nota.name}: {ja[0]} já está na nota (outro processo escreveu): não duplico")
                return False
            if antes is None:
                dados = (cab + "\n" + bloco + "\n").encode("utf-8")
            else:
                try:
                    s = antes.decode("utf-8")                  # a BOM stays where it is (decoded as ﻿)
                except UnicodeDecodeError:
                    _log(f"{nota.name} não está em UTF-8: não mexo nela")
                    return False
                eol = "\r\n" if "\r\n" in s else "\n"
                base = s if not s or s.endswith("\n") else s + eol
                dados = (base + eol + bloco.replace("\n", eol) + eol).encode("utf-8")
            tmp.write_bytes(dados)
            if _GANCHO_TESTE:
                _GANCHO_TESTE(nota)
            try:
                agora_mt = nota.stat().st_mtime_ns
            except FileNotFoundError:
                agora_mt = None
            if agora_mt != mt:                                 # someone (Obsidian, sync) wrote meanwhile
                tmp.unlink(missing_ok=True)
                continue
            os.replace(tmp, nota)
            return True
        except OSError as e:
            _log(f"não consegui gravar {nota.name}: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
    _log(f"{nota.name} mudou enquanto eu gravava (duas vezes): fica para a próxima execução")
    return False


def _estado(f):
    """retro.json, validated: feitos = list of "AAAA-MM-DD", tentativas = {"AAAA-MM-DD": int}. Anything else is
    dropped (a broken file must not raise every 6 h)."""
    vazio = dict(tentativas={}, feitos=[])
    try:
        d = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return vazio
    if not isinstance(d, dict):
        return vazio
    dia = re.compile(r"\d{4}-\d{2}-\d{2}$")
    feitos = d.get("feitos")
    feitos = list(feitos) if isinstance(feitos, (list, dict)) else []
    tent = d.get("tentativas") if isinstance(d.get("tentativas"), dict) else {}
    return dict(feitos=[x for x in feitos if isinstance(x, str) and dia.match(x)],
                tentativas={k: v for k, v in tent.items()
                            if isinstance(k, str) and dia.match(k) and isinstance(v, int) and not isinstance(v, bool) and v >= 0})


def _salva_estado(f, est):
    try:
        f = pathlib.Path(f)
        f.parent.mkdir(parents=True, exist_ok=True)
        est["feitos"] = est["feitos"][-60:]
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(est, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)
    except OSError as e:
        _log(f"não consegui gravar o estado ({e})")


def _trava(lock):
    """Exclusive lock between PROCESSES (server thread + `retro.py --agora`): O_EXCL file; one older than LOCK_VELHO
    is from a run that died and is removed. True if this process holds it now."""
    for _ in range(2):
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as fh:
                fh.write(f"{os.getpid()} {time.time():.0f}")
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime <= LOCK_VELHO:
                    return False
                lock.unlink()
                _log(f"lock velho removido ({lock})")
            except FileNotFoundError:
                pass
            except OSError:
                return False
        except OSError as e:
            _log(f"não consegui criar o lock ({e})")
            return False
    return False


def rodar(agora=None, runner=None, vault=None, log=None, estado=None):
    """Write the blocks still missing. Returns {"escritos": [days], "pendentes": [days], ...}; never raises."""
    if not _LOCK.acquire(blocking=False):
        _log("já está rodando")
        return {"ocupado": True}
    lock = None
    try:
        log = pathlib.Path(log or turnolog.FILE)
        estado = pathlib.Path(estado or log.parent / "retro.json")
        lock = estado.with_name("retro.lock")
        if not _trava(lock):
            lock = None
            _log("outro processo está escrevendo a retrospectiva: fica para a próxima")
            return {"ocupado": True}
        return _rodar(agora, runner or haiku, vault, log, estado)
    except Exception as e:  # noqa: BLE001
        _log(f"falhou: {type(e).__name__}: {str(e)[:200]}")
        return {"erro": str(e)}
    finally:
        if lock is not None:
            try:
                lock.unlink()
            except OSError:
                pass
        _LOCK.release()


def _rodar(agora, runner, vault, log, estado):
    agora = agora or datetime.datetime.now()
    hoje = agora.date()
    vault = pathlib.Path(vault or memory.VAULT)
    dias = collections.defaultdict(list)
    for t in turnolog.ler(log):
        try:
            d = datetime.date.fromisoformat(str(t["ts"])[:10])
        except ValueError:
            continue
        if hoje - datetime.timedelta(days=DIAS_ATRAS) <= d < hoje:    # complete days only; today is still going on
            dias[d.isoformat()].append(t)
    out = dict(escritos=[], pendentes=[], chamadas=0)
    if not dias:
        return out
    caixa = inbox(vault)
    if caixa is None:
        _log(f"não achei 00-INB*/_a-processar em {vault}: nada foi escrito")
        return dict(out, sem_caixa=True)
    est = _estado(estado)
    for dia in sorted(dias):
        iy, iw, _ = datetime.date.fromisoformat(dia).isocalendar()
        semana = f"{iy}-W{iw:02d}"
        nota = caixa / NOME.format(semana)
        texto = _ler(nota)
        if MARCA_FIM.format(dia) in texto or dia in est["feitos"]:
            continue
        turnos = dias[dia]
        n = numeros(turnos)
        tem_num = MARCA_NUM.format(dia) in texto
        partes = [] if tem_num else [bloco_numeros(dia, n)]
        concluido = True
        if n["total"] >= MIN_TURNOS_HAIKU:
            if out["chamadas"] >= MAX_DIAS_POR_EXECUCAO:      # the rest waits for the next run (quota)
                out["adiados"] = [d for d in sorted(dias) if d >= dia and d not in est["feitos"]]
                break
            if out["chamadas"]:
                time.sleep(PAUSA_HAIKU)
            titulo = "### Leitura do dia (Haiku)" if not tem_num else f"### Leitura de {dia} (Haiku, nova tentativa)"
            try:
                out["chamadas"] += 1
                analise = _limpa(runner(prompt_do_dia(dia, n, turnos))[0])
                if not analise:
                    raise RuntimeError("resposta vazia")
                partes.append(f"{titulo}\n{analise}")
                est["tentativas"].pop(dia, None)
            except Exception as e:  # noqa: BLE001 — quota, timeout, no claude: numbers only, try again next time
                motivo = _curto(str(e) or type(e).__name__, 100)
                if e_cota(e):                                  # not the day's fault: no attempt counted, run stops
                    out["cota"] = True
                    concluido = False
                    _log(f"{dia}: limite de uso do Claude ({motivo}): paro esta execução, sem contar tentativa")
                    if not tem_num:
                        partes.append(f"(análise indisponível: limite de uso do Claude; tento de novo depois)")
                    if partes and acrescentar(nota, "\n\n".join(partes), cabecalho(semana, hoje),
                                              (MARCA_NUM.format(dia), MARCA_FIM.format(dia))):
                        out["escritos"].append(dia)
                    out["pendentes"].append(dia)
                    break
                tent = est["tentativas"][dia] = est["tentativas"].get(dia, 0) + 1
                _log(f"{dia}: análise indisponível ({motivo}), tentativa {tent} de {MAX_TENTATIVAS}")
                if tent < MAX_TENTATIVAS:
                    concluido = False
                    if not tem_num:
                        partes.append(f"(análise indisponível: {motivo})")
                else:
                    partes.append(f"(análise indisponível: {motivo}; desisti depois de {MAX_TENTATIVAS} tentativas)")
        elif not tem_num:
            partes.append(f"(menos de {MIN_TURNOS_HAIKU} turnos: só os números)")
        if concluido:
            partes.append(MARCA_FIM.format(dia))
        # re-checked on the note as read right before writing: another writer may have added it meanwhile
        proibidos = (MARCA_FIM.format(dia),) if tem_num else (MARCA_NUM.format(dia), MARCA_FIM.format(dia))
        if partes and acrescentar(nota, "\n\n".join(partes), cabecalho(semana, hoje), proibidos):
            out["escritos"].append(dia)
            if concluido:
                est["feitos"].append(dia)
            _log(f"{dia}: bloco acrescentado em {nota.name}" + ("" if concluido else " (análise pendente)"))
        if not concluido:
            out["pendentes"].append(dia)
        _salva_estado(estado, est)
    return out


# ── tests ──
def _sinteticos(dia, n, tag=""):
    rotas = ["atalho", "claude", "filtro_local→claude", "claude", "erro", "atalho", "claude", "comando"]
    out = []
    for i in range(n):
        r = rotas[i % len(rotas)]
        t = dict(ts=f"{dia}T{9 + i // 6:02d}:{(i * 7) % 60:02d}:00-03:00", canal="voz" if i % 4 else "chat",
                 pedido=f"pedido {tag}{i} abre o orca slicer" if r == "atalho" else f"pedido {tag}{i} me explica o extrato",
                 rota=r, atalho="abrir" if r == "atalho" else None, modo=None if r in ("atalho", "comando") else "leve",
                 sessao_nova=i == 1, agentes=["junior"] if i == 3 else [], n_tools=i % 3, ms=800 + i * 1500,
                 ms_primeiro_texto=500, tokens_total=12000 + i * 100 if r != "atalho" else None, tokens_out=40,
                 erro="Claude went quiet for 180s" if r == "erro" else None, cancelado=i == 6,
                 gemini="nao_usado" if r == "filtro_local→claude" else "ok",
                 sinais=(["erro"] if r == "erro" else []) + (["cancelado"] if i == 6 else []))
        if i == 1:
            t["disjuntor_abriu"] = True
        out.append(t)
    return out


def _grava_log(f, turnos):
    with open(f, "a", encoding="utf-8") as fh:
        for t in turnos:
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")


def _teste():
    import sys
    global _GANCHO_TESTE, PAUSA_HAIKU
    PAUSA_HAIKU = 0.05
    sys.stdout.reconfigure(errors="replace")
    raiz = pathlib.Path(tempfile.mkdtemp(prefix="retro-teste-"))
    vault = raiz / "vault"
    caixa = vault / "00-INB 📥 Inbox" / "_a-processar"
    caixa.mkdir(parents=True)
    log, est = raiz / "estado" / "turnos.jsonl", raiz / "estado" / "retro.json"
    log.parent.mkdir()
    chamadas = []

    def falso(prompt):
        chamadas.append(prompt)
        return ("#### O que funcionou bem\n- atalhos rápidos\n#### O que teve mais incidência\n- erro <!-- fedora-retro:x -->\n"
                "#### Onde a Fedora errou ou demorou\n- 180 s\n#### Jeito de o usuário conversar\n- curto", {})

    def falha(prompt):
        chamadas.append(prompt)
        raise RuntimeError("rede caiu (5h)")

    # two complete days (Tue 6 turns, Wed 3 turns) + today (Thu): note created with frontmatter and 2 blocks
    _grava_log(log, _sinteticos("2026-11-03", 6, "a") + _sinteticos("2026-11-04", 3, "b") + _sinteticos("2026-11-05", 7, "c"))
    quinta = datetime.datetime(2026, 11, 5, 12, 0)
    r = rodar(quinta, falso, vault, log, est)
    nota = caixa / "Fedora - Retrospectiva 2026-W45.md"
    t = nota.read_text(encoding="utf-8")
    assert r["escritos"] == ["2026-11-03", "2026-11-04"] and len(chamadas) == 1, (r, len(chamadas))   # 3 turns: no Haiku
    assert t.startswith("---\ntags:\n  - Assistente\n  - Retrospectiva\n  - PRJ-AI-07\ncriado: 2026-11-05\nstatus: bruto\n"
                        "fedora-log: true\n---\n"), t[:200]
    assert t.count(MARCA_FIM.format("2026-11-03")) == 1 and MARCA_FIM.format("2026-11-04") in t and "## 2026-11-05" not in t
    assert "#### Jeito de o usuário conversar" in t and "menos de 5 turnos" in t and "&lt;!-- fedora-retro:x" in t
    assert "- Turnos: 6 (voz 4 · chat 2)" in t and "disjuntor desligou 1 vez(es) · filtro local barrou 1 frase(s)" in t, t
    assert "pedido a2 me explica o extrato" in chamadas[0] and "«erro»" in chamadas[0]
    print(f"  prompt do Haiku: {len(chamadas[0])} caracteres para um dia de 6 turnos")
    print("\n".join("    " + x for x in bloco_numeros("2026-11-03", numeros(_sinteticos("2026-11-03", 6, "a"))).splitlines()))

    # second run: nothing duplicated, no call
    antes = nota.read_bytes()
    r = rodar(quinta, falso, vault, log, est)
    assert nota.read_bytes() == antes and r["escritos"] == [] and len(chamadas) == 1

    # the user edits the note in the middle (and Obsidian saved it with CRLF): kept, new day appended at the end
    t = nota.read_text(encoding="utf-8").replace("\n## 2026-11-04", "\nMinha anotação no meio.\n\n## 2026-11-04")
    nota.write_bytes(t.replace("\n", "\r\n").encode("utf-8"))
    sexta = datetime.datetime(2026, 11, 6, 8, 0)
    est.unlink()                                  # the markers in the note alone keep it idempotent
    r = rodar(sexta, falso, vault, log, est)
    b = nota.read_bytes()
    assert r["escritos"] == ["2026-11-05"] and b"Minha anota\xc3\xa7\xc3\xa3o no meio.\r\n" in b and b.count(b"\n") == b.count(b"\r\n")
    assert b.index(b"Minha anota") < b.index(b"## 2026-11-05") and b.startswith(antes.replace(b"\n", b"\r\n")[:60])
    assert b.count(b"fedora-retro:2026-11-03 -->") == 1

    # Haiku fails: numbers only + reason, day NOT done; retried next run (nothing appended while it fails), then ok
    _grava_log(log, _sinteticos("2026-11-06", 5, "d"))
    sabado = datetime.datetime(2026, 11, 7, 9, 0)
    r = rodar(sabado, falha, vault, log, est)
    t = nota.read_text(encoding="utf-8")
    assert r["pendentes"] == ["2026-11-06"] and "(análise indisponível: rede caiu (5h))" in t
    assert MARCA_NUM.format("2026-11-06") in t and MARCA_FIM.format("2026-11-06") not in t
    antes = nota.read_bytes()
    r = rodar(sabado, falha, vault, log, est)
    assert nota.read_bytes() == antes and r["pendentes"] == ["2026-11-06"]
    assert _estado(est)["tentativas"]["2026-11-06"] == 2
    r = rodar(sabado, falso, vault, log, est)
    t = nota.read_text(encoding="utf-8")
    assert r["escritos"] == ["2026-11-06"] and "### Leitura de 2026-11-06 (Haiku, nova tentativa)" in t
    assert t.count("## 2026-11-06 (sexta-feira)") == 1 and MARCA_FIM.format("2026-11-06") in t
    # ... and after MAX_TENTATIVAS failures it gives up (marked done, with the reason)
    _grava_log(log, _sinteticos("2026-11-07", 5, "e"))
    domingo = datetime.datetime(2026, 11, 8, 9, 0)
    for _ in range(MAX_TENTATIVAS):
        r = rodar(domingo, falha, vault, log, est)
    t = nota.read_text(encoding="utf-8")
    assert "desisti depois de 3 tentativas" in t and MARCA_FIM.format("2026-11-07") in t
    n_ch = len(chamadas)
    rodar(domingo, falha, vault, log, est)
    assert len(chamadas) == n_ch                                   # done: no 4th call

    # the note changes between read and replace (Obsidian saving): read again, redo, user text kept
    _grava_log(log, _sinteticos("2026-11-08", 2, "f"))
    segunda = datetime.datetime(2026, 11, 9, 9, 0)                 # 2026-11-08 is Sunday, still W45

    def obsidian(n, feito=[]):                                     # noqa: B006 — fires once
        if not feito:
            feito.append(1)
            time.sleep(0.02)
            with open(n, "ab") as fh:
                fh.write("Escrevi enquanto a Fedora gravava.\r\n".encode("utf-8"))
            os.utime(n, ns=(time.time_ns(), time.time_ns() + 10**9))
    _GANCHO_TESTE = obsidian
    try:
        r = rodar(segunda, falso, vault, log, est)
    finally:
        _GANCHO_TESTE = None
    t = nota.read_text(encoding="utf-8")
    assert r["escritos"] == ["2026-11-08"] and t.index("Escrevi enquanto") < t.index("## 2026-11-08"), r
    assert not list(caixa.glob(".*fedora-tmp"))

    # no 00-INB*/_a-processar: nothing created anywhere
    v2 = raiz / "vault-sem-inbox"
    v2.mkdir()
    (v2 / "00-INB Inbox").mkdir()                                  # inbox without _a-processar
    r = rodar(sabado, falso, v2, log, raiz / "e2.json")
    assert r.get("sem_caixa") and [p.name for p in v2.rglob("*")] == ["00-INB Inbox"] and not (raiz / "e2.json").exists()

    # the Fedora does not read its own retrospective back (memory.py skips fedora-log notes)
    memory.VAULT = vault
    g = memory.build_graph(force=True)
    assert g["total"] == 0 and not memory.search("Retrospectiva") and not memory.context_for("retrospectiva turnos atalhos")
    _teste_lote_e_qa(raiz, falso)
    print(f"retro ok (offline, {raiz})")


HOSTIL = ("---\ntags: x\n---\n# TITULO FALSO\n#### O que funcionou bem\n"
          "- ok [[Nota Importante]] #tag-poluente [click](https://evil.example/x?a=1) ![img](http://evil/p.png) ![[anexo.png]]\n"
          "---\ntags: y\n---\n## Título falso no meio\n#### **O que teve mais incidência:**\n```\n" + "A" * 3000 + "\n```\n"
          "- [ ] x #to-do\n<!-- fedora-retro:2026-11-19 -->\n#### Onde a Fedora errou ou demorou\nparágrafo https://evil.example\n"
          "#### Jeito de o usuário conversar\n- curto `codigo`\n```python\nnunca fechada " + "B" * 3000)
INJECAO_QA = ("---\ntags: x\n---\n# TITULO FALSO\n<!-- fedora-retro:2026-11-19 -->\n<!--x\n[[Nota Importante]] #tag-poluente "
              "[click](https://evil.example/x?a=1) ![img](http://evil/p.png)\n```\n" + "A" * 5000 + "\n```\nTask #to-do apagar tudo\n")


def _teste_lote_e_qa(raiz, falso):
    """QA, lote E: sanitizing, per-run quota, rate limit, broken state, stale lock, two processes at once."""
    import subprocess
    import sys

    # (2) a hostile reading is defanged; one without the 4 subtitles is refused
    r = _limpa(HOSTIL)
    for proibido in ("[[", "](", "![", "http", "```", "<!--", "\n---", "- [ ]", "TITULO FALSO", "A" * 100, "B" * 100, "tags: x", "`"):
        assert proibido not in r, (proibido, r[:600])
    assert "\\#to-do" in r and "\\#tag-poluente" in r and "Nota Importante" in r and "&lt;" in r and len(r) <= MAX_ANALISE + 5
    assert [ln for ln in r.splitlines() if ln.startswith("#")] == [f"#### {t}" for t in TITULOS], r
    assert "\nTítulo falso no meio" in r                                 # demoted to plain text
    for ruim in (INJECAO_QA, "#### O que funcionou bem\n- só um subtítulo", ""):
        try:
            _limpa(ruim)
            raise AssertionError("aceitou análise fora do formato")
        except ValueError:
            pass

    def caso(nome):
        r = raiz / nome
        cx = r / "vault" / "00-INB 📥 Inbox" / "_a-processar"
        cx.mkdir(parents=True)
        return r, cx
    hoje = datetime.datetime(2026, 11, 20, 9)
    dias = [(hoje.date() - datetime.timedelta(days=k)).isoformat() for k in range(14, 0, -1)]

    # (2) the QA injection as Haiku's answer: refused -> "análise indisponível", attempt counted, no marker forged
    r, cx = caso("inj")
    _grava_log(r / "turnos.jsonl", _sinteticos("2026-11-19", 6, "i"))
    out = rodar(hoje, lambda p: (INJECAO_QA, {}), r / "vault", r / "turnos.jsonl", r / "retro.json")
    t = next(cx.glob("*.md")).read_text(encoding="utf-8")
    assert out["pendentes"] == ["2026-11-19"] and "análise fora do formato" in t and "TITULO FALSO" not in t
    assert t.count("<!--") == 1 and _estado(r / "retro.json")["tentativas"] == {"2026-11-19": 1}, t

    # (3) 14 days: at most MAX_DIAS_POR_EXECUCAO calls per run, PAUSA_HAIKU between them, all done after 7 runs
    r, cx = caso("d14")
    for i, d in enumerate(dias):
        _grava_log(r / "turnos.jsonl", _sinteticos(d, 8, f"d{i}"))
    instantes = []

    def anota(p):
        instantes.append(time.monotonic())
        return falso(p)
    for execucao in range(7):
        antes = len(instantes)
        out = rodar(hoje, anota, r / "vault", r / "turnos.jsonl", r / "retro.json")
        assert out["chamadas"] == len(instantes) - antes <= MAX_DIAS_POR_EXECUCAO, out
        assert instantes[-1] - instantes[-2] >= PAUSA_HAIKU * 0.7     # Windows clock: ~15 ms resolution
    texto = "".join(f.read_text(encoding="utf-8") for f in cx.glob("*.md"))
    assert all(texto.count(MARCA_FIM.format(d)) == 1 for d in dias) and len(instantes) == 14
    assert rodar(hoje, anota, r / "vault", r / "turnos.jsonl", r / "retro.json")["chamadas"] == 0

    # (3) rate limit: no attempt counted, the run stops at once (the 2nd day is not touched), next run finishes
    r, cx = caso("cota")
    _grava_log(r / "turnos.jsonl", _sinteticos("2026-11-18", 6, "x") + _sinteticos("2026-11-19", 6, "y"))
    chamadas = []

    def cota(p):
        chamadas.append(p)
        raise RuntimeError("You've hit your usage limit · resets 5pm")
    for _ in range(4):
        out = rodar(hoje, cota, r / "vault", r / "turnos.jsonl", r / "retro.json")
        assert out.get("cota") and out["pendentes"] == ["2026-11-18"], out
    t = next(cx.glob("*.md")).read_text(encoding="utf-8")
    assert len(chamadas) == 4 and _estado(r / "retro.json")["tentativas"] == {} and "## 2026-11-19" not in t
    assert t.count("## 2026-11-18 (") == 1 and "limite de uso do Claude" in t
    assert e_cota(CotaError("x")) and e_cota(RuntimeError("HTTP 429")) and not e_cota(RuntimeError("tempo esgotado (120 s)"))
    out = rodar(hoje, falso, r / "vault", r / "turnos.jsonl", r / "retro.json")
    t = next(cx.glob("*.md")).read_text(encoding="utf-8")
    assert out["escritos"] == ["2026-11-18", "2026-11-19"] and t.count("## 2026-11-18 (") == 1 and MARCA_FIM.format("2026-11-19") in t

    # (10) a broken retro.json never raises and counts as empty
    r, cx = caso("estado")
    _grava_log(r / "turnos.jsonl", _sinteticos("2026-11-19", 6, "e"))
    for conteudo in ["", "{bad", "[]", "5", '"x"', '{"feitos":"abc","tentativas":5}', '{"feitos":[1,null],"tentativas":{"x":"y"}}',
                     '{"feitos":{"2026-11-01":1},"tentativas":{"2026-11-19":"2","2026-11-18":true,"2026-11-17":-1,"2026-11-16":2}}']:
        (r / "retro.json").write_text(conteudo, encoding="utf-8")
        e = _estado(r / "retro.json")
        assert isinstance(e["feitos"], list) and isinstance(e["tentativas"], dict), (conteudo, e)
        assert all(isinstance(v, int) for v in e["tentativas"].values()) and "erro" not in rodar(
            hoje, falso, r / "vault", r / "turnos.jsonl", r / "retro.json"), conteudo
    assert e == dict(feitos=["2026-11-01"], tentativas={"2026-11-16": 2}), e

    # (1) acrescentar re-reads the note: a marker another writer already put there blocks the duplicate
    n1 = cx / "x.md"
    n1.write_text("já tem\n" + MARCA_NUM.format("2026-11-19") + "\n", encoding="utf-8")
    antes = n1.read_bytes()
    assert not acrescentar(n1, "bloco", "cab", (MARCA_NUM.format("2026-11-19"),)) and n1.read_bytes() == antes
    assert acrescentar(n1, "bloco", "cab", (MARCA_FIM.format("2026-11-19"),)) and n1.read_text(encoding="utf-8").endswith("bloco\n")

    # (1) stale lock removed, fresh lock respected
    r, cx = caso("lock")
    _grava_log(r / "turnos.jsonl", _sinteticos("2026-11-19", 2, "l"))
    lock = r / "retro.lock"
    lock.write_text("123 0", encoding="utf-8")
    assert rodar(hoje, falso, r / "vault", r / "turnos.jsonl", r / "retro.json").get("ocupado")
    os.utime(lock, (time.time() - LOCK_VELHO - 60,) * 2)
    out = rodar(hoje, falso, r / "vault", r / "turnos.jsonl", r / "retro.json")
    assert out["escritos"] == ["2026-11-19"] and not lock.exists(), out

    # (1) two PROCESSES at once with a slow fake Haiku: one block and one call per day
    r, cx = caso("conc")
    _grava_log(r / "turnos.jsonl", _sinteticos("2026-11-19", 6, "i") + _sinteticos("2026-11-18", 6, "j"))
    codigo = ("import datetime, json, sys, time; sys.path.insert(0, sys.argv[1]); import retro\n"
              "n = []\n"
              "def lento(p):\n    n.append(1); time.sleep(1.5)\n"
              "    return ('\\n'.join('#### ' + t + '\\n- x' for t in retro.TITULOS), {})\n"
              "retro.PAUSA_HAIKU = 0.05\n"
              "out = retro.rodar(datetime.datetime(2026, 11, 20, 9), lento, *sys.argv[2:5])\n"
              "print(json.dumps(dict(out, n=len(n))))\n")
    args = [sys.executable, "-c", codigo, str(pathlib.Path(__file__).parent), str(r / "vault"), str(r / "turnos.jsonl"),
            str(r / "retro.json")]
    procs = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) for _ in range(2)]
    res = [json.loads(p.communicate(timeout=60)[0].strip().splitlines()[-1]) for p in procs]
    t = next(cx.glob("*.md")).read_text(encoding="utf-8")
    assert sum(x["n"] for x in res) == 2 and t.count("## 2026-11-19 (") == 1 and t.count("## 2026-11-18 (") == 1, (res, t)
    assert t.count(MARCA_FIM.format("2026-11-18")) == 1 and t.count(MARCA_FIM.format("2026-11-19")) == 1
    assert sorted(bool(x.get("ocupado")) for x in res) == [False, True], res

    # (5) the Haiku argv is well formed (not executed)
    cmd = comando_haiku("oi")
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk" and "--strict-mcp-config" in cmd and cmd[cmd.index("--tools") + 1] == ""
    json.loads(cmd[cmd.index("--settings") + 1])
    print(f"  lote E/QA: saneamento, cota por execução ({MAX_DIAS_POR_EXECUCAO}), limite de uso, estado quebrado, lock e "
          f"2 processos: ok ({res})")


def _haiku_real(pasta):
    """ONE real call (quota): synthetic day in `pasta`, same code path as rodar(); prints time and tokens."""
    import sys
    sys.stdout.reconfigure(errors="replace")
    pasta = pathlib.Path(pasta)
    caixa = pasta / "vault" / "00-INB 📥 Inbox" / "_a-processar"
    caixa.mkdir(parents=True, exist_ok=True)
    log = pasta / "turnos.jsonl"
    log.unlink(missing_ok=True)
    ontem = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    _grava_log(log, _sinteticos(ontem, 8, "r"))
    medida = {}

    def medindo(prompt):
        t0 = time.monotonic()
        try:
            texto, uso = haiku(prompt)
        finally:
            medida.update(seg=round(time.monotonic() - t0, 1), chars=len(prompt))
        medida["uso"] = uso
        return texto, uso
    r = rodar(None, medindo, pasta / "vault", log, pasta / "retro.json")
    u = medida.get("uso") or {}
    entrada = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    print(f"rodar: {r}")
    print(f"prompt {medida.get('chars')} caracteres · {medida.get('seg')} s · entrada {entrada} tokens "
          f"(input {u.get('input_tokens')}, cache lido {u.get('cache_read_input_tokens')}, "
          f"cache criado {u.get('cache_creation_input_tokens')}) · saída {u.get('output_tokens')}")
    for f in caixa.glob("*.md"):
        print(f"--- {f}\n{f.read_text(encoding='utf-8')}")


if __name__ == "__main__":
    import sys
    if "--agora" in sys.argv:
        print(rodar())
    elif "--haiku-real" in sys.argv:
        _haiku_real(sys.argv[sys.argv.index("--haiku-real") + 1])
    else:   # default and --teste: offline
        _teste()
