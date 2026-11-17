"""The Hub's write operations, used by the web portal (never exposed to Claude).

Every change:
  - checks the user may make it (lab-shared: lab members and BSR; personal/private: only the owner),
  - validates input and explains problems in plain language,
  - writes the file atomically (a crash never leaves half a file),
  - records a local git commit in the right history (the lab's own, or the person's own),
  - writes a usage-log row (details withheld for personal and private items).
"""

import json
import os
import pwd
import re
import socket
import subprocess
import tempfile
import threading
from datetime import date
from pathlib import Path

import yaml

from . import acl, config, knowledge, store
from .errors import Forbidden, HubError, NotFound
from .permissions import (can_access_lab, can_edit_area, can_edit_lab, can_see, display_name, is_bsr, is_member, require_lab,
                          require_lab_editor, require_member, require_visible_lab)

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
USERNAME = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
CATEGORIES = {
    "guidance": "Guidance: how BSR/the lab wants a kind of analysis done",
    "protocols": "Protocol / SOP: step-by-step lab or analysis procedure",
    "notes": "Note: background, decisions, things worth remembering",
    "grants": "Grant / project description: aims and scientific background",
}
ASSAYS = ["rna-seq", "scrna-seq", "atac-seq", "chip-seq", "wgs", "wes", "dna-seq", "methylation",
          "proteomics", "imaging", "other"]
KINDS = ("data", "code")
SCOPES = ("lab", "shared", "personal", "private")  # private = only the owner, not their PI
WHO_CAN_SEE = ("Choose who can see it: the lab, you and your PI, or only you. In a core facility: everyone "
               "(shared), the core's staff, you and the core's lead, or only you.")


def _area_for(lab, scope: str) -> str:
    """Where a lab-level item of this scope lives: '' (a lab's top folder) or a core's section."""
    if scope == "shared" and not lab.is_core:
        raise HubError("Only core facilities have a shared section; in a lab, choose the lab, you and your PI, or only you.")
    if not lab.is_core:
        return ""
    return knowledge.SHARED_PART if scope == "shared" else knowledge.INTERNAL_PART
YAML_HEADER = "# Managed by the CodeSpringHub portal. Hand edits are fine; keep the structure.\n"

_git_lock = threading.Lock()


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:63] or "untitled"


# ---- files, history, audit ---------------------------------------------------------

def _write_atomic(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o660
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    if not acl.inherits(path.parent):  # on GPFS the inherited ACL already fits; chmod would replace it
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def _repo_for(path: Path) -> Path | None:
    """The nearest git repository holding this file: a person's own, the lab's own, or (for BSR's
    knowledge and the workflow list) the main Hub repository."""
    for d in [path.parent, *path.parent.parents]:
        if (d / ".git").exists():
            return d
        if d == config.HUB_HOME:
            break
    return None


def _git(repo: Path, *args, env=None, check=True):
    """safe.directory: git refuses repos owned by another user unless told otherwise.
    core.sharedRepository=group: new git objects stay writable for whoever shares the folder. Not in
    a folder with inherited GPFS ACLs: there git's chmod would replace them, and inheritance already
    gives the right people access."""
    shared = [] if acl.inherits(repo) else ["-c", "core.sharedRepository=group"]
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", *shared, "-C", str(repo), *args],
        check=check, capture_output=True, text=True, env=env,
    )


def init_history(folder: Path, ignore: list[str]):
    if (folder / ".git").exists():
        return
    _git(folder, "init", "-q")
    (folder / ".gitignore").write_text("".join(f"{i}\n" for i in ignore))


