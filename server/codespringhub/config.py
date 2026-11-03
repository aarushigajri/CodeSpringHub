"""Where the Hub keeps things.

Everything is relative to HUB_HOME, which defaults to the repository root.
Set CODESPRINGHUB_HOME to point a test or a second deployment somewhere else.
"""

import os
from pathlib import Path

HUB_HOME = Path(os.environ.get("CODESPRINGHUB_HOME", Path(__file__).resolve().parents[2]))

# Human-edited content: one folder per lab, plus BSR-global files (workflows.yaml).
KNOWLEDGE_DIR = HUB_HOME / "knowledge"

# Machine state (search index, audit log, review tickets). Safe to delete; it is rebuilt.
VAR_DIR = HUB_HOME / "var"
DB_PATH = VAR_DIR / "hub.db"

# The folder picker and dataset registration are limited to these roots.
BROWSE_ROOTS = [r for r in os.environ.get("CODESPRINGHUB_BROWSE_ROOTS", "/grid").split(":") if r]

# Output limits: the most any single tool call may send back to the model.
MAX_OUTPUT_CHARS = 20_000
MAX_TABLE_ROWS = 50
MAX_LISTED_FILES = 200
MAX_QUERY_FILE_BYTES = 500 * 1024**2  # query_table refuses files bigger than this
