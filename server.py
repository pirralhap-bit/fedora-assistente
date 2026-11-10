"""
Fedora — local voice assistant driven by Claude Code.

    python3 server.py

The brain is your own `claude` CLI, so this inherits your subscription, your
tools, your MCP servers and your CLAUDE.md. The memory is a folder of markdown
you can open in Obsidian. Nothing is hosted; it binds to localhost only.
"""
import json
import mimetypes
import os
import pathlib
import queue
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

for _s in (sys.stdout, sys.stderr):   # Windows consoles/pipes default to cp1252: the banner has non-ASCII
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent
UI = ROOT / "ui"

_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import commands    # noqa: E402
import conversa    # noqa: E402
import listener    # noqa: E402
import memory      # noqa: E402
import motor_regra  # noqa: E402
import notes       # noqa: E402
import runtime     # noqa: E402
import speech      # noqa: E402
import sysinfo     # noqa: E402
import tidy        # noqa: E402
import turnolog    # noqa: E402

PORT = int(os.environ.get("JARVIS_PORT", "8720"))
def _token():
    """Same token across service restarts (an open window keeps working); new one each boot (tmpfs)."""
    if os.name == "nt":
        d = pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home())) / "fedora-assistente"
        d.mkdir(parents=True, exist_ok=True)
    else:
        d = pathlib.Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    f = d / "fedora-assistente.token"
    try:
        tok = f.read_text(encoding="utf-8").strip()
        if len(tok) >= 32:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    try:
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(tok)
    except OSError:
        pass   # no runtime dir: fall back to a per-process token
    return tok


API_TOKEN = _token()
RUN_LOCK = threading.Lock()
MAX_JSON = 1024 * 1024
SESSION = {"id": None, "modo": None, "ultima": 0, "turnos": 0}
CHAT = {"id": None}   # typed chat: its own long session, full Claude Code, no window
# the turn running now: which channel's session it uses, and whether Esc cancelled it (then its late "complete"
# must not write the session id back). Read and written under TURNO_LOCK.
TURNO_LOCK = threading.Lock()
TURNO = {"atual": None}
listener.EAR.busy = RUN_LOCK.locked   # the ear doesn't open the mic for a new command while a turn runs


def _turno_novo(chat):
    t = {"canal": "chat" if chat else "voz", "cancelado": False, "t0": time.monotonic(), "reg": None}
    with TURNO_LOCK:
        TURNO["atual"] = t
    runtime.novo_turno()
    return t


def _turno_fim(t):
    if t is None:
        return
    _registra(t)
    with TURNO_LOCK:
        if TURNO["atual"] is t:
            TURNO["atual"] = None


_SEM_TEXTO = "Claude completed without textual output."


def _observa(reg, ev):
    """What the turn log (turnolog.py) needs from each event the page gets. Never raises."""
    try:
        k = ev.get("t")
        if k == "delta":
            txt = str(ev.get("text") or "")
            if txt and txt != _SEM_TEXTO:
                reg["n_texto"] = reg.get("n_texto", 0) + len(txt)
            # "ESCALAR:" only at the start of a line (runtime.ESCALAR_RE); a line may come split across deltas, so the
            # current unfinished line is kept (a too-long one is cut with a non-line-start prefix: no false match)
            linha = reg.get("_linha", "") + txt
            if runtime.ESCALAR_RE.search(linha):
                reg["escalar"] = True
            ultima = linha.rsplit("\n", 1)[-1]
            reg["_linha"] = ultima if len(ultima) <= 200 else "x" + ultima[-40:]
        elif k == "latency" and reg.get("ms_primeiro_texto") is None:
            reg["ms_primeiro_texto"] = ev.get("ms")
        elif k == "tool":
            if ev.get("phase") == "use":
                reg["n_tools"] = reg.get("n_tools", 0) + 1
                if ev.get("agent"):
                    reg.setdefault("agentes", []).append(str(ev["agent"]))
            elif ev.get("escalar"):   # runtime.escalar_em on the tool/agent result
                reg["escalar"] = True
        elif k == "usage":
            last = runtime.LIMITS.get("last") or {}
            reg.update(tokens_total=last.get("total"), tokens_out=last.get("out"))
        elif k == "error":
            if ev.get("cancelled"):
                reg["cancelado"] = True
            else:
                reg["erro"] = str(ev.get("message") or "erro")
        elif k == "complete" and ev.get("erro") and not reg.get("erro"):
            reg["erro"] = "falha interna"
    except Exception:  # noqa: BLE001
        pass


