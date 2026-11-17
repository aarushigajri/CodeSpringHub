"""File permissions that make one shared Hub folder safe for several labs.

Unix groups alone can't express "this lab AND BSR" or "this member AND their PI", so the Hub
adds ACLs. /grid is GPFS with NFSv4 ACL semantics: `setfacl` doesn't work there, so ACLs are
set with GPFS's own `mmputacl` / `mmgetacl` (any file owner may run them). Elsewhere (local
disks, the test suite's temporary folders) POSIX ACLs and `setfacl` are used. Both give the same
result; `entries()` reports either kind in one numeric form, e.g. {'group:1234:rwx',
'default:group:1234:rwx'} ("default:" = inherited by new files).

    knowledge/                 group BSR rwx; each pilot lab group r-x (list labs, read workflows)
    knowledge/<lab>/           group BSR rwx + lab group rwx, inherited by everything inside
    knowledge/<lab>/members/<user>/
                               PERSONAL: owner + each PI of the lab; the lab folder's access is
                               removed so BSR and other members can't get in
    knowledge/<lab>/private/<user>/
                               PRIVATE: owner only (not BSR, not the PI)
    knowledge/<core>/          a core facility: its staff group rwx; every Hub group r-x on the folder
                               and lab.yaml only (nothing inherited)
    knowledge/<core>/shared/   SHARED: staff rwx, every Hub group (labs, cores, BSR) r-x, inherited
    knowledge/<core>/internal/ INTERNAL: the core's staff only, inherited (no BSR, no labs)
    var/                       BSR only; each pilot lab group --x (to reach var/events)
    var/events/                BSR rwx; each pilot lab group -wx (create own file, can't list);
                               sticky, so nobody can delete or rename someone else's log

GPFS caveat: `chmod` REPLACES an NFSv4 ACL with one made from the mode bits. That always fails
closed (extra access disappears, nothing is opened up), and `hub check` reports it. The Hub's own
code therefore never chmods inside an ACL-managed folder (see `inherits`), and git runs without
core.sharedRepository there. Numeric uid/gid are used because CSHL group names can contain spaces.
"""

import grp
import os
import pwd
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from . import config

MMGETACL = "/usr/lpp/mmfs/bin/mmgetacl"
MMPUTACL = "/usr/lpp/mmfs/bin/mmputacl"


class ACLError(RuntimeError):
    pass


def _gid(group: str) -> int:
    return grp.getgrnam(group).gr_gid


def _uid(user: str) -> int:
    return pwd.getpwnam(user).pw_uid


def _safe_uid(user):
    try:
        return _uid(user)
    except KeyError:
        return None


# ---- which kind of ACL --------------------------------------------------------------

def _mm(path: Path) -> list[str] | None:
    """GPFS NFSv4 ACL lines for path, or None when path isn't on an NFSv4-ACL GPFS."""
    if not (os.path.exists(MMGETACL) or shutil.which("mmgetacl")):
        return None
    r = subprocess.run([MMGETACL, "-k", "nfs4", str(path)], capture_output=True, text=True)
    if r.returncode or not r.stdout.startswith("#NFSv4 ACL"):
        return None
    return r.stdout.splitlines()


@lru_cache(maxsize=64)
def _nfs4_mount(st_dev: int, probe: str) -> bool:
    return _mm(Path(probe)) is not None


def is_nfs4(path: Path) -> bool:
    path = Path(path)
    while not path.exists():
        path = path.parent
    return _nfs4_mount(path.stat().st_dev, str(path))


# ---- NFSv4 (GPFS) ----------------------------------------------------------------------

_FLAGS = ["READ/LIST", "WRITE/CREATE", "APPEND/MKDIR", "SYNCHRONIZE", "READ_ACL", "READ_ATTR", "READ_NAMED",
          "DELETE", "DELETE_CHILD", "CHOWN", "EXEC/SEARCH", "WRITE_ACL", "WRITE_ATTR", "WRITE_NAMED"]


