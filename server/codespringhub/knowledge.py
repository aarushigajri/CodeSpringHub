"""Read the human-edited knowledge folder.

Layout (everything here is plain text, edited by people, versioned in git):

    knowledge/
      workflows.yaml            BSR-global workflow registry
      <lab_id>/
        lab.yaml                name, unix_group, description, projects
        context.md              the lab overview Claude reads first
        datasets.yaml           registered pointers to data on /grid/
        <category>/<doc>.md     e.g. guidance/, protocols/, grants/, notes/

Files whose name starts with "_" (like _TEMPLATE.md) are ignored, so templates
and drafts can sit next to real documents without reaching the model.

Nothing is cached here: every call re-reads the files, so an edit is live as soon
as the file is saved.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import config


@dataclass
class Project:
    id: str
    name: str
    description: str = ""


@dataclass
class Dataset:
    id: str  # "ds:<lab>/<short id>", the only handle the model ever sees
    lab_id: str
    title: str
    path: str  # never shown to the model
    project: str | None = None
    assay: str = ""
    description: str = ""
    sensitivity: str = "standard"  # "standard" or "sensitive" (no previews)
    registered_by: str = ""
    registered_on: str = ""


@dataclass
class Document:
    id: str  # "doc:<lab>/<relative path without .md>"
    lab_id: str
    category: str  # "context" for context.md, otherwise the subfolder name
    title: str
    path: Path


@dataclass
class Lab:
    id: str
    name: str
    unix_group: str
    description: str = ""
    projects: list[Project] = field(default_factory=list)
    dir: Path | None = None


def _read_yaml(path: Path):
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def load_labs() -> dict[str, Lab]:
    labs = {}
    for lab_yaml in sorted(config.KNOWLEDGE_DIR.glob("*/lab.yaml")):
        data = _read_yaml(lab_yaml)
        lab_id = lab_yaml.parent.name
        labs[lab_id] = Lab(
            id=lab_id,
            name=data.get("name", lab_id),
            unix_group=data["unix_group"],
            description=data.get("description", ""),
            projects=[Project(**p) for p in data.get("projects", [])],
            dir=lab_yaml.parent,
        )
    return labs


def load_datasets(lab: Lab) -> dict[str, Dataset]:
    path = lab.dir / "datasets.yaml"
    if not path.exists():
        return {}
    datasets = {}
    for entry in _read_yaml(path).get("datasets", []):
        entry = dict(entry)
        ds_id = f"ds:{lab.id}/{entry.pop('id')}"
        datasets[ds_id] = Dataset(id=ds_id, lab_id=lab.id, **entry)
    return datasets


def _title_of(md_path: Path) -> str:
    with open(md_path) as fh:
        for line in fh:
            if line.startswith("# "):
                return line[2:].strip()
    return md_path.stem.replace("-", " ")


def load_documents(lab: Lab) -> dict[str, Document]:
    docs = {}
    for md in sorted(lab.dir.rglob("*.md")):
        rel = md.relative_to(lab.dir)
        if any(part.startswith(("_", ".")) for part in rel.parts):
            continue
        category = "context" if rel == Path("context.md") else rel.parts[0] if len(rel.parts) > 1 else "notes"
        doc_id = f"doc:{lab.id}/{rel.with_suffix('').as_posix()}"
        docs[doc_id] = Document(id=doc_id, lab_id=lab.id, category=category, title=_title_of(md), path=md)
    return docs


def load_workflows() -> list[dict]:
    path = config.KNOWLEDGE_DIR / "workflows.yaml"
    return _read_yaml(path).get("workflows", []) if path.exists() else []


def knowledge_fingerprint() -> str:
    """A cheap signature of every knowledge file (path, size, mtime).

    The search index compares this on each search and rebuilds when it changes,
    which is what makes an edited file searchable without any manual step.
    """
    parts = []
    for p in sorted(config.KNOWLEDGE_DIR.rglob("*")):
        if p.is_file():
            st = p.stat()
            parts.append(f"{p.relative_to(config.KNOWLEDGE_DIR)}:{st.st_size}:{st.st_mtime_ns}")
    return "|".join(parts)