def _commit(user: str, paths: list[Path], message: str):
    """A local commit for just these paths. Never pushes. A git problem never blocks a save:
    the file is already written, so it is logged and the save carries on."""
    repo = _repo_for(paths[0])
    if repo is None:
        return
    rel = [str(p.relative_to(repo)) for p in paths]
    name, email = display_name(user), f"{user}@cshl.edu"
    # Set the committer too: a portal user may never have configured git.
    env = os.environ | {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    with _git_lock:
        try:
            _git(repo, "add", "-A", "--", *rel, env=env)
            if _git(repo, "diff", "--cached", "--quiet", "--", *rel, check=False).returncode == 0:
                return  # nothing changed
            _git(repo, "commit", "-q", "-m", f"portal: {message}", "--", *rel, env=env)
        except subprocess.CalledProcessError as e:
            personal = "/members/" in str(paths[0]) or "/private/" in str(paths[0])
            store.log_call(user, "portal:history_not_recorded", {"paths": rel}, False, 0,
                           (e.stderr or str(e)).strip()[:500], personal=personal)


def _audit(user, action, details, lab_id=None, personal=False):
    try:
        store.log_call(user, f"portal:{action}", details, True, 0, lab_id=lab_id, personal=personal)
    except Exception:  # noqa: BLE001 - logging never blocks an edit
        pass


def history(user, lab_id, rel_path=None, limit=20) -> list[dict]:
    labs = knowledge.load_labs()
    # A core's shared documents have their own history (shared/.git), readable by everyone; the rest
    # is for the lab's members or the core's staff.
    lab = require_visible_lab(user, labs, lab_id) if rel_path else require_lab(user, labs, lab_id)
    target = lab.dir / rel_path if rel_path else lab.dir / "lab.yaml"
    if rel_path:
        _check_doc_visible(user, lab, rel_path)
    repo = _repo_for(target)
    if repo is None:
        return []
    where = str(target.relative_to(repo)) if rel_path else "."
    out = _git(repo, "log", f"-{limit}", "--format=%h%x09%an%x09%ad%x09%s", "--date=format:%Y-%m-%d %H:%M",
               "--", where, check=False).stdout
    rows = [line.split("\t", 3) for line in out.splitlines() if line]
    return [{"commit": c, "author": a, "date": d, "message": m} for c, a, d, m in rows]


# ---- personal and private folders --------------------------------------------------------

def _own_folder(lab, user, private: bool) -> Path:
    return knowledge.private_dir(lab, user) if private else knowledge.personal_dir(lab, user)


def secure_own(lab, user, private: bool):
    folder = _own_folder(lab, user, private)
    if private:
        acl.secure_private(folder)
    else:
        acl.secure_personal(folder, lab.pis, user)


def ensure_personal(user, lab, private: bool = False) -> Path:
    """Create (or re-secure) the user's own folder. Personal: owner + the lab's PIs, never BSR.
    Private: owner only. Runs before every such write, so PI changes take effect on the owner's
    next save. Each folder has its own history, so the PI can't read private files through git."""
    require_member(user, {lab.id: lab}, lab.id)
    (lab.dir / ("private" if private else "members")).mkdir(exist_ok=True)
    folder = _own_folder(lab, user, private)
    folder.mkdir(exist_ok=True)
    try:
        secure_own(lab, user, private)
    except (acl.ACLError, OSError) as e:
        raise HubError(f"Couldn't lock down your {'private' if private else 'personal'} folder, so nothing "
                       f"was saved. Please tell BSR. ({e})")
    init_history(folder, ["_suggestions/"])
    return folder


# ---- lab details ---------------------------------------------------------------

def update_lab(user, lab_id, name: str, description: str, projects: list[dict]):
    lab = require_lab_editor(user, knowledge.load_labs(), lab_id)
    if not name.strip():
        raise HubError("The lab needs a name.")
    seen = set()
    clean = []
    for p in projects:
        pid = p.get("id") or slugify(p.get("name", ""))
        if not SLUG.match(pid):
            raise HubError(f"Project ID '{pid}' should use lowercase letters, numbers and dashes.")
        if pid in seen:
            raise HubError(f"Two projects share the ID '{pid}'.")
        seen.add(pid)
        clean.append({"id": pid, "name": p.get("name", "").strip() or pid, "description": p.get("description", "").strip()})
    path = lab.dir / "lab.yaml"
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}  # keep kind, website, contacts and anything else BSR set
    data.update({"name": name.strip(), "unix_group": lab.unix_group or data.get("unix_group", ""),
                 "description": description.strip(), "projects": clean})
    _write_atomic(path, YAML_HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    _commit(user, [path], f"update {lab_id} lab details")
    _audit(user, "update_lab", {"lab_id": lab_id}, lab_id=lab_id)


# ---- documents -----------------------------------------------------------------

def _parse_doc_path(rel_path: str, lab=None) -> tuple[str | None, str, bool]:
    """'guidance/x.md' -> (None, ..., False); 'members/ann/notes/x.md' -> ('ann', 'notes/x.md', False);
    'private/ann/notes/x.md' -> ('ann', 'notes/x.md', True). In a core, lab-level documents live in
    'shared/...' or 'internal/...' (use _doc_area for which)."""
    parts = Path(rel_path).parts
    if len(parts) == 4 and parts[0] in ("members", "private"):
        owner, inner = parts[1], "/".join(parts[2:])
        if not USERNAME.match(owner):
            raise HubError("Unknown personal folder.")
        return owner, inner, parts[0] == "private"
    if parts and parts[0] in ("members", "private"):
        raise HubError("Unknown personal folder.")
    if lab is not None and lab.is_core:
        if len(parts) < 2 or parts[0] not in (knowledge.SHARED_PART, knowledge.INTERNAL_PART):
            raise HubError("A core's documents live in its shared/ or internal/ section.")
        return None, "/".join(parts[1:]), False
    return None, rel_path, False


def _doc_area(lab, rel_path: str) -> str:
    parts = Path(rel_path).parts
    return parts[0] if lab.is_core and parts and parts[0] in (knowledge.SHARED_PART, knowledge.INTERNAL_PART) else ""


def _check_inner(inner: str, owner: str | None):
    parts = Path(inner).parts
    ok = (
        (inner == "context.md" and owner is None)
        or (len(parts) == 2 and parts[0] in CATEGORIES and parts[1].endswith(".md") and SLUG.match(parts[1][:-3]))
    )
    if not ok:
        raise HubError("Documents live at context.md or <category>/<name>.md (lowercase, dashes).")


def _check_doc_visible(user, lab, rel_path) -> tuple[str | None, bool]:
    owner, inner, private = _parse_doc_path(rel_path, lab)
    _check_inner(inner, owner)
    if not can_see(user, lab, owner, private, _doc_area(lab, rel_path) == knowledge.SHARED_PART):
        raise NotFound("That document doesn't exist (it may have been deleted).")
    return owner, private


def read_document_file(user, lab_id, rel_path) -> dict:
    lab = require_visible_lab(user, knowledge.load_labs(), lab_id)
    owner, private = _check_doc_visible(user, lab, rel_path)
    path = lab.dir / rel_path
    if not path.exists():
        raise NotFound("That document doesn't exist (it may have been deleted).")
    can_edit = os.access(path, os.W_OK) and (owner == user if owner else can_edit_lab(user, lab))
    shared = _doc_area(lab, rel_path) == knowledge.SHARED_PART
    return {"rel_path": rel_path, "text": path.read_text(), "can_edit": can_edit,
            "scope": "private" if private else "personal" if owner else "shared" if shared else "lab", "owner": owner}


def _require_area(user, lab, area: str):
    """A core's internal section: its staff only (BSR may edit a starter page's shared section only)."""
    if not can_edit_area(user, lab, area):
        raise NotFound("That document doesn't exist (it may have been deleted).")


def _writable_doc(user, lab_id, rel_path):
    labs = knowledge.load_labs()
    owner, inner, private = _parse_doc_path(rel_path, labs.get(lab_id))
    _check_inner(inner, owner)
    if owner is None:
        lab = require_lab_editor(user, labs, lab_id)
        _require_area(user, lab, _doc_area(lab, rel_path))
        return lab, lab.dir / rel_path, None, False
    lab = require_lab(user, labs, lab_id)
    if owner != user:
        raise Forbidden("Only the owner can change a personal document.")
    folder = ensure_personal(user, lab, private)
    return lab, folder / inner, owner, private


def save_document(user, lab_id, rel_path, text: str):
    lab, path, owner, private = _writable_doc(user, lab_id, rel_path)
    if len(text) > 200_000:
        raise HubError("That document is very long (over 200,000 characters). Split it into several documents.")
    if not path.parent.exists():
        path.parent.mkdir(parents=True)  # parents: a core's internal/ section may not exist yet
        if owner and not acl.inherits(path.parent.parent):
            os.chmod(path.parent, 0o2700)
    _write_atomic(path, text if text.endswith("\n") else text + "\n")
    if owner:
        secure_own(lab, user, private)
    _commit(user, [path], "edit personal document" if owner else f"edit {lab_id}/{rel_path}")
    _audit(user, "save_document", {"lab_id": lab_id, "rel_path": rel_path}, lab_id=lab_id, personal=bool(owner))


def create_document(user, lab_id, category: str, title: str, scope: str = "lab") -> str:
    if scope not in SCOPES:
        raise HubError(WHO_CAN_SEE)
    if category not in CATEGORIES:
        raise HubError(f"Pick one of: {', '.join(CATEGORIES)}.")
    if not title.strip():
        raise HubError("Give the document a title.")
    inner = f"{category}/{slugify(title)}.md"
    lab = require_visible_lab(user, knowledge.load_labs(), lab_id)
    area = "" if scope in ("personal", "private") else _area_for(lab, scope)
    rel_path = {"personal": f"members/{user}/{inner}", "private": f"private/{user}/{inner}"}.get(
        scope, f"{area}/{inner}" if area else inner)
    if (lab.dir / rel_path).exists():
        raise HubError("A document with a very similar title already exists. Open that one, or choose another title.")
    template = (
        f"# {title.strip()}\n\n## Summary\nTODO: one or two sentences.\n\n"
        "## Details\nTODO\n\n## When to involve BSR\nTODO\n"
    )
    save_document(user, lab_id, rel_path, template)
    return rel_path


def delete_document(user, lab_id, rel_path):
    if Path(rel_path).name == "context.md" and len(Path(rel_path).parts) <= 2 and not rel_path.startswith(("members/", "private/")):
        raise HubError("The lab context page can't be deleted; clear its text instead.")
    lab, path, owner, private = _writable_doc(user, lab_id, rel_path)
    if not path.exists():
        raise NotFound("That document doesn't exist.")
    path.unlink()
    _commit(user, [path], f"delete {'personal document' if owner else lab_id + '/' + rel_path}")
    _audit(user, "delete_document", {"lab_id": lab_id, "rel_path": rel_path}, lab_id=lab_id, personal=bool(owner))


# ---- datasets and code ---------------------------------------------------------------

def _datasets_file(user, lab, scope) -> Path:
    if scope in ("personal", "private"):
        return ensure_personal(user, lab, scope == "private") / "datasets.yaml"
    area = _area_for(lab, scope)
    folder = lab.dir / area if area else lab.dir
    folder.mkdir(exist_ok=True)
    return folder / "datasets.yaml"


def _load_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as fh:
        return (yaml.safe_load(fh) or {}).get("datasets", [])


def check_dataset_path(user, path_str: str) -> dict:
    """Plain-language checks on a folder/file before it is registered."""
    if not path_str or not os.path.isabs(path_str):
        raise HubError("Choose a folder from the browser (it should start with /grid/).")
    real = os.path.realpath(path_str)
    if not any(real == r or real.startswith(r + os.sep) for r in config.BROWSE_ROOTS):
        raise HubError(f"Only data under {', '.join(config.BROWSE_ROOTS)} can be registered.")
    hub_home = str(config.HUB_HOME)
    inside_hub = real == hub_home or real.startswith(hub_home + os.sep)
    if inside_hub and not real.startswith(str(config.HUB_HOME / "demo_data")):
        raise HubError("That folder is part of the Hub itself and can't be registered as data.")
    if not os.path.exists(real):
        raise HubError("That folder doesn't exist (it may have been moved or deleted).")
    if not os.access(real, os.R_OK):
        raise Forbidden("You don't have permission to read that folder on the cluster.")
    return {"path": real, "is_dir": os.path.isdir(real)}


def _split_dataset_id(user, lab, hub_id: str) -> tuple[str, str]:
    """Hub ID -> (scope, short id), checking the user may change it."""
    owner = knowledge.owner_of(hub_id)
    short = hub_id.rsplit("/", 1)[-1]
    if not hub_id.startswith(f"ds:{lab.id}/"):
        raise NotFound("That dataset isn't registered in this lab.")
    if owner is None:
        require_lab_editor(user, {lab.id: lab}, lab.id)
        scope = "shared" if lab.is_core and hub_id.startswith(f"ds:{lab.id}/{knowledge.SHARED_PART}/") else "lab"
        _require_area(user, lab, knowledge.INTERNAL_PART if lab.is_core and scope == "lab" else "")
        return scope, short
    if owner != user:
        raise Forbidden("Only the owner can change a personal item.")
    return ("private" if knowledge.is_private_id(hub_id) else "personal"), short


def save_dataset(user, lab_id, entry: dict, original_id: str | None = None) -> str:
    """Register (or update) data or code. entry['scope']: 'lab' (all lab members), 'personal'
    (you and your PI) or 'private' (only you)."""
    lab = require_visible_lab(user, knowledge.load_labs(), lab_id)
    if original_id:
        scope, original_short = _split_dataset_id(user, lab, original_id)
    else:
        scope, original_short = entry.get("scope", "lab"), None
        if scope not in SCOPES:
            raise HubError(WHO_CAN_SEE)
        if scope in ("lab", "shared"):
            area = _area_for(lab, scope)
            require_lab_editor(user, {lab.id: lab}, lab.id)
            _require_area(user, lab, area)
    title = (entry.get("title") or "").strip()
    if not title:
        raise HubError("Give it a short title, e.g. 'Mouse WGS, 12 samples' or 'RNA-seq analysis scripts'.")
    ds_id = (entry.get("id") or original_short or slugify(title)).strip()
    if not SLUG.match(ds_id):
        raise HubError("The short ID should use lowercase letters, numbers and dashes.")
    kind = entry.get("kind", "data")
    if kind not in KINDS:
        raise HubError("Choose whether this is data or code.")
    checked = check_dataset_path(user, entry.get("path", ""))
    project = entry.get("project") or None
    if project and project not in {p.id for p in lab.projects}:
        raise HubError(f"'{project}' isn't one of this lab's projects. Add it on the lab page first.")
    sensitivity = entry.get("sensitivity", "standard")
    if sensitivity not in ("standard", "sensitive"):
        raise HubError("Sensitivity must be 'standard' or 'sensitive'.")

    path = _datasets_file(user, lab, scope)
    entries = _load_entries(path)
    existing = next((e for e in entries if e["id"] == (original_short or ds_id)), None)
    if any(e["id"] == ds_id for e in entries if e is not existing):
        raise HubError(f"Something with the ID '{ds_id}' is already registered here.")
    new = {
        "id": ds_id, "title": title, "kind": kind, "project": project, "path": checked["path"],
        "assay": entry.get("assay", ""), "description": (entry.get("description") or "").strip(),
        "sensitivity": sensitivity,
        "registered_by": existing["registered_by"] if existing else user,
        "registered_on": existing["registered_on"] if existing else date.today().isoformat(),
    }
    if existing:
        entries[entries.index(existing)] = new
    else:
        entries.append(new)
    _write_atomic(path, YAML_HEADER + yaml.safe_dump({"datasets": entries}, sort_keys=False, allow_unicode=True))
    mine = scope in ("personal", "private")
    if mine:
        secure_own(lab, user, scope == "private")
    verb = "update" if existing else "register"
    _commit(user, [path], f"{verb} {kind} {f'({scope})' if mine else lab_id + '/' + ds_id}")
    _audit(user, "save_dataset", {"lab_id": lab_id, "dataset": ds_id, "kind": kind}, lab_id=lab_id, personal=mine)
    area = "" if mine else _area_for(lab, scope)
    return {"personal": f"ds:{lab_id}/~{user}/{ds_id}", "private": f"ds:{lab_id}/~{user}/private/{ds_id}"}.get(
        scope, f"ds:{lab_id}/{area}/{ds_id}" if area else f"ds:{lab_id}/{ds_id}")


def delete_dataset(user, lab_id, hub_id):
    lab = require_visible_lab(user, knowledge.load_labs(), lab_id)
    scope, short = _split_dataset_id(user, lab, hub_id)
    path = _datasets_file(user, lab, scope)
    entries = _load_entries(path)
    remaining = [e for e in entries if e["id"] != short]
    if len(remaining) == len(entries):
        raise NotFound("That isn't registered.")
    _write_atomic(path, YAML_HEADER + yaml.safe_dump({"datasets": remaining}, sort_keys=False, allow_unicode=True))
    mine = scope in ("personal", "private")
    _commit(user, [path], f"unregister {f'({scope})' if mine else lab_id + '/' + short}")
    _audit(user, "delete_dataset", {"lab_id": lab_id, "dataset": short}, lab_id=lab_id, personal=mine)


def datasets_for_portal(user, lab_id) -> list[dict]:
    """Like the model's view, plus the folder path (people registered it) and edit rights."""
    lab = require_visible_lab(user, knowledge.load_labs(), lab_id)
    out = []
    for d in knowledge.load_datasets(lab).values():
        if not can_see(user, lab, d.owner, d.private, d.shared):
            continue
        can_edit = (d.owner == user) if d.owner else can_edit_lab(user, lab)
        out.append({
            "id": d.id, "short_id": d.short_id.split("/")[-1], "title": d.title, "kind": d.kind,
            "project": d.project, "assay": d.assay, "description": d.description, "sensitivity": d.sensitivity,
            "scope": knowledge.scope_of(d), "owner": d.owner, "registered_by": d.registered_by,
            "registered_on": d.registered_on, "path": d.path, "can_edit": can_edit,
        })
    return out


def browse(user, path_str: str | None) -> dict:
    """List sub-folders and files the user can read, for the folder picker."""
    roots = config.BROWSE_ROOTS
    if not path_str:
        return {"path": None, "parent": None,
                "folders": [{"name": r, "path": r} for r in roots if os.access(r, os.R_OK | os.X_OK)], "files": []}
    real = os.path.realpath(path_str)
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        raise Forbidden(f"Browsing is limited to {', '.join(roots)}.")
    if not os.path.isdir(real) or not os.access(real, os.R_OK | os.X_OK):
        raise Forbidden("You don't have permission to open that folder.")
    folders, files = [], []
    try:
        with os.scandir(real) as it:
            for e in sorted(it, key=lambda e: e.name.lower()):
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir():
                        if os.access(e.path, os.R_OK | os.X_OK):
                            folders.append({"name": e.name, "path": e.path})
                    elif len(files) < 50:
                        files.append({"name": e.name, "size": e.stat().st_size})
                except OSError:
                    continue
                if len(folders) >= 500:
                    break
    except PermissionError:
        raise Forbidden("You don't have permission to open that folder.") from None
    parent = os.path.dirname(real) if real not in roots else None
    return {"path": real, "parent": parent, "folders": folders, "files": files}


# ---- workflows (BSR only) ------------------------------------------------------

def _require_bsr(user):
    if not is_bsr(user):
        raise Forbidden("Only BSR staff can do this.")


def _workflows_path() -> Path:
    return config.KNOWLEDGE_DIR / "workflows.yaml"


def save_workflow(user, wf: dict, original_id: str | None = None) -> str:
    _require_bsr(user)
    name = (wf.get("name") or "").strip()
    if not name:
        raise HubError("Give the workflow a name, e.g. nf-core/rnaseq.")
    wf_id = (wf.get("id") or original_id or slugify(name)).strip()
    if not SLUG.match(wf_id):
        raise HubError("The workflow ID should use lowercase letters, numbers and dashes.")
    if wf.get("status") not in ("validated", "candidate"):
        raise HubError("Status must be 'validated' (BSR signed off) or 'candidate'.")
    assays = [a.strip().lower() for a in wf.get("assays", []) if a.strip()]
    if not assays:
        raise HubError("Pick at least one assay this workflow is for.")
    entries = knowledge.load_workflows()
    new = {"id": wf_id, "name": name, "assays": assays, "status": wf["status"],
           "version": (wf.get("version") or "TODO").strip(), "url": (wf.get("url") or "").strip(),
           "summary": (wf.get("summary") or "").strip()}
    idx = next((i for i, e in enumerate(entries) if e["id"] == (original_id or wf_id)), None)
    if idx is None and any(e["id"] == wf_id for e in entries):
        raise HubError(f"A workflow with the ID '{wf_id}' already exists.")
    if idx is None:
        entries.append(new)
    else:
        entries[idx] = new
    path = _workflows_path()
    _write_atomic(path, YAML_HEADER + yaml.safe_dump({"workflows": entries}, sort_keys=False, allow_unicode=True))
    _commit(user, [path], f"{'update' if idx is not None else 'add'} workflow {wf_id}")
    _audit(user, "save_workflow", {"workflow": wf_id})
    return wf_id


def delete_workflow(user, wf_id):
    _require_bsr(user)
    entries = knowledge.load_workflows()
    remaining = [e for e in entries if e["id"] != wf_id]
    if len(remaining) == len(entries):
        raise NotFound("That workflow isn't registered.")
    path = _workflows_path()
    _write_atomic(path, YAML_HEADER + yaml.safe_dump({"workflows": remaining}, sort_keys=False, allow_unicode=True))
    _commit(user, [path], f"remove workflow {wf_id}")
    _audit(user, "delete_workflow", {"workflow": wf_id})


# ---- inbox: Claude's suggestions and BSR review tickets -------------------------

def _suggestion_files(user) -> list[tuple[Path, dict, bool]]:
    """(file, record, can_change) for every suggestion this user may see."""
    out = []
    for lab in knowledge.load_labs().values():
        if not can_access_lab(user, lab):
            continue
        folders = [((lab.dir / knowledge.INTERNAL_PART if lab.is_core else lab.dir) / "_suggestions", None, False)]
        folders += [(d / "_suggestions", owner, False) for owner, d in knowledge.member_dirs(lab)]
        folders += [(d / "_suggestions", owner, True) for owner, d in knowledge.private_dirs(lab)]
        for folder, owner, private in folders:
            if not can_see(user, lab, owner, private):
                continue
            try:
                files = sorted(folder.glob("*.yaml"))
            except PermissionError:
                continue
            for f in files:
                try:
                    rec = yaml.safe_load(f.read_text()) or {}
                except OSError:
                    continue
                rec["scope"] = "private" if private else "personal" if owner else "lab"
                can_change = (owner == user) if owner else os.access(f, os.W_OK)
                out.append((f, rec, can_change))
    return out


def inbox(user) -> dict:
    suggestions = [rec | {"can_change": ok} for _, rec, ok in _suggestion_files(user)]
    suggestions.sort(key=lambda r: r.get("ts", ""), reverse=True)
    statuses = store.review_statuses() if is_bsr(user) else {}
    reviews = store.read_events("reviews", only_user=None if is_bsr(user) else user)
    for r in reviews:
        r["key"] = f"{r['user']}:{r['id']}"
        r["status"] = statuses.get((r["user"], r["id"]), "open" if is_bsr(user) else "sent to BSR")
    reviews.sort(key=lambda r: r["ts"], reverse=True)
    return {"suggestions": suggestions, "reviews": reviews}


def set_suggestion_status(user, sid: str, status: str):
    if status not in ("pending", "applied", "dismissed"):
        raise HubError("Unknown status.")
    for f, rec, can_change in _suggestion_files(user):
        if rec.get("id") == sid:
            if not can_change:
                raise Forbidden("You can't change this suggestion.")
            rec.pop("can_change", None)
            scope = rec.pop("scope", "lab")
            rec["status"] = status
            _write_atomic(f, yaml.safe_dump(rec, sort_keys=False, allow_unicode=True))
            _audit(user, "suggestion_status", {"id": sid, "status": status}, lab_id=rec.get("lab_id"),
                   personal=scope != "lab")
            return
    raise NotFound("No such suggestion.")


def set_review_status(user, key: str, status: str):
    if status not in ("open", "in progress", "closed"):
        raise HubError("Unknown status.")
    _require_bsr(user)
    owner, _, rid = key.partition(":")
    if not rid.isdigit():
        raise NotFound("No such review ticket.")
    conn = store.bsr_connect()
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO review_status VALUES (?,?,?,?,?)",
                         (owner, int(rid), status, user, store.now()))
    finally:
        conn.close()
    _audit(user, "review_status", {"ticket": key, "status": status})


