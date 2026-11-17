"""Read the human-edited knowledge folder.

Layout (everything here is plain text, edited by people):

    knowledge/
      workflows.yaml              BSR-global workflow registry
      roles.yaml                  PIs per lab (BSR-managed)
      <lab_id>/                   a lab (kind: lab, the default) or a core facility (kind: core)
                                  LAB-SHARED: the lab's group + BSR
        lab.yaml                  name, unix_group, description, projects
        context.md                the lab overview Claude reads first
        datasets.yaml             registered data and code (pointers to /grid/)
        <category>/<doc>.md       guidance/, protocols/, notes/, grants/
        _suggestions/             Claude's suggested edits, waiting for a person
        members/<user>/           PERSONAL: that member + the lab's PIs only (never BSR)
          datasets.yaml
          <category>/<doc>.md
        private/<user>/           PRIVATE: that member only (not the PI, not BSR); same layout
      <core_id>/                  A CORE FACILITY (BSR, Sequencing, Microscopy, ...): lab.yaml says kind: core
        lab.yaml                  name, unix_group (the core's staff), website, contacts; readable by everyone
        shared/                   SHARED: everyone using the Hub (protocols, guidance, example data, scripts)
          context.md, datasets.yaml, <category>/<doc>.md
        internal/                 INTERNAL: the core's staff only; same layout
        members/<user>/, private/<user>/   as for labs (the core's lead plays the PI's part)

Hub IDs: lab-shared   ds:<lab>/<id>           doc:<lab>/<category>/<name>
         personal     ds:<lab>/~<user>/<id>   doc:<lab>/~<user>/<category>/<name>
         private      ds:<lab>/~<user>/private/<id>   doc:<lab>/~<user>/private/<category>/<name>
         core shared  ds:<core>/shared/<id>   doc:<core>/shared/<category>/<name>
         core internal ds:<core>/internal/<id> doc:<core>/internal/<category>/<name>

A core's top folder holds nothing but lab.yaml and those four folders, so even the names of its
internal documents are hidden from other labs.

Files whose name starts with "_" or "." are ignored. Folders the current user can't read are
skipped silently: the filesystem is the first permission check, permissions.py the second.

Nothing is cached here: every call re-reads the files, so an edit is live as soon
as the file is saved.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import config

PERSONAL_MARK = "~"
PRIVATE_PART = "private"
SHARED_PART = "shared"      # a core's everyone-visible section
INTERNAL_PART = "internal"  # a core's staff-only section
KINDS = ("lab", "core")


@dataclass
class Project:
    id: str
    name: str
    description: str = ""


@dataclass
class Dataset:
    id: str  # the only handle the model ever sees
    lab_id: str
    title: str
    path: str  # never shown to the model
    project: str | None = None
    assay: str = ""
    description: str = ""
    sensitivity: str = "standard"  # "standard" or "sensitive" (no previews)
    kind: str = "data"  # "data" (summaries only) or "code" (source files readable)
    registered_by: str = ""
    registered_on: str = ""
    owner: str | None = None  # set for personal and private resources
    private: bool = False  # owner only (not their PI)
    shared: bool = False  # in a core's shared section: everyone using the Hub

    @property
    def short_id(self) -> str:
        return self.id.split("/", 1)[1]


@dataclass
class Document:
    id: str
    lab_id: str
    category: str  # "context" for context.md, otherwise the subfolder name
    title: str
    path: Path
    rel_path: str  # inside the lab folder, e.g. "guidance/x.md", "members/ann/notes/x.md", "private/ann/notes/x.md"
    owner: str | None = None
    private: bool = False
    shared: bool = False


@dataclass
class Lab:
    """A lab, or a core facility (kind 'core'). For a core, unix_group is its staff and `pis` its
    lead(s); an empty unix_group means the core isn't on the Hub yet (a template BSR keeps)."""
    id: str
    name: str
    unix_group: str
    description: str = ""
    projects: list[Project] = field(default_factory=list)
    pis: list[str] = field(default_factory=list)
    dir: Path | None = None
    kind: str = "lab"
    website: str = ""
    contacts: list[dict] = field(default_factory=list)

    @property
    def is_core(self) -> bool:
        return self.kind == "core"

    @property
    def onboarded(self) -> bool:
        return bool(self.unix_group)


def _read_yaml(path: Path):
    try:
        with open(path) as fh:
            return yaml.safe_load(fh) or {}
    except (PermissionError, FileNotFoundError):
        return {}


def load_roles() -> dict:
    return _read_yaml(config.KNOWLEDGE_DIR / "roles.yaml").get("labs", {}) or {}


def load_labs() -> dict[str, Lab]:
    roles = load_roles()
    labs = {}
    for lab_yaml in sorted(config.KNOWLEDGE_DIR.glob("*/lab.yaml")):
        data = _read_yaml(lab_yaml)
        if not data:
            continue  # not readable by this user
        lab_id = lab_yaml.parent.name
        kind = data.get("kind", "lab")
        labs[lab_id] = Lab(
            id=lab_id,
            name=data.get("name", lab_id),
            unix_group=data.get("unix_group") or "",
            description=data.get("description", ""),
            projects=[Project(**p) for p in data.get("projects", []) or []],
            pis=list((roles.get(lab_id) or {}).get("pis", [])),
            dir=lab_yaml.parent,
            kind=kind if kind in KINDS else "lab",
            website=data.get("website", "") or "",
            contacts=[c for c in data.get("contacts", []) or [] if isinstance(c, dict) and c.get("name")],
        )
        if not labs[lab_id].unix_group and kind != "core":
            del labs[lab_id]  # a lab needs its group; only a core may wait for one
    return labs


def personal_dir(lab: Lab, user: str) -> Path:
    return lab.dir / "members" / user


