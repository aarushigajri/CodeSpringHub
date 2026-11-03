"""The Hub's own small database (SQLite in Phase 0, PostgreSQL later).

It holds only machine state, never the knowledge itself:
  - docs_fts     full-text search index over knowledge documents (rebuilt from files)
  - audit        one row per tool call: who, what, which resource, how much was returned
  - reviews      "send this to BSR" tickets raised from Claude
  - suggestions  context edits Claude proposed, waiting for a person to apply them
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

from . import config, knowledge

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(doc_id UNINDEXED, lab_id UNINDEXED, category, title, body);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, ts TEXT, user TEXT, tool TEXT, args TEXT,
    ok INTEGER, chars_out INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY, ts TEXT, user TEXT, lab_id TEXT, summary TEXT,
    resource_ids TEXT, status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY, ts TEXT, user TEXT, lab_id TEXT, doc_id TEXT,
    suggestion TEXT, status TEXT DEFAULT 'pending');
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    # The audit log can contain users' question text: shared with the folder's group
    # (BSR in Phase 0) and nobody else. setgid keeps new files in that group.
    if not config.VAR_DIR.exists():
        config.VAR_DIR.mkdir(parents=True)
        os.chmod(config.VAR_DIR, 0o2770)
    new_db = not config.DB_PATH.exists()
    conn = sqlite3.connect(config.DB_PATH)
    if new_db:
        os.chmod(config.DB_PATH, 0o660)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---- search index ---------------------------------------------------------------

def ensure_index(conn: sqlite3.Connection, force: bool = False) -> bool:
    """Rebuild the search index if any knowledge file changed. Returns True if rebuilt."""
    fp = knowledge.knowledge_fingerprint()
    row = conn.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
    if not force and row and row["value"] == fp:
        return False
    with conn:
        conn.execute("DELETE FROM docs_fts")
        for lab in knowledge.load_labs().values():
            for doc in knowledge.load_documents(lab).values():
                conn.execute(
                    "INSERT INTO docs_fts VALUES (?,?,?,?,?)",
                    (doc.id, doc.lab_id, doc.category, doc.title, doc.path.read_text()),
                )
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('fingerprint', ?)", (fp,))
    return True


def _fts_query(text: str) -> str:
    # Quote each word so user text can't inject FTS syntax; OR them for recall.
    words = [w.replace('"', "") for w in text.split() if w.strip('"')]
    return " OR ".join(f'"{w}"' for w in words)


def search(conn, query: str, lab_ids: list[str], limit: int = 8) -> list[dict]:
    ensure_index(conn)
    if not lab_ids or not _fts_query(query):
        return []
    marks = ",".join("?" * len(lab_ids))
    rows = conn.execute(
        f"""SELECT doc_id, lab_id, category, title,
                   snippet(docs_fts, 4, '**', '**', ' … ', 24) AS snippet
            FROM docs_fts WHERE docs_fts MATCH ? AND lab_id IN ({marks})
            ORDER BY rank LIMIT ?""",
        (_fts_query(query), *lab_ids, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- audit, reviews, suggestions -------------------------------------------------

def log_call(conn, user, tool, args, ok, chars_out, error=None):
    with conn:
        conn.execute(
            "INSERT INTO audit (ts,user,tool,args,ok,chars_out,error) VALUES (?,?,?,?,?,?,?)",
            (now(), user, tool, json.dumps(args, default=str), int(ok), chars_out, error),
        )


def add_review(conn, user, lab_id, summary, resource_ids) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO reviews (ts,user,lab_id,summary,resource_ids) VALUES (?,?,?,?,?)",
            (now(), user, lab_id, summary, json.dumps(resource_ids)),
        )
    return cur.lastrowid


def add_suggestion(conn, user, lab_id, doc_id, suggestion) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO suggestions (ts,user,lab_id,doc_id,suggestion) VALUES (?,?,?,?,?)",
            (now(), user, lab_id, doc_id, suggestion),
        )
    return cur.lastrowid
