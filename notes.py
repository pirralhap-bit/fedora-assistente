"""Daily notes in the Obsidian vault: list, read, save (with a conflict check), plus the vault summary."""
import datetime
import os
import pathlib
import time
import zoneinfo

import memory

DAILY = pathlib.Path(os.path.expanduser(os.environ.get("JARVIS_DAILY_DIR", memory.VAULT / "08-DNT 📅 Daily Notes")))
INBOX = memory.VAULT / "00-INB 📥 Inbox" / "_a-processar"
TZ = zoneinfo.ZoneInfo(os.environ.get("JARVIS_TZ", "America/Sao_Paulo"))
MONTHS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
          "setembro", "outubro", "novembro", "dezembro"]
DAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
MAX_NOTE = 2 * 1024 * 1024


class Conflict(Exception):
    pass


def today_rel(now=None):
    """Same layout the vault already uses: 2026/11 - novembro/03 - terça-feira.md"""
    d = (now or datetime.datetime.now(TZ)).date()
    return f"{d.year}/{d.month:02d} - {MONTHS[d.month - 1]}/{d.day:02d} - {DAYS[d.weekday()]}.md"


def _path(rel):
    p = (DAILY / rel).resolve()
    if p.suffix != ".md" or not p.is_relative_to(DAILY.resolve()):
        raise ValueError("note outside the daily folder")
    return p


def listing(limit=14):
    files = sorted((str(p.relative_to(DAILY)) for p in DAILY.rglob("*.md")), reverse=True)[:limit]
    t = today_rel()
    if t not in files:
        files.insert(0, t)
    return [dict(id=f, name=pathlib.PurePath(f).stem, today=f == t) for f in files]


def read(rel):
    p = _path(rel)
    if not p.exists():
        return dict(id=rel, text="", mtime=None, path=str(p.relative_to(memory.VAULT)))
    return dict(id=rel, text=p.read_text(encoding="utf-8"), mtime=p.stat().st_mtime_ns,
                path=str(p.relative_to(memory.VAULT)))


def save(rel, text, mtime):
    """Write the whole note. Refuses if the file changed since it was read (Obsidian edit, sync)."""
    if len(text.encode()) > MAX_NOTE:
        raise ValueError("note too large")
    p = _path(rel)
    current = p.stat().st_mtime_ns if p.exists() else None
    if current != mtime:
        raise Conflict("a nota mudou desde que foi aberta")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.jarvis-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return p.stat().st_mtime_ns


def vault_summary():
    files = [p for p in memory.VAULT.rglob("*.md")
             if not any(x.startswith(".") for x in p.relative_to(memory.VAULT).parts)]
    stats = sorted(((p.stat().st_mtime, p) for p in files), reverse=True)
    cutoff = time.time() - 86400
    last = stats[0] if stats else None
    return dict(total=len(files), today=sum(1 for t, _ in stats if t > cutoff),
                inbox=len(list(INBOX.glob("*.md"))) if INBOX.exists() else 0,
                last=dict(title=last[1].stem, at=last[0]) if last else None)