def activity(user, limit=200) -> list[dict]:
    """Usage log: BSR sees every lab's (shared by connecting); others see their own."""
    rows = store.read_events("audit", only_user=None if is_bsr(user) else user)
    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows[:limit]


# ---- connecting Claude -----------------------------------------------------------

def _laptop_hostname() -> str:
    """The name laptops on the CSHL network use: prefer <host>.cshl.edu over internal cluster names."""
    short = socket.gethostname().split(".")[0]
    try:
        socket.gethostbyname(f"{short}.cshl.edu")
        return f"{short}.cshl.edu"
    except OSError:
        return socket.getfqdn()


def portal_port(username: str) -> int:
    """Each person's portal gets its own fixed port (from their uid), so the one-line start
    command works and two people's portals on the same login node never collide."""
    # CodeSpringApp uses 20000-40098 (20000 + uid % 20000, plus 99); stay clear of it.
    return 41000 + pwd.getpwnam(username).pw_uid % 20000


def codespringapp_port(username: str) -> int:
    """The first port CodeSpringApp tries for this person (run_codespringweb.sh: 20000 + uid % 20000)."""
    return 20000 + pwd.getpwnam(username).pw_uid % 20000


def _setup_mac(server: dict, target: str, host: str) -> str:
    """A Terminal command that (1) copies the reconnect helper (laptop/connect.py) from the
    cluster to ~/.codespringhub/, and (2) adds CodeSpringHub to Claude Desktop's config through
    that helper, keeping every other setting and a backup copy."""
    helper_src = config.HUB_HOME / "laptop" / "connect.py"
    ssh_cmd = [server["command"], *server["args"]]
    return f"""mkdir -p ~/.codespringhub && scp -q -o BatchMode=yes -o StrictHostKeyChecking=accept-new {target}@{host}:{helper_src} ~/.codespringhub/connect.py && /usr/bin/python3 - <<'EOF'
import json, pathlib, shutil
helper = pathlib.Path.home() / ".codespringhub" / "connect.py"
p = pathlib.Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
p.parent.mkdir(parents=True, exist_ok=True)
c = {{}}
if p.exists():
    shutil.copy(p, p.with_name(p.name + ".bak"))
    c = json.loads(p.read_text() or "{{}}")
ssh = json.loads('{json.dumps(ssh_cmd)}')
c.setdefault("mcpServers", {{}})["codespringhub"] = {{"command": "/usr/bin/python3", "args": [str(helper), "--"] + ssh}}
p.write_text(json.dumps(c, indent=2))
print("Added CodeSpringHub. Now open Claude Desktop.")
EOF"""