def _registra(t):
    """One line in turnos.jsonl for a finished turn (nothing for a request rejected before it started)."""
    reg = t.get("reg")
    if not reg:
        return
    try:
        info = reg.pop("info", None) or {}
        for k in ("atalho", "ms_gemini", "disjuntor_abriu", "filtrado"):
            if info.get(k):
                reg[k] = info[k]
        reg["gemini"] = info.get("gemini") or "nao_usado"
        reg["ms"] = int((time.monotonic() - t["t0"]) * 1000)
        reg["cancelado"] = bool(reg.get("cancelado") or t.get("cancelado"))
        if reg.get("rota") in ("claude", "filtro_local→claude"):
            if reg.get("erro") and not reg["cancelado"]:
                reg["rota"] = "erro"
            elif not reg.get("n_texto") and not reg["cancelado"]:
                reg["resposta_vazia"] = True
        turnolog.registrar(reg)
    except Exception as e:  # noqa: BLE001 — the log never costs a turn
        print(f"[turnolog] {type(e).__name__}: {e}", flush=True)


def cancelar():
    """Esc: stops the Claude process (even one not started yet) and drops the session of the channel that was running."""
    with TURNO_LOCK:
        t = TURNO["atual"]
        if t:
            t["cancelado"] = True
            (CHAT if t["canal"] == "chat" else SESSION)["id"] = None
    return runtime.cancel_active()


def _guarda_sessao(t, alvo, **campos):
    """A finished turn's session id, unless that turn was cancelled meanwhile."""
    with TURNO_LOCK:
        if t["cancelado"]:
            return False
        alvo.update(**campos)
        return True