def _nfs4_entry(who: str, perm: str, inherit: bool) -> str:
    """One mmputacl entry. perm uses r, w, x, a (add only) and c (owner's control: chown, ACL)."""
    on = {"SYNCHRONIZE", "READ_ACL", "READ_ATTR", "READ_NAMED"}
    if "r" in perm:
        on.add("READ/LIST")
    if "w" in perm:
        on |= {"WRITE/CREATE", "APPEND/MKDIR", "DELETE_CHILD"}
    if "a" in perm:  # add only: create entries but not delete or rename other people's
        on |= {"WRITE/CREATE", "APPEND/MKDIR"}
    if "x" in perm:
        on.add("EXEC/SEARCH")
    if "c" in perm:
        on |= {"CHOWN", "WRITE_ACL", "WRITE_ATTR", "WRITE_NAMED"}
    summary = "".join(ch if ch in perm.replace("a", "w") else "-" for ch in "rwxc")
    head = f"{who}:{summary}:allow" + (":FileInherit:DirInherit" if inherit else "")
    cells = [f"({'X' if f in on else '-'}){f}" for f in _FLAGS]
    return f"{head}\n {' '.join(cells[:7])}\n {' '.join(cells[7:])}\n"


def _nfs4_parse(lines: list[str]) -> list[tuple[str, str, bool]]:
    """[(who, 'rwx'-style perms, inheritable)] for the allow entries, e.g. ('group:1234', 'rwx', True)."""
    out = []
    for line in lines:
        if not line or line.startswith(("#", " ")):
            continue
        parts = line.split(":")
        if parts[0] == "special":
            who, rest = f"special:{parts[1]}", parts[2:]
        else:
            name = parts[1]
            try:
                num = _gid(name) if parts[0] == "group" else _uid(name)
            except KeyError:
                num = name  # already numeric, or unknown
            who, rest = f"{parts[0]}:{num}", parts[2:]
        if len(rest) < 2 or rest[1] != "allow":
            continue
        perm = "".join(ch for ch in rest[0][:3] if ch != "-")
        perm = "".join(ch if ch in perm else "-" for ch in "rwx")
        out.append((who, perm, "FileInherit" in rest[2:] or "DirInherit" in rest[2:]))
    return out


def _nfs4_put(path: Path, entries: list[tuple[str, str, bool]]):
    is_dir = path.is_dir()
    text = "#NFSv4 ACL\n" + "\n".join(
        _nfs4_entry(who, perm if is_dir else perm.replace("x", ""), inherit and is_dir)
        for who, perm, inherit in entries)
    r = subprocess.run([MMPUTACL, str(path)], input=text, capture_output=True, text=True)
    if r.returncode:
        raise ACLError(f"mmputacl on {path}: {r.stderr.strip()}")


def _nfs4_current(path: Path) -> list[tuple[str, str, bool]]:
    lines = _mm(path)
    if lines is None:
        raise ACLError(f"couldn't read the ACL of {path}")
    return _nfs4_parse(lines)


def _nfs4_add(path: Path, who: str, perm: str, inherit: bool):
    """Add (or replace) one entry, keeping the rest."""
    kept = [e for e in _nfs4_current(path) if e[0] != who]
    _nfs4_put(path, kept + [(who, perm, inherit)])


def _walk(root: Path):
    yield root
    for d, dirs, files in os.walk(root):
        for name in dirs + files:
            yield Path(d) / name


# ---- POSIX (setfacl) -------------------------------------------------------------------

def setfacl(path: Path, spec: str, *flags: str):
    r = subprocess.run(["setfacl", *flags, "-m", spec, str(path)], capture_output=True, text=True)
    if r.returncode:
        raise ACLError(f"setfacl {spec} on {path}: {r.stderr.strip()}")


def strip(path: Path, recursive=False):
    r = subprocess.run(["setfacl", *(["-R"] if recursive else []), "-b", str(path)], capture_output=True, text=True)
    if r.returncode:
        raise ACLError(f"setfacl -b on {path}: {r.stderr.strip()}")


