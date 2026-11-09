"""The Hub's small databases (SQLite). They hold machine state only, never the knowledge itself.

    var/events/<user>.db   that person's usage log and review requests. Written only by their own
                           process; readable by BSR (usage logs are shared with BSR by connecting).
                           Requests touching a personal resource are stored with details withheld,
                           because BSR is not authorised to see personal material.
    ~/.codespringhub/index.db
                           that person's search index: only documents they may see.
    var/hub.db             BSR-only: status of review tickets.
"""

import json
import os
import pwd
import sqlite3
from datetime import datetime, timezone

from . import acl, config, knowledge
from .errors import HubError

EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, ts TEXT, user TEXT, lab_id TEXT, tool TEXT, args TEXT,
    ok INTEGER, chars_out INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY, ts TEXT, user TEXT, lab_id TEXT, summary TEXT, resource_ids TEXT);
CREATE TABLE IF NOT EXISTS notices (ts TEXT, kind TEXT);
"""
INDEX_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(doc_id UNINDEXED, lab_id UNINDEXED, category, title, body);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
BSR_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_status (
    owner TEXT, review_id INTEGER, status TEXT, updated_by TEXT, ts TEXT, PRIMARY KEY (owner, review_id));
"""
WITHHELD = {"details": "withheld: personal resource"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _process_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


# ---- usage log (per person) -------------------------------------------------------

def events_connect() -> sqlite3.Connection:
    """This process's own usage log. Created 640 (owner writes, BSR reads), never with ACLs."""
    path = acl.events_dir() / f"{_process_user()}.db"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        os.close(fd)
        os.chmod(path, 0o640)
    elif path.stat().st_uid != os.getuid():
        # Someone else created a file with your name in the drop box.
        raise HubError("Your Hub usage log is owned by another account. Please tell BSR.")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(EVENTS_SCHEMA)
    return conn


def _redact(args: dict) -> dict:
    return WITHHELD if knowledge.is_personal_id(json.dumps(args, default=str)) else args


def log_call(user, tool, args, ok, chars_out, error=None, lab_id=None, personal=False):
    args = WITHHELD if personal else _redact(args)
    if args is WITHHELD and error:
        error = "failed"  # error text can echo personal names
    conn = events_connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO audit (ts,user,lab_id,tool,args,ok,chars_out,error) VALUES (?,?,?,?,?,?,?,?)",
                (now(), user, lab_id, tool, json.dumps(args, default=str), int(ok), chars_out, error),
            )
    finally:
        conn.close()


def add_review(user, lab_id, summary, resource_ids) -> int:
    conn = events_connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO reviews (ts,user,lab_id,summary,resource_ids) VALUES (?,?,?,?,?)",
                (now(), user, lab_id, summary, json.dumps(resource_ids)),
            )
        return cur.lastrowid
    finally:
        conn.close()


def note_once(kind: str) -> bool:
    """Record that a notice was shown; True the first time."""
    conn = events_connect()
    try:
        if conn.execute("SELECT 1 FROM notices WHERE kind=?", (kind,)).fetchone():
            return False
        with conn:
            conn.execute("INSERT INTO notices VALUES (?,?)", (now(), kind))
        return True
    finally:
        conn.close()


def read_events(table: str, only_user: str | None = None) -> list[dict]:
    """Rows from every usage log this process can read (BSR: all; others: their own).
    A row only counts if the file's owner is the user it claims to be from."""
    rows = []
    try:
        files = sorted((config.VAR_DIR / "events").glob("*.db"))
    except PermissionError:
        files = []
    for path in files:
        try:
            owner = pwd.getpwuid(path.stat().st_uid).pw_name
        except (KeyError, OSError):
            continue
        if path.stem != owner or (only_user and owner != only_user):
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            for r in conn.execute(f"SELECT * FROM {table}"):
                if r["user"] == owner:
                    rows.append(dict(r))
            conn.close()
        except sqlite3.Error:
            continue
    return rows


# ---- BSR-only state --------------------------------------------------------------

def bsr_connect() -> sqlite3.Connection:
    if not config.VAR_DIR.exists():
        config.VAR_DIR.mkdir(parents=True)
        os.chmod(config.VAR_DIR, 0o2770)
    new_db = not config.DB_PATH.exists()
    conn = sqlite3.connect(config.DB_PATH)
    if new_db:
        os.chmod(config.DB_PATH, 0o660)
    conn.row_factory = sqlite3.Row
    conn.executescript(BSR_SCHEMA)
    return conn


def review_statuses() -> dict[tuple[str, int], str]:
    try:
        conn = bsr_connect()
    except (PermissionError, sqlite3.Error):
        return {}
    try:
        return {(r["owner"], r["review_id"]): r["status"] for r in conn.execute("SELECT * FROM review_status")}
    finally:
        conn.close()


# ---- search index (per person) ------------------------------------------------------

def index_connect() -> sqlite3.Connection:
    config.USER_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(config.USER_DIR, 0o700)
    conn = sqlite3.connect(config.USER_DIR / "index.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(INDEX_SCHEMA)
    return conn


def ensure_index(conn, docs: list[knowledge.Document], force: bool = False) -> bool:
    """(Re)build the index from exactly the documents this user may see, whenever any of
    them changed. Returns True if rebuilt."""
    parts = []
    for d in docs:
        try:
            st = d.path.stat()
        except OSError:
            continue
        parts.append(f"{d.id}:{st.st_size}:{st.st_mtime_ns}")
    fp = "|".join(sorted(parts))
    row = conn.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
    if not force and row and row["value"] == fp:
        return False
    with conn:
        conn.execute("DELETE FROM docs_fts")
        for d in docs:
            try:
                body = d.path.read_text()
            except OSError:
                continue
            conn.execute("INSERT INTO docs_fts VALUES (?,?,?,?,?)", (d.id, d.lab_id, d.category, d.title, body))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('fingerprint', ?)", (fp,))
    return True


def _fts_query(text: str) -> str:
    # Quote each word so user text can't inject FTS syntax; OR them for recall.
    words = [w.replace('"', "") for w in text.split() if w.strip('"')]
    return " OR ".join(f'"{w}"' for w in words)


def search(docs: list[knowledge.Document], query: str, limit: int = 8) -> list[dict]:
    if not docs or not _fts_query(query):
        return []
    conn = index_connect()
    try:
        ensure_index(conn, docs)
        allowed = {d.id for d in docs}
        rows = conn.execute(
            """SELECT doc_id, lab_id, category, title,
                      snippet(docs_fts, 4, '**', '**', ' … ', 24) AS snippet
               FROM docs_fts WHERE docs_fts MATCH ? ORDER BY rank LIMIT ?""",
            (_fts_query(query), limit * 3),
        ).fetchall()
        return [dict(r) for r in rows if r["doc_id"] in allowed][:limit]
    finally:
        conn.close()