def _obj(raw):
    """A POST body as a dict; bad JSON or a non-object (list, number, string) is a ValueError -> 400, not a traceback."""
    try:
        body = json.loads(raw or b"{}")
    except ValueError:   # JSONDecodeError and UnicodeDecodeError
        raise ValueError("bad json") from None
    if not isinstance(body, dict):
        raise ValueError("json object required")
    return body


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("JARVIS_VERBOSE"):
            sys.stderr.write("  " + (fmt % args) + "\n")

    def _json(self, body, code=200):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, data, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read(self, limit):
        n = int(self.headers.get("Content-Length", 0))
        if n < 0 or n > limit:
            raise ValueError("body too large")
        return self.rfile.read(n) if n else b""

    def _host_ok(self):
        return self.headers.get("Host", "") in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

    def _origin_ok(self):
        return self.headers.get("Origin") in {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}

    def _token_ok(self):
        supplied = self.headers.get("X-Jarvis-Token", "")
        return bool(supplied) and secrets.compare_digest(supplied, API_TOKEN)

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "invalid host"}, 403)
        p = urlparse(self.path).path

        if p == "/api/status":
            g = memory.build_graph()
            motor = runtime.motor_atual()
            # fim da pausa do Claude (epoch), so com o agy no comando pela regra; forcado ou no Claude: None
            ate = (motor_regra.espera_ativa(motor_regra._le(motor_regra.ARQUIVO), time.time())
                   if motor == "agy" and runtime.MOTOR == "auto" else None)
            return self._json(dict(
                runtime=runtime.runtime_kind(), version=runtime.version(), motor=motor, motor_ate=ate,
                motor_forcado=runtime.MOTOR in ("claude", "agy"),
                motor_motivo=(motor_regra.ULTIMO["motivo"] if runtime.MOTOR == "auto" else f"forcado: {runtime.MOTOR}"
                              if runtime.MOTOR in ("claude", "agy") else f"FEDORA_MOTOR={runtime.MOTOR!r} invalido, usando claude"),
                model=runtime.MODEL or "Claude default", permission=runtime.PERMISSION,
                workdir=runtime.WORKDIR, vault=g["vault"], notes=g["total"],
                links=len(g["links"]), session=SESSION["id"],
                ear=listener.EAR.state(), ear_error=listener.EAR.error))

        if p == "/api/events":   # ?passivo=1: the orb, which only shows level/state and must not count as an open page
            return self._events(parse_qs(urlparse(self.path).query).get("passivo", [""])[0] == "1")

        if p == "/api/system":
            return self._json(dict(sysinfo.snapshot(), claude=runtime.LIMITS))

        if p == "/api/vault":
            return self._json(notes.vault_summary())

        if p == "/api/daily":
            return self._json(dict(notes=notes.listing()))

        if p == "/api/daily/read":
            try:
                return self._json(notes.read(parse_qs(urlparse(self.path).query).get("id", [""])[0]))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        rel = "index.html" if p == "/" else p.lstrip("/")
        f = (UI / rel).resolve()
        if not f.is_relative_to(UI.resolve()) or not f.is_file():   # startswith let "ui-x/" next to "ui/" through
            return self._bytes(b"not found", "text/plain", 404)
        data = f.read_bytes()
        if rel == "index.html":
            data = data.replace(b"__JARVIS_TOKEN__", API_TOKEN.encode())
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        return self._bytes(data, ctype)

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            raw = self._read(MAX_JSON)   # before any reply: an unread body would be parsed as the next keep-alive request
        except (TypeError, ValueError):
            raw = None
            self.close_connection = True   # body left unread: this connection can't be reused
        if not self._host_ok() or not self._origin_ok():
            return self._json({"error": "origin rejected"}, 403)
        if not self._token_ok():
            return self._json({"error": "unauthorized"}, 401)
        if raw is None:
            return self._json({"error": "body too large"}, 413)
        ctype = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if p in {"/api/run", "/api/speak", "/api/cancel", "/api/chat/new", "/api/daily/save", "/api/tidy", "/api/ear"} and ctype != "application/json":
            return self._json({"error": "application/json required"}, 415)
        body = {}
        if p in {"/api/run", "/api/speak", "/api/daily/save", "/api/tidy", "/api/ear"}:
            try:
                body = _obj(raw)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        if p == "/api/speak":
            text = str(body.get("text") or "").strip()
            if not text:
                speech.stop()
                return self._json({"ok": True})
            if not speech.ready():
                return self._json({"error": "Piper não encontrado (PIPER_BIN / PIPER_VOICE)"}, 503)
            # a question keeps the mic open (8 s, no "Ok, Fedora") once the voice has finished, so it doesn't hear itself
            then = (lambda: listener.EAR.control("listen")) if text.rstrip().endswith("?") else None
            return self._json({"ok": speech.say(text, then)})

        if p == "/api/ear":
            action = str(body.get("action", ""))
            if action not in {"on", "off", "listen", "dictate", "stop", "focus", "blur"}:
                return self._json({"error": "ação desconhecida"}, 400)
            return self._json({"state": listener.EAR.control(action)})

        if p == "/api/daily/save":
            # mtime travels as a decimal string (ns > 2**53 would be rounded by JSON.parse); an int is still accepted
            try:
                return self._json({"mtime": notes.save(str(body.get("id", "")), str(body.get("text", "")), body.get("mtime"))})
            except notes.Conflict as e:
                return self._json({"error": str(e)}, 409)
            except ValueError as e:
                return self._json({"error": str(e)[:200]}, 400)
            except OSError as e:   # file locked by Obsidian/sync, disk full: the temp file is already cleaned up
                return self._json({"error": f"não consegui gravar a nota ({e.strerror or e})"[:200]}, 503)

        if p == "/api/tidy":
            try:
                note = str(body.get("note") or "")
                if not note.strip():
                    return self._json({"error": "nota vazia"}, 400)
                return self._json({"text": tidy.tidy_note(note)})
            except RuntimeError as e:
                return self._json({"error": str(e)}, 503)

        if p == "/api/chat/new":
            CHAT["id"] = None
            return self._json({"ok": True})

        if p == "/api/cancel":
            return self._json({"ok": True, "stopped": cancelar()})

        if p == "/api/run":
            if not RUN_LOCK.acquire(blocking=False):
                return self._json({"error": "JARVIS is already processing a request"}, 409)
            t = None
            try:   # opened right after acquire: a failure in _turno_novo must never leave RUN_LOCK held
                t = _turno_novo(body.get("canal") == "chat")
                return self._stream(body, t)
            finally:
                try:
                    _turno_fim(t)
                finally:   # the lock never depends on the turn log
                    RUN_LOCK.release()

        return self._json({"error": "no such endpoint"}, 404)

    def _events(self, passivo=False):
        """Server-Sent Events: what the ear hears, for as long as the page is open."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        q = listener.EAR.subscribe(passivo=passivo)
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            listener.EAR.unsubscribe(q)
        self.close_connection = True

    def _stream(self, payload, turno):
        message = str(payload.get("message") or "").strip()
        if not message:
            return self._json({"error": "empty message"}, 400)
        chat = payload.get("canal") == "chat"
        if payload.get("fresh"):
            (CHAT if chat else SESSION)["id"] = None

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        reg = turno["reg"] = dict(canal="chat" if chat else "voz", pedido=message, rota="claude",
                                  modo="chat" if chat else None, info={})

        def emit(ev):
            _observa(reg, ev)   # before the write: a closed page must not hide the turn from the log
            self.wfile.write((json.dumps(ev) + "\n").encode())
            self.wfile.flush()

        # after the 200 nothing may escape as an empty reply: any failure still reaches the page as error + complete
        try:
            self._turno(message, chat, emit, turno)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            print(f"[run] {type(e).__name__}: {e}", flush=True)
            reg["erro"] = f"{type(e).__name__}: {e}"
            try:
                emit(dict(t="error", message=f"Falha interna: {str(e)[:300] or type(e).__name__}"))
                emit(dict(t="complete", ms=0, erro=True))
            except OSError:
                pass

    def _turno(self, message, chat, emit, turno):
        reg = turno.get("reg") or {}
        cmd = None if chat else commands.handle(message, info=reg.setdefault("info", {}))
        if reg.get("info", {}).get("filtrado"):
            reg["rota"] = "filtro_local→claude"
        if cmd:
            if cmd.get("message") is None:
                reg["rota"] = "atalho" if cmd.get("note") == "atalho" else "comando"
            if cmd.get("note"):
                emit(dict(t="note", message=cmd["note"]))
            if cmd.get("fresh"):
                SESSION["id"] = None
            if cmd.get("focus"):
                emit(dict(t="focus", id=str(cmd["focus"]).lower()))
            if cmd.get("graph"):
                emit(dict(t="graph"))
            if cmd.get("message") is None:
                delay = float(cmd.get("delay") or 0)
                for event in cmd.get("events", []):
                    if delay and event.get("phase") == "result":
                        time.sleep(delay)
                        delay = 0
                    emit(event)
                emit(dict(t="delta", text=cmd.get("reply", "Done.")))
                emit(dict(t="complete", ms=0))
                if cmd.get("note") == "atalho":
                    conversa.lembrar(message, cmd.get("reply"))   # "abre de novo" after a shortcut still works
                return
            message = cmd["message"]

        if chat:
            reg["sessao_nova"] = CHAT["id"] is None
            return self._chat(message, emit, turno)
        full = conversa.completo(message)
        motor = runtime.motor_atual()
        modo_sessao = (full, motor)   # um id de sessao do Claude nao serve no agy (e vice-versa): trocar de motor abre sessao nova
        if not conversa.continua(SESSION, modo_sessao):
            SESSION.update(id=None, turnos=0)
        reg.update(modo={False: "leve", "equipe": "equipe", True: "completo"}.get(full, str(full)),
                   sessao_nova=SESSION["id"] is None)
        extra = [commands.turn_block(message)]
        if SESSION["id"] is None:
            extra.append(conversa.recente())
        prompt = "\n\n".join([x for x in extra if x] + ["## Pedido\n" + message])
        hit = memory.top_match(message)
        if hit:
            emit(dict(t="focus", id=hit))
        reply = []
        for ev in runtime.run(prompt, SESSION["id"], commands.system_block(equipe=(full == "equipe")), full, motor):
            if ev.get("t") == "delta":
                reply.append(ev.get("text", ""))
            if ev.get("t") == "complete" and runtime.valid_session(ev.get("session_id")):
                if _guarda_sessao(turno, SESSION, id=ev["session_id"], modo=modo_sessao, ultima=time.time(), turnos=SESSION["turnos"] + 1):
                    conversa.lembrar(message, "".join(reply))
            if ev.get("t") == "error":
                SESSION["id"] = None
            emit(ev)

    def _chat(self, message, emit, turno):
        prompt = commands.turn_block(message) + "\n\n## Pedido\n" + message
        motor = runtime.motor_atual()
        if CHAT.get("motor") not in (None, motor):   # outro motor nao retoma a sessao do primeiro
            CHAT["id"] = None
        for ev in runtime.run(prompt, CHAT["id"], commands.system_block(chat=True), True, motor):
            if ev.get("t") == "complete" and runtime.valid_session(ev.get("session_id")):
                _guarda_sessao(turno, CHAT, id=ev["session_id"], motor=motor)
            emit(ev)   # an error keeps the session: the chat history is worth more than a retry from scratch


RETRO_ATRASO, RETRO_INTERVALO = 60, 6 * 3600


def _retro_loop():
    """Weekly retrospective in the vault inbox (retro.py): 60 s after start, then every 6 h. Never stops the server."""
    time.sleep(RETRO_ATRASO)
    while True:
        try:
            import retro
            retro.rodar()
        except Exception as e:  # noqa: BLE001
            print(f"[retro] falhou: {type(e).__name__}: {str(e)[:200]}", flush=True)
        try:   # cache manual do agy (contexto.py) anda junto com a retrospectiva: bloco automático + aviso de revisão
            import contexto
            r = contexto.atualiza()
            if r.get("revisao_atrasada"):
                print(f"[contexto] revise docs/CONTEXTO-AGY.md à mão (última revisão: {r.get('dias')} dias atrás)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[contexto] falhou: {type(e).__name__}: {str(e)[:200]}", flush=True)
        time.sleep(RETRO_INTERVALO)


def main():
    kind = runtime.runtime_kind()
    g = memory.build_graph()
    brain = (f"Claude Code {runtime.version()} — your subscription, no API key"
             if kind == "claude" else "MOCK — claude CLI not reachable")
    print(f"""
  FEDORA · assistente local (toque no orbe ou Ctrl+Alt+Espaço)
  ────────────────────────────────────────────────
  brain        {brain}
  model        {runtime.MODEL or 'Claude default'}
  vault        {g['total']} notes · {len(g['links'])} links
               {g['vault']}
  voice        Piper ({os.path.basename(speech.VOICE)})
  listening    push-to-talk {listener.HOTKEY} · {listener.STT_URL}
  workdir      {runtime.WORKDIR}
  open         http://localhost:{PORT}
""", flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    if os.environ.get("FEDORA_EAR", "1") != "0":
        listener.EAR.start()
    if os.environ.get("FEDORA_ORB", "1") != "0" and os.name == "nt":
        import subprocess
        py = str(ROOT / ".venv" / "Scripts" / "pythonw.exe")   # o do venv: tem numpy
        subprocess.Popen([py if os.path.exists(py) else sys.executable, str(ROOT / "orb.py")],
                         creationflags=0x08000000)   # CREATE_NO_WINDOW; o orb encerra sozinho quando o servidor cai
    if os.environ.get("FEDORA_RETRO", "1") != "0":
        threading.Thread(target=_retro_loop, daemon=True, name="retro").start()
    if os.environ.get("JARVIS_OPEN", "1") != "0":
        webbrowser.open(f"http://localhost:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  down.")
        srv.shutdown()


if __name__ == "__main__":
    main()
