"""Who is asking, and what may they see or change?

Rule from the proposal: nobody gets anything through the Hub that they could not
already read on the cluster. Three checks enforce it:

  1. Lab membership: the user must be in the lab's Unix group (from lab.yaml). Core facilities
     (kind: core) have two sections: `shared/`, for everyone using the Hub, and `internal/`, for
     the core's staff (its Unix group) only.
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
import time
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
    return bool(lab.unix_group) and lab.unix_group in user_groups(user)


def is_hub_user(user: str) -> bool:
    """In any registered lab or core, or BSR: the people a core's shared section is for. (On disk,
    the ACLs give exactly these groups read access to every core's shared folder.)"""
    from .knowledge import load_labs
    groups = user_groups(user)
    if config.BSR_GROUP in groups:
        return True
    key = (user, config.KNOWLEDGE_DIR, frozenset(groups))
    hit = _HUB_USERS.get(key)
    if hit and time.monotonic() - hit[1] < 30:  # checked once per item otherwise; lab.yaml rarely changes
        return hit[0]
    ok = any(lab.unix_group in groups for lab in load_labs().values() if lab.unix_group)
    _HUB_USERS[key] = (ok, time.monotonic())
    return ok


_HUB_USERS: dict = {}


def is_pi(user: str, lab: Lab) -> bool:
    return user in lab.pis


def can_access_lab(user: str, lab: Lab) -> bool:
    """Lab-shared content: the lab's members, plus BSR, who support every pilot lab. A core's
    internal section: its staff only (BSR keeps the template of a core not on the Hub yet)."""
    if lab.is_core:
        return is_member(user, lab) or (not lab.onboarded and is_bsr(user))
    return is_member(user, lab) or is_bsr(user)


def can_view_lab(user: str, lab: Lab) -> bool:
    """May see the lab at all: full access, or a core's shared section as a Hub user."""
    return can_access_lab(user, lab) or (lab.is_core and is_hub_user(user))


def can_see(user: str, lab: Lab, owner: str | None, private: bool = False, shared: bool = False) -> bool:
    """Personal content (owner set): only the owner and the lab's PIs (a core's lead). Private
    content: only the owner. Never BSR as such. A core's shared section: everyone using the Hub."""
    if owner is None:
        if shared:
            return lab.is_core and can_view_lab(user, lab)
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


def require_visible_lab(user: str, labs: dict[str, Lab], lab_id: str) -> Lab:
    """For reading: a lab you belong to, or any core (whose shared section everyone may read)."""
    lab = labs.get(lab_id)
    if lab is None or not can_view_lab(user, lab):
        raise NotFound(f"No accessible lab or core '{lab_id}'. Call list_labs to see the ones you can use.")
    return lab


def require_lab_editor(user: str, labs: dict[str, Lab], lab_id: str) -> Lab:
    lab = require_lab(user, labs, lab_id)
    if not can_edit_lab(user, lab):
        who = "the core's staff" if lab.is_core else "the lab's Hub contact or BSR"
        raise Forbidden(f"You can view {lab.name} but not edit it. Ask {who} for edit access.")
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
