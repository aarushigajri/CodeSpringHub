"""Where the Hub keeps things.

Everything is relative to HUB_HOME, which defaults to the repository root.
Set CODESPRINGHUB_HOME to point a test or a second deployment somewhere else.
"""

import os
from pathlib import Path

HUB_HOME = Path(os.environ.get("CODESPRINGHUB_HOME", Path(__file__).resolve().parents[2]))

# Human-edited content: one folder per lab, plus BSR-global files (workflows.yaml).
KNOWLEDGE_DIR = HUB_HOME / "knowledge"

# Machine state. var/ is BSR's; var/events/ is a drop box where each person's process writes
# its own usage log (<user>.db): people can add their own file but not read anyone else's.
VAR_DIR = HUB_HOME / "var"
DB_PATH = VAR_DIR / "hub.db"  # BSR-only: review-ticket status
# (var/events/ and knowledge/roles.yaml, the BSR-managed PI list, are derived from these at use time.)

# BSR staff: support every lab's shared content and the workflow registry, never personal content.
BSR_GROUP = "bioinformatics"

# Per-person private files (portal link, personal search index). Never inside the shared Hub folder.
USER_DIR = Path(os.environ.get("CODESPRINGHUB_USER_DIR", Path.home() / ".codespringhub"))

# What researchers' laptops SSH to when Claude Desktop starts the Hub (shown on the Connect page).
CONNECT_HOST = os.environ.get("CODESPRINGHUB_CONNECT_HOST", "")

# The folder picker and dataset registration are limited to these roots.
BROWSE_ROOTS = [r for r in os.environ.get("CODESPRINGHUB_BROWSE_ROOTS", "/grid").split(":") if r]

# Output limits: the most any single tool call may send back to the model.
MAX_OUTPUT_CHARS = 20_000
MAX_TABLE_ROWS = 50
MAX_LISTED_FILES = 200
MAX_QUERY_FILE_BYTES = 500 * 1024**2  # query_table refuses files bigger than this