def private_dir(lab: Lab, user: str) -> Path:
    return lab.dir / "private" / user


def private_dirs(lab: Lab) -> list[tuple[str, Path]]:
    """Private folders; the filesystem only lets each person into their own."""
    return member_dirs(lab, "private")


def member_dirs(lab: Lab, area: str = "members") -> list[tuple[str, Path]]:
    root = lab.dir / area
    try:
        return [(d.name, d) for d in sorted(root.iterdir()) if d.is_dir() and not d.name.startswith((".", "_"))]
    except (FileNotFoundError, PermissionError):
        return []


def _prefix(lab: Lab, owner: str | None, private: bool, area: str | None = None) -> str:
    if not owner:
        return f"{lab.id}/{area}" if area else lab.id
    return f"{lab.id}/{PERSONAL_MARK}{owner}" + (f"/{PRIVATE_PART}" if private else "")


def _datasets_in(path: Path, lab: Lab, owner: str | None, private: bool = False,
                 area: str | None = None) -> dict[str, Dataset]:
    out = {}
    try:
        entries = _read_yaml(path).get("datasets", []) if path.exists() else []
    except PermissionError:
        return out
    for entry in entries or []:
        entry = dict(entry)
        ds_id = f"ds:{_prefix(lab, owner, private, area)}/{entry.pop('id')}"
        for k in ("owner", "private", "shared"):
            entry.pop(k, None)
        out[ds_id] = Dataset(id=ds_id, lab_id=lab.id, owner=owner, private=private,
                             shared=area == SHARED_PART, **entry)
    return out


def core_areas(lab: Lab) -> list[tuple[str, Path]]:
    """A core's shared and internal sections (folder name, path); none for a lab."""
    return [(a, lab.dir / a) for a in (SHARED_PART, INTERNAL_PART)] if lab.is_core else []


def load_datasets(lab: Lab) -> dict[str, Dataset]:
    """Lab-shared datasets (or a core's shared and internal ones) plus every personal folder this
    process can read."""
    if lab.is_core:
        datasets = {}
        for area, d in core_areas(lab):
            datasets |= _datasets_in(d / "datasets.yaml", lab, None, area=area)
    else:
        datasets = _datasets_in(lab.dir / "datasets.yaml", lab, None)
    for private, dirs in ((False, member_dirs(lab)), (True, private_dirs(lab))):
        for user, d in dirs:
            try:
                datasets |= _datasets_in(d / "datasets.yaml", lab, user, private)
            except PermissionError:
                continue
    return datasets


def _title_of(md_path: Path) -> str:
    with open(md_path) as fh:
        for line in fh:
            if line.startswith("# "):
                return line[2:].strip()
    return md_path.stem.replace("-", " ")


def _docs_under(root: Path, lab: Lab, owner: str | None, private: bool = False,
                area: str | None = None) -> dict[str, Document]:
    docs = {}
    try:
        files = sorted(root.rglob("*.md"))
    except PermissionError:
        return docs
    for md in files:
        rel = md.relative_to(root)
        if any(part.startswith(("_", ".")) for part in rel.parts):
            continue
        if owner is None and area is None and rel.parts[0] in ("members", "private", SHARED_PART, INTERNAL_PART):
            continue  # personal, private and a core's sections are read separately
        try:
            title = _title_of(md)
        except PermissionError:
            continue
        if owner is None:
            category = "context" if rel == Path("context.md") else rel.parts[0] if len(rel.parts) > 1 else "notes"
            doc_id = f"doc:{_prefix(lab, None, False, area)}/{rel.with_suffix('').as_posix()}"
        else:
            category = rel.parts[0] if len(rel.parts) > 1 else "notes"
            doc_id = f"doc:{_prefix(lab, owner, private)}/{rel.with_suffix('').as_posix()}"
        docs[doc_id] = Document(id=doc_id, lab_id=lab.id, category=category, title=title, path=md,
                                rel_path=md.relative_to(lab.dir).as_posix(), owner=owner, private=private,
                                shared=area == SHARED_PART)
    return docs


def load_documents(lab: Lab) -> dict[str, Document]:
    if lab.is_core:
        docs = {}
        for area, d in core_areas(lab):
            docs |= _docs_under(d, lab, None, area=area)
    else:
        docs = _docs_under(lab.dir, lab, None)
    for user, d in member_dirs(lab):
        docs |= _docs_under(d, lab, user)
    for user, d in private_dirs(lab):
        docs |= _docs_under(d, lab, user, private=True)
    return docs


def load_workflows() -> list[dict]:
    path = config.KNOWLEDGE_DIR / "workflows.yaml"
    return _read_yaml(path).get("workflows", []) if path.exists() else []


def owner_of(hub_id: str) -> str | None:
    """'ds:lab/~ann/x' -> 'ann'; lab-shared IDs -> None."""
    parts = hub_id.split(":", 1)[-1].split("/")
    return parts[1][1:] if len(parts) > 2 and parts[1].startswith(PERSONAL_MARK) else None


def scope_of(item) -> str:
    """'lab' (the lab, or a core's staff), 'shared' (a core's everyone-visible section),
    'personal' (owner + PI/lead) or 'private' (owner only) for a Dataset or Document."""
    return "private" if item.private else "personal" if item.owner else "shared" if item.shared else "lab"


def is_private_id(hub_id: str) -> bool:
    """'ds:lab/~ann/private/x' -> True. Categories and dataset IDs can't be named 'private' at that spot."""
    parts = hub_id.split(":", 1)[-1].split("/")
    return owner_of(hub_id) is not None and len(parts) > 3 and parts[2] == PRIVATE_PART


def is_personal_id(text: str) -> bool:
    """True for personal AND private IDs (both carry '/~user')."""
    return f"/{PERSONAL_MARK}" in text
