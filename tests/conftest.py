"""Shared test setup: a throwaway Hub in a temp dir, with a cast of people.

    <tmp>/hub/knowledge   copy of the real knowledge/ plus two pilot labs
    <tmp>/hub/var         fresh state (events drop box created on first use)
    <tmp>/hub/.git        so BSR portal edits produce real commits
    <tmp>/data            copy of the demo dataset (+ a symlink that tries to escape) and a code folder
    <tmp>/home            the tester's private ~/.codespringhub

People (group membership is simulated; see GROUPS):
    USER      the real account running the tests: BSR staff AND a member of lab-a
    bsrstaff  BSR staff only
    ann, bob  lab-a members          pia   lab-a PI
    carl      lab-b member
Files on disk are all owned by USER, so filesystem privacy is tested through the ACLs and
modes the Hub sets, and the Hub's own checks are tested through these simulated people.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from codespringhub import config, permissions

REPO = Path(__file__).resolve().parents[1]
USER = permissions.current_user()
REAL_GROUP = sorted(permissions.user_groups(USER))[0]  # a real group, for ACL tests
GROUPS: dict[str, set[str]] = {}


def _lab(kdir: Path, lab_id: str, group: str, context: str):
    d = kdir / lab_id
    (d / "members").mkdir(parents=True)
    (d / "lab.yaml").write_text(f"name: {lab_id.upper()}\nunix_group: {group}\nprojects: []\n")
    (d / "context.md").write_text(f"# {lab_id}\n\n{context}\n")
    (d / "datasets.yaml").write_text("datasets: []\n")


@pytest.fixture(autouse=True)
def hub(tmp_path, monkeypatch):
    home = tmp_path / "hub"
    kdir = home / "knowledge"
    shutil.copytree(REPO / "knowledge", kdir)
    shutil.rmtree(kdir / "bsr" / "members", ignore_errors=True)
    lab_yaml = kdir / "bsr" / "lab.yaml"
    lab_yaml.write_text(lab_yaml.read_text().replace("unix_group: bioinformatics", "unix_group: bsr-grp"))

    data = tmp_path / "data"
    shutil.copytree(REPO / "demo_data" / "demo-rnaseq", data / "demo-rnaseq")
    (tmp_path / "secret.csv").write_text("a,b\n1,2\n")
    os.symlink(tmp_path / "secret.csv", data / "demo-rnaseq" / "escape.csv")
    code = data / "scripts"
    (code / "R").mkdir(parents=True)
    (code / "R" / "de.R").write_text("library(DESeq2)\n# alpha-lab differential expression\n")
    (code / "run.sh").write_text("#!/bin/bash\nRscript R/de.R\n")
    ds_yaml = kdir / "bsr" / "datasets.yaml"
    ds_yaml.write_text(ds_yaml.read_text().replace(str(REPO / "demo_data"), str(data)))

    _lab(kdir, "lab-a", "laba-grp", "Alpha lab studies limb regeneration.")
    _lab(kdir, "lab-b", "labb-grp", "confidential zebrafish secrets")
    (kdir / "roles.yaml").write_text("labs:\n  lab-a:\n    pis: [pia]\n")

    subprocess.run(["git", "init", "-q", str(home)], check=True)
    (home / ".gitignore").write_text("knowledge/*/\n!knowledge/bsr/\nknowledge/*/members/\nvar/\n")
    subprocess.run(["git", "-C", str(home), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(home), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "init"],
                   check=True)

    GROUPS.clear()
    GROUPS.update({
        USER: {"bsr-grp", "laba-grp"},
        "bsrstaff": {"bsr-grp"},
        "ann": {"laba-grp"}, "bob": {"laba-grp"}, "pia": {"laba-grp"},
        "carl": {"labb-grp"},
    })
    monkeypatch.setattr(permissions, "user_groups", lambda u: GROUPS.get(u, set()))
    monkeypatch.setattr(config, "BSR_GROUP", "bsr-grp")
    monkeypatch.setattr(config, "HUB_HOME", home)
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", kdir)
    monkeypatch.setattr(config, "VAR_DIR", home / "var")
    monkeypatch.setattr(config, "DB_PATH", home / "var" / "hub.db")
    monkeypatch.setattr(config, "BROWSE_ROOTS", [str(tmp_path)])
    monkeypatch.setattr(config, "USER_DIR", tmp_path / "home" / ".codespringhub")
    return tmp_path
