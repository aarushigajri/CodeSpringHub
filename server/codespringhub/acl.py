"""File permissions that make one shared Hub folder safe for several labs.

Unix groups alone can't express "this lab AND BSR" or "this member AND their PI", so the
Hub uses POSIX ACLs (setfacl/getfacl, supported on /grid's GPFS). Numeric uid/gid are used
because CSHL group names can contain spaces.

    knowledge/                 group BSR rwx; each pilot lab group r-x (list labs, read workflows)
    knowledge/<lab>/           group BSR rwx + lab group rwx, inherited by everything inside
    knowledge/<lab>/members/<user>/
                               owner only (2700) + each PI of the lab; ACLs from the lab
                               folder are removed so BSR and other members can't get in
    var/                       BSR only; each pilot lab group --x (to reach var/events)
    var/events/                BSR rwx; each pilot lab group -wx (create own file, can't list);
                               sticky, so nobody can delete or rename someone else's log
"""

import grp
import os
import pwd
import subprocess
from pathlib import Path

from . import config


class ACLError(RuntimeError):
    pass


def _gid(group: str) -> int:
    return grp.getgrnam(group).gr_gid


def _uid(user: str) -> int:
    return pwd.getpwnam(user).pw_uid


def setfacl(path: Path, spec: str, *flags: str):
    r = subprocess.run(["setfacl", *flags, "-m", spec, str(path)], capture_output=True, text=True)
    if r.returncode:
        raise ACLError(f"setfacl {spec} on {path}: {r.stderr.strip()}")


def strip(path: Path, recursive=False):
    r = subprocess.run(["setfacl", *(["-R"] if recursive else []), "-b", str(path)], capture_output=True, text=True)
    if r.returncode:
        raise ACLError(f"setfacl -b on {path}: {r.stderr.strip()}")


def owning_group_entry(path: Path) -> str:
    """The real permission of the folder's own group (the mode bits show the ACL mask instead)."""
    r = subprocess.run(["getfacl", "-cpn", str(path)], capture_output=True, text=True)
    return next((ln for ln in r.stdout.splitlines() if ln.startswith("group::")), "")


def entries(path: Path) -> set[str]:
    """Extended ACL entries, numeric, e.g. {'group:1234:rwx', 'default:group:1234:rwx'}."""
    r = subprocess.run(["getfacl", "-cpn", str(path)], capture_output=True, text=True)
    if r.returncode:
        raise ACLError(r.stderr.strip())
    out = set()
    for line in r.stdout.splitlines():
        line = line.split("#")[0].strip()
        parts = line.split(":")
        named = (len(parts) == 3 and parts[1]) or (len(parts) == 4 and parts[0] == "default" and parts[2])
        if line and named:
            out.add(line)
    return out


# ---- labs ------------------------------------------------------------------------

def grant_lab(lab_dir: Path, unix_group: str):
    """Lab folder: BSR (owning group) and the lab group get rwx, now and for new files."""
    g = _gid(unix_group)
    os.chmod(lab_dir, 0o2770)
    setfacl(lab_dir, f"g:{g}:rwx,d:g:{g}:rwx,d:g::rwx,d:u::rwx,d:o::---,m::rwx,d:m::rwx", "-R")


def grant_lab_paths(unix_group: str):
    """What a lab's members need outside their folder: list labs, reach var/events."""
    g = _gid(unix_group)
    events = events_dir()
    setfacl(config.KNOWLEDGE_DIR, f"g:{g}:r-x")
    setfacl(config.VAR_DIR, f"g:{g}:--x")
    setfacl(events, f"g:{g}:-wx")


def events_dir() -> Path:
    if not config.VAR_DIR.exists():
        config.VAR_DIR.mkdir(parents=True)
        os.chmod(config.VAR_DIR, 0o2770)
    d = config.VAR_DIR / "events"
    if not d.exists():
        d.mkdir()
        os.chmod(d, 0o3770)  # setgid (BSR group) + sticky (can't delete others' files)
    return d


# ---- personal folders --------------------------------------------------------------

def secure_personal(personal: Path, pis: list[str], owner: str):
    """Owner + PIs only. Called by the owner's own process (only an owner may set ACLs)."""
    strip(personal, recursive=True)
    for root, dirs, _ in os.walk(personal):
        os.chmod(root, 0o2700)
    pis = [p for p in pis if p != owner]
    for pi in pis:
        try:
            u = _uid(pi)
        except KeyError:
            continue
        setfacl(personal, f"u:{u}:rwX", "-R")
        for root, _, _ in os.walk(personal):
            setfacl(Path(root), f"d:u:{u}:rwx,d:u::rwx,d:g::---,d:o::---")


def personal_is_private(personal: Path, pis: list[str]) -> list[str]:
    """Problems with a personal folder's permissions (empty list = fine)."""
    problems = []
    mode = personal.stat().st_mode
    if mode & 0o007:
        problems.append(f"{personal} is open to all cluster users")
    if not owning_group_entry(personal).startswith("group::---"):
        problems.append(f"{personal} is open to its group ({owning_group_entry(personal)})")
    allowed = {f"user:{_uid(p)}:" for p in pis if _safe_uid(p) is not None}
    for e in entries(personal):
        if e.startswith("default:"):
            e = e[len("default:"):]
        if e.startswith("group:") or not any(e.startswith(a) for a in allowed):
            problems.append(f"{personal} grants extra access: {e}")
    return problems


def _safe_uid(user):
    try:
        return _uid(user)
    except KeyError:
        return None
