"""Read the human-edited knowledge folder.

Layout (everything here is plain text, edited by people):

    knowledge/
      workflows.yaml              BSR-global workflow registry
      roles.yaml                  PIs per lab (BSR-managed)
      <lab_id>/                   LAB-SHARED: the lab's group + BSR
        lab.yaml                  name, unix_group, description, projects
        context.md                the lab overview Claude reads first
        datasets.yaml             registered data and code (pointers to /grid/)
        <category>/<doc>.md       guidance/, protocols/, notes/, grants/
        _suggestions/             Claude's suggested edits, waiting for a person
        members/<user>/           PERSONAL: that member + the lab's PIs only (never BSR)
          datasets.yaml
          <category>/<doc>.md

Hub IDs: lab-shared   ds:<lab>/<id>           doc:<lab>/<category>/<name>
         personal     ds:<lab>/~<user>/<id>   doc:<lab>/~<user>/<category>/<name>

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
    owner: str | None = None  # set for personal resources

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
    rel_path: str  # inside the lab folder, e.g. "guidance/x.md" or "members/ann/notes/x.md"
    owner: str | None = None


@dataclass
class Lab:
    id: str
    name: str
    unix_group: str
    description: str = ""
    projects: list[Project] = field(default_factory=list)
    pis: list[str] = field(default_factory=list)
    dir: Path | None = None


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
        labs[lab_id] = Lab(
            id=lab_id,
            name=data.get("name", lab_id),
            unix_group=data["unix_group"],
            description=data.get("description", ""),
            projects=[Project(**p) for p in data.get("projects", [])],
            pis=list((roles.get(lab_id) or {}).get("pis", [])),
            dir=lab_yaml.parent,
        )
    return labs


def personal_dir(lab: Lab, user: str) -> Path:
    return lab.dir / "members" / user


def member_dirs(lab: Lab) -> list[tuple[str, Path]]:
    root = lab.dir / "members"
    try:
        return [(d.name, d) for d in sorted(root.iterdir()) if d.is_dir() and not d.name.startswith((".", "_"))]
    except (FileNotFoundError, PermissionError):
        return []


def _datasets_in(path: Path, lab: Lab, owner: str | None) -> dict[str, Dataset]:
    out = {}
    for entry in _read_yaml(path).get("datasets", []) if path.exists() else []:
        entry = dict(entry)
        prefix = f"{lab.id}/{PERSONAL_MARK}{owner}" if owner else lab.id
        ds_id = f"ds:{prefix}/{entry.pop('id')}"
        entry.pop("owner", None)
        out[ds_id] = Dataset(id=ds_id, lab_id=lab.id, owner=owner, **entry)
    return out


def load_datasets(lab: Lab) -> dict[str, Dataset]:
    """Lab-shared datasets plus every personal folder this process can read."""
    datasets = _datasets_in(lab.dir / "datasets.yaml", lab, None)
    for user, d in member_dirs(lab):
        try:
            datasets |= _datasets_in(d / "datasets.yaml", lab, user)
        except PermissionError:
            continue
    return datasets


def _title_of(md_path: Path) -> str:
    with open(md_path) as fh:
        for line in fh:
            if line.startswith("# "):
                return line[2:].strip()
    return md_path.stem.replace("-", " ")


def _docs_under(root: Path, lab: Lab, owner: str | None) -> dict[str, Document]:
    docs = {}
    try:
        files = sorted(root.rglob("*.md"))
    except PermissionError:
        return docs
    for md in files:
        rel = md.relative_to(root)
        if any(part.startswith(("_", ".")) for part in rel.parts):
            continue
        if owner is None and rel.parts[0] == "members":
            continue  # personal folders are read separately
        try:
            title = _title_of(md)
        except PermissionError:
            continue
        if owner is None:
            category = "context" if rel == Path("context.md") else rel.parts[0] if len(rel.parts) > 1 else "notes"
            doc_id = f"doc:{lab.id}/{rel.with_suffix('').as_posix()}"
        else:
            category = rel.parts[0] if len(rel.parts) > 1 else "notes"
            doc_id = f"doc:{lab.id}/{PERSONAL_MARK}{owner}/{rel.with_suffix('').as_posix()}"
        docs[doc_id] = Document(id=doc_id, lab_id=lab.id, category=category, title=title, path=md,
                                rel_path=md.relative_to(lab.dir).as_posix(), owner=owner)
    return docs


def load_documents(lab: Lab) -> dict[str, Document]:
    docs = _docs_under(lab.dir, lab, None)
    for user, d in member_dirs(lab):
        docs |= _docs_under(d, lab, user)
    return docs


def load_workflows() -> list[dict]:
    path = config.KNOWLEDGE_DIR / "workflows.yaml"
    return _read_yaml(path).get("workflows", []) if path.exists() else []


def owner_of(hub_id: str) -> str | None:
    """'ds:lab/~ann/x' -> 'ann'; lab-shared IDs -> None."""
    parts = hub_id.split(":", 1)[-1].split("/")
    return parts[1][1:] if len(parts) > 2 and parts[1].startswith(PERSONAL_MARK) else None


def is_personal_id(text: str) -> bool:
    return f"/{PERSONAL_MARK}" in text
