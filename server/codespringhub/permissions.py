"""Who is asking, and what may they see?

Rule from the proposal: nobody gets anything through the Hub that they could not
already read on the cluster. Three checks enforce it:

  1. Lab membership: the user must be in the lab's Unix group (from lab.yaml).
  2. Registration: only datasets listed in a lab's datasets.yaml exist for Claude,
     and Claude refers to them by Hub ID, never by path.
  3. The filesystem itself: the resolved file must sit inside the registered folder
     (no "../" or symlink escapes) and be readable by the user.

Phase 0 runs locally over stdio as the logged-in user, so the user is simply whoever
started the process and check 3 is a real OS check. When the server moves to a shared
host with SSO, current_user() will come from the login token and check 3 will need a
helper that runs as that user.
"""

import getpass
import grp
import os
import pwd
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .knowledge import Dataset, Lab


def current_user() -> str:
    return getpass.getuser()


def user_groups(user: str) -> set[str]:
    primary_gid = pwd.getpwnam(user).pw_gid
    names = set()
    for gid in os.getgrouplist(user, primary_gid):
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            pass
    return names


def can_access_lab(user: str, lab: Lab) -> bool:
    return lab.unix_group in user_groups(user)


def require_lab(user: str, labs: dict[str, Lab], lab_id: str) -> Lab:
    lab = labs.get(lab_id)
    # Same message whether the lab doesn't exist or the user isn't a member,
    # so the tool can't be used to discover other labs.
    if lab is None or not can_access_lab(user, lab):
        raise ToolError(f"No accessible lab '{lab_id}'. Call list_labs to see the labs you can use.")
    return lab


def resolve_asset(dataset: Dataset, asset: str = "") -> Path:
    """Turn (dataset, relative file name) into a real path, refusing anything outside it."""
    root = Path(os.path.realpath(dataset.path))
    target = Path(os.path.realpath(root / asset)) if asset else root
    if target != root and root not in target.parents:
        raise ToolError("That file is outside the registered dataset.")
    if not target.exists():
        raise ToolError(f"'{asset or dataset.id}' was not found in the registered dataset.")
    if not os.access(target, os.R_OK):
        raise ToolError("You do not have read permission for that file on the cluster.")
    return target
