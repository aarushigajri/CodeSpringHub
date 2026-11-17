"""Who is asking, and what may they see or change?

Rule from the proposal: nobody gets anything through the Hub that they could not
already read on the cluster. Three checks enforce it:

  1. Lab membership: the user must be in the lab's Unix group (from lab.yaml).
  2. Registration: only datasets listed in a lab's datasets.yaml exist for Claude,
     and Claude refers to them by Hub ID, never by path.
  3. The filesystem itself: the resolved file must sit inside the registered folder
     (no "../" or symlink escapes) and be readable by the user.

Identity: every request runs "as" one CSHL user. The web portal and (later) the HTTPS
MCP endpoint set that user from the login (see identity.py) via `acting_as()`. Over
stdio nobody sets it, and the user is whoever started the process.

Check 3 uses os.access, which tests the *process* user. That is exact for stdio and the
local portal (the process is the user). A shared HTTPS service will need a helper that
runs as the user; see docs/DESIGN.md section 10.
"""

import contextvars
import getpass
import grp
import os
import pwd
from contextlib import contextmanager
from pathlib import Path

from . import config
from .errors import Forbidden, NotFound
from .knowledge import Dataset, Lab


_acting_user: contextvars.ContextVar[str | None] = contextvars.ContextVar("acting_user", default=None)


@contextmanager
def acting_as(user: str):
    token = _acting_user.set(user)
    try:
        yield
    finally:
        _acting_user.reset(token)


def current_user() -> str:
    return _acting_user.get() or getpass.getuser()


def user_groups(user: str) -> set[str]:
    primary_gid = pwd.getpwnam(user).pw_gid
    names = set()
    for gid in os.getgrouplist(user, primary_gid):
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            pass
    return names


def display_name(user: str) -> str:
    try:
        return pwd.getpwnam(user).pw_gecos.split(",")[0] or user
    except KeyError:
        return user


def is_bsr(user: str) -> bool:
    return config.BSR_GROUP in user_groups(user)


def is_member(user: str, lab: Lab) -> bool:
    return lab.unix_group in user_groups(user)


def is_pi(user: str, lab: Lab) -> bool:
    return user in lab.pis


def can_access_lab(user: str, lab: Lab) -> bool:
    """Lab-shared content: the lab's members, plus BSR, who support every pilot lab."""
    return is_member(user, lab) or is_bsr(user)


def can_see(user: str, lab: Lab, owner: str | None, private: bool = False) -> bool:
    """Personal content (owner set): only the owner and the lab's PIs. Private content: only the
    owner. Never BSR as such."""
    if owner is None:
        return can_access_lab(user, lab)
    if private:
        return user == owner
    return user == owner or (is_pi(user, lab) and is_member(user, lab))


def can_edit_lab(user: str, lab: Lab) -> bool:
    """Lab members (and BSR) can edit lab-shared knowledge. The filesystem has the final
    say: the process must also be able to write the lab folder."""
    return can_access_lab(user, lab) and os.access(lab.dir, os.W_OK)


def require_lab(user: str, labs: dict[str, Lab], lab_id: str) -> Lab:
    lab = labs.get(lab_id)
    # Same message whether the lab doesn't exist or the user isn't a member,
    # so the Hub can't be used to discover other labs.
    if lab is None or not can_access_lab(user, lab):
        raise NotFound(f"No accessible lab '{lab_id}'. Call list_labs to see the labs you can use.")
    return lab


def require_lab_editor(user: str, labs: dict[str, Lab], lab_id: str) -> Lab:
    lab = require_lab(user, labs, lab_id)
    if not can_edit_lab(user, lab):
        raise Forbidden(f"You can view {lab.name} but not edit it. Ask the lab's Hub contact or BSR for edit access.")
    return lab


def require_member(user: str, labs: dict[str, Lab], lab_id: str) -> Lab:
    """Personal areas belong to lab members; BSR staff who aren't members don't get one."""
    lab = require_lab(user, labs, lab_id)
    if not is_member(user, lab):
        raise Forbidden(f"Personal items are for members of {lab.name}.")
    return lab


def resolve_asset(dataset: Dataset, asset: str = "") -> Path:
    """Turn (dataset, relative file name) into a real path, refusing anything outside it."""
    root = Path(os.path.realpath(dataset.path))
    target = Path(os.path.realpath(root / asset)) if asset else root
    if target != root and root not in target.parents:
        raise Forbidden("That file is outside the registered dataset.")
    if not target.exists():
        raise NotFound(f"'{asset or dataset.id}' was not found in the registered dataset.")
    if not os.access(target, os.R_OK):
        raise Forbidden("You do not have read permission for that file on the cluster.")
    return target