def _setup_windows(server: dict) -> str:
    """The same for Windows PowerShell. Written without a byte-order mark, which Claude can't read."""
    return f"""$p = "$env:APPDATA\\Claude\\claude_desktop_config.json"
New-Item -ItemType Directory -Force (Split-Path $p) | Out-Null
$c = [pscustomobject]@{{}}
if (Test-Path $p) {{ Copy-Item $p "$p.bak"; $raw = Get-Content $p -Raw; if ($raw) {{ $c = $raw | ConvertFrom-Json }} }}
if (-not $c.mcpServers) {{ $c | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{{}}) -Force }}
$c.mcpServers | Add-Member -NotePropertyName codespringhub -NotePropertyValue ('{json.dumps(server)}' | ConvertFrom-Json) -Force
[IO.File]::WriteAllText($p, ($c | ConvertTo-Json -Depth 10))
Write-Output 'Added CodeSpringHub. Now open Claude Desktop.'"""


def connect_info(user, for_user: str | None = None) -> dict:
    """Setup instructions for `for_user` (default: the signed-in user). BSR uses this to prepare
    a researcher's setup; it contains only their username and public paths, nothing private."""
    target = (for_user or user).strip()
    if not USERNAME.fullmatch(target):
        raise HubError("That doesn't look like a cluster username (e.g. jsmith).")
    try:
        port = portal_port(target)
    except KeyError:
        raise NotFound(f"There's no cluster account called '{target}'.")
    host = config.CONNECT_HOST or _laptop_hostname()
    command = str(config.HUB_HOME / "env" / "bin" / "codespringhub")
    app_port = codespringapp_port(target)
    server = {
        "command": "ssh",
        # BatchMode: never stop to ask for a password (nobody can answer inside Claude Desktop).
        # accept-new: trust the cluster's host key on the very first connection only; SSH still
        # refuses if that key ever changes, which is the check that protects against impostors.
        # -L: while Claude Desktop is open, the same login also carries the person's portal and
        # CodeSpringApp ports to their laptop (127.0.0.1 only), so links from open_portal and
        # open_results just work. A port already busy on the laptop is skipped, never fatal.
        "args": ["-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30", "-o", "ExitOnForwardFailure=no",
                 "-L", f"127.0.0.1:{port}:localhost:{port}", "-L", f"127.0.0.1:{app_port}:localhost:{app_port}",
                 f"{target}@{host}", command],
    }
    return {
        "user": target, "host": host, "command": command,
        "claude_desktop": {"mcpServers": {"codespringhub": server}},
        "setup_mac": _setup_mac(server, target, host),
        "setup_windows": _setup_windows(server),
        "portal_port": port,
        "codespringapp_port": app_port,
        "portal_command": f"ssh -t -L {port}:localhost:{port} {target}@{host} {config.HUB_HOME / 'portal.sh'}",
        "notice": (
            "Connecting means your CodeSpringHub usage (which tools Claude uses, when, and their inputs; "
            "not your conversation) is shared with BSR. Requests about your personal items are logged "
            "without details."
        ),
    }