def _posix_lines(path: Path) -> list[str]:
    r = subprocess.run(["getfacl", "-cpn", str(path)], capture_output=True, text=True)
    if r.returncode:
        raise ACLError(r.stderr.strip())
    return [ln.split("#")[0].strip() for ln in r.stdout.splitlines()]


# ---- reading (both kinds) ----------------------------------------------------------------

def owning_group_entry(path: Path) -> str:
    """The real permission of the folder's own group, as 'group::rwx' / 'group::---'."""
    if is_nfs4(path):
        perm = next((p for who, p, _ in _nfs4_current(path) if who == "special:group@"), "---")
        return f"group::{perm}"
    return next((ln for ln in _posix_lines(path) if ln.startswith("group::")), "")


def entries(path: Path) -> set[str]:
    """Named ACL entries, numeric, e.g. {'group:1234:rwx', 'default:group:1234:rwx'}."""
    out = set()
    if is_nfs4(path):
        for who, perm, inherit in _nfs4_current(path):
            if who.startswith("special:"):
                continue
            out.add(f"{who}:{perm}")
            if inherit:
                out.add(f"default:{who}:{perm}")
        return out
    for line in _posix_lines(path):
        parts = line.split(":")
        named = (len(parts) == 3 and parts[1]) or (len(parts) == 4 and parts[0] == "default" and parts[2])
        if line and named:
            out.add(line)
    return out


def inherits(folder: Path) -> bool:
    """True when new files in this folder get their access from an inherited NFSv4 ACL. Then
    nothing may chmod them: on GPFS that would replace the inherited ACL."""
    try:
        return is_nfs4(folder) and any(inh for _, _, inh in _nfs4_current(folder))
    except ACLError:
        return False


# ---- labs ------------------------------------------------------------------------

def grant_lab(lab_dir: Path, unix_group: str):
    """Lab folder: BSR (owning group) and the lab group get rwx, now and for new files.
    Personal and private folders inside keep their own, narrower access."""
    g = _gid(unix_group)
    os.chmod(lab_dir, 0o2770)  # before any ACL: on GPFS a chmod would replace it
    for sub in ("members", "private"):
        (lab_dir / sub).mkdir(exist_ok=True)
        os.chmod(lab_dir / sub, 0o3770)  # sticky: nobody removes or renames someone else's folder
    if not is_nfs4(lab_dir):
        setfacl(lab_dir, f"g:{g}:rwx,d:g:{g}:rwx,d:g::rwx,d:u::rwx,d:o::---,m::rwx,d:m::rwx", "-R")
        return
    lab_entries = [("special:owner@", "rwxc", True), ("special:group@", "rwx", True), (f"group:{g}", "rwx", True)]
    skip = {lab_dir / "members", lab_dir / "private"}
    for p in _walk(lab_dir):
        if any(p == s or s in p.parents for s in skip):
            if p in skip:  # the folder itself, not its contents: add your own folder, touch nobody else's
                _nfs4_put(p, [("special:owner@", "rwxc", False), ("special:group@", "rax", False),
                              (f"group:{g}", "rax", False)])
            continue
        if p.stat().st_uid == os.getuid():  # only an owner may set an ACL
            _nfs4_put(p, lab_entries)


# ---- core facilities ------------------------------------------------------------------

def _mine(p: Path) -> bool:
    return p.stat().st_uid == os.getuid()  # only an owner may set an ACL


def grant_core(core_dir: Path, staff_group: str | None, reader_groups: list[str]):
    """A core's folders. staff_group None = not on the Hub yet: BSR keeps it (staff = BSR's group).
    reader_groups: every lab and core group, who may read lab.yaml and the shared section."""
    staff = _gid(staff_group or config.BSR_GROUP)
    readers = set()
    for g in reader_groups:
        try:
            readers.add(_gid(g))
        except KeyError:
            continue  # a lab whose group was removed; `hub check` reports it
    readers = sorted(readers - {staff})
    bsr_keeps = staff_group is None
    shared, internal = core_dir / "shared", core_dir / "internal"
    for d in (shared, internal, core_dir / "members", core_dir / "private"):
        d.mkdir(exist_ok=True)
    os.chmod(core_dir, 0o2750)
    os.chmod(shared, 0o2770)
    os.chmod(internal, 0o2770 if bsr_keeps else 0o2700)
    for sub in ("members", "private"):
        os.chmod(core_dir / sub, 0o3770 if bsr_keeps else 0o3700)
    if not is_nfs4(core_dir):
        read = ",".join(f"g:{g}:r-x" for g in readers)
        top = f"g:{staff}:rwx" + (f",{read}" if read else "")
        setfacl(core_dir, top)
        if (core_dir / "lab.yaml").exists():
            setfacl(core_dir / "lab.yaml", top.replace("r-x", "r--").replace("rwx", "rw-"))
        dflt = f"g:{staff}:rwx,d:g:{staff}:rwx,d:u::rwx,d:o::---,m::rwx,d:m::rwx" + "".join(
            f",g:{g}:r-x,d:g:{g}:r-x" for g in readers)
        setfacl(shared, dflt + ",d:g::rwx", "-R")
        strip(internal, recursive=True)
        setfacl(internal, f"g::{'rwx' if bsr_keeps else '---'},o::---,g:{staff}:rwx,d:g:{staff}:rwx,d:u::rwx,"
                          f"d:g::{'rwx' if bsr_keeps else '---'},d:o::---,m::rwx,d:m::rwx", "-R")
        for sub in ("members", "private"):
            setfacl(core_dir / sub, f"g:{staff}:rwx")
        return
    bsr = "r-x"
    top = [("special:owner@", "rwxc", False), ("special:group@", bsr if not bsr_keeps else "rwx", False),
           (f"group:{staff}", "rwx", False)] + [(f"group:{g}", "r-x", False) for g in readers]
    if _mine(core_dir):
        _nfs4_put(core_dir, top)
    if (core_dir / "lab.yaml").exists() and _mine(core_dir / "lab.yaml"):
        _nfs4_put(core_dir / "lab.yaml", top)
    shared_entries = [("special:owner@", "rwxc", True), ("special:group@", "rwx" if bsr_keeps else "r-x", True),
                      (f"group:{staff}", "rwx", True)] + [(f"group:{g}", "r-x", True) for g in readers]
    for p in _walk(shared):
        if _mine(p):
            _nfs4_put(p, shared_entries)
    internal_entries = [("special:owner@", "rwxc", True), ("special:group@", "rwx" if bsr_keeps else "---", True),
                        (f"group:{staff}", "rwx", True)]
    for p in _walk(internal):
        if _mine(p):
            _nfs4_put(p, internal_entries)
    for sub in ("members", "private"):
        if _mine(core_dir / sub):
            _nfs4_put(core_dir / sub, [("special:owner@", "rwxc", False),
                                       ("special:group@", "rax" if bsr_keeps else "---", False),
                                       (f"group:{staff}", "rax", False)])


def grant_core_reader(core_dir: Path, group: str):
    """A newly added lab (or core) may read an existing core's lab.yaml and shared section."""
    g = _gid(group)
    shared = core_dir / "shared"
    if not is_nfs4(core_dir):
        setfacl(core_dir, f"g:{g}:r-x")
        if (core_dir / "lab.yaml").exists():
            setfacl(core_dir / "lab.yaml", f"g:{g}:r--")
        if shared.exists():
            setfacl(shared, f"g:{g}:r-x,d:g:{g}:r-x", "-R")
        return
    for p in (core_dir, core_dir / "lab.yaml"):
        if p.exists() and _mine(p):
            _nfs4_add(p, f"group:{g}", "r-x", False)
    if shared.exists():
        for p in _walk(shared):
            if _mine(p):
                _nfs4_add(p, f"group:{g}", "r-x", True)


def core_problems(core_dir: Path, staff_group: str | None) -> list[str]:
    """What `hub check` reports for a core: an internal section anyone but its staff can read."""
    problems = []
    internal = core_dir / "internal"
    if not internal.exists() or staff_group is None:
        return problems
    staff = _gid(staff_group)
    for p in [internal, *[q for q in internal.rglob("*")][:500]]:
        try:
            if p.stat().st_mode & 0o007:
                problems.append(f"{p} is open to all cluster users")
            for e in entries(p):
                e2 = e[len("default:"):] if e.startswith("default:") else e
                if e2.startswith("group:") and not e2.startswith(f"group:{staff}:"):
                    problems.append(f"{p} grants a group other than the core's staff: {e2}")
        except OSError:
            continue
    return problems


def grant_lab_paths(unix_group: str):
    """What a lab's members need outside their folder: list labs, reach var/events."""
    g = _gid(unix_group)
    events = events_dir()
    if not is_nfs4(config.KNOWLEDGE_DIR):
        setfacl(config.KNOWLEDGE_DIR, f"g:{g}:r-x")
        setfacl(config.VAR_DIR, f"g:{g}:--x")
        setfacl(events, f"g:{g}:-wx")
        return
    _nfs4_add(config.KNOWLEDGE_DIR, f"group:{g}", "r-x", False)
    _nfs4_add(config.VAR_DIR, f"group:{g}", "--x", False)
    _nfs4_add(events, f"group:{g}", "-wx", False)


def events_dir() -> Path:
    if not config.VAR_DIR.exists():
        config.VAR_DIR.mkdir(parents=True)
        os.chmod(config.VAR_DIR, 0o2770)
    d = config.VAR_DIR / "events"
    if not d.exists():
        d.mkdir()
        os.chmod(d, 0o3770)  # setgid (BSR group) + sticky (can't delete others' files)
    return d


# ---- personal and private folders ----------------------------------------------------------

def _readers(pis: list[str], owner: str) -> list[int]:
    return [u for u in (_safe_uid(p) for p in pis if p != owner) if u is not None]


def secure_personal(personal: Path, pis: list[str], owner: str):
    """Owner + PIs only. Called by the owner's own process (only an owner may set ACLs)."""
    readers = _readers(pis, owner)
    if not is_nfs4(personal):
        strip(personal, recursive=True)
        for root, dirs, _ in os.walk(personal):
            os.chmod(root, 0o2700)
        for u in readers:
            setfacl(personal, f"u:{u}:rwX", "-R")
            for root, _, _ in os.walk(personal):
                setfacl(Path(root), f"d:u:{u}:rwx,d:u::rwx,d:g::---,d:o::---")
        return
    wanted = [("special:owner@", "rwxc", True)] + [(f"user:{u}", "rwx", True) for u in readers]
    for p in _walk(personal):
        if p.stat().st_uid == os.getuid():
            _nfs4_put(p, wanted)


def secure_private(folder: Path):
    """Owner only: no PI, no BSR, no lab."""
    if not is_nfs4(folder):
        strip(folder, recursive=True)
        for root, dirs, files in os.walk(folder):
            os.chmod(root, 0o2700)
            for f in files:
                os.chmod(Path(root) / f, 0o600)
        return
    for p in _walk(folder):
        if p.stat().st_uid == os.getuid():
            _nfs4_put(p, [("special:owner@", "rwxc", True)])


def personal_is_private(personal: Path, pis: list[str]) -> list[str]:
    """Problems with a personal (or, with pis=[], private) folder's permissions. Empty = fine."""
    problems = []
    if is_nfs4(personal):
        for who, perm, _ in _nfs4_current(personal):
            if who == "special:everyone@" and perm != "---":
                problems.append(f"{personal} is open to all cluster users")
            if who == "special:group@" and perm != "---":
                problems.append(f"{personal} is open to its group (group@ {perm})")
    else:
        if personal.stat().st_mode & 0o007:
            problems.append(f"{personal} is open to all cluster users")
        if not owning_group_entry(personal).startswith("group::---"):
            problems.append(f"{personal} is open to its group ({owning_group_entry(personal)})")
    allowed = {f"user:{u}:" for u in _readers(pis, "")}
    for e in entries(personal):
        if e.startswith("default:"):
            e = e[len("default:"):]
        if e.startswith("group:") or not any(e.startswith(a) for a in allowed):
            problems.append(f"{personal} grants extra access: {e}")
    return problems
