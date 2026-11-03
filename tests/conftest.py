"""Shared test setup: a throwaway Hub in a temp dir.

    <tmp>/hub/knowledge   copy of the real knowledge/ (BSR lab made accessible to the tester)
    <tmp>/hub/var         fresh database
    <tmp>/hub/.git        so portal edits produce real commits
    <tmp>/data            copy of the demo dataset (+ a symlink that tries to escape)

plus "otherlab", whose Unix group the tester is not in.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from codespringhub import config, permissions

REPO = Path(__file__).resolve().parents[1]
USER = permissions.current_user()
MY_GROUP = sorted(permissions.user_groups(USER))[0]


@pytest.fixture(autouse=True)
def hub(tmp_path, monkeypatch):
    home = tmp_path / "hub"
    kdir = home / "knowledge"
    shutil.copytree(REPO / "knowledge", kdir)
    lab_yaml = kdir / "bsr" / "lab.yaml"
    lab_yaml.write_text(lab_yaml.read_text().replace("unix_group: bioinformatics", f"unix_group: {MY_GROUP}"))

    data = tmp_path / "data"
    shutil.copytree(REPO / "demo_data" / "demo-rnaseq", data / "demo-rnaseq")
    (tmp_path / "secret.csv").write_text("a,b\n1,2\n")
    os.symlink(tmp_path / "secret.csv", data / "demo-rnaseq" / "escape.csv")
    ds_yaml = kdir / "bsr" / "datasets.yaml"
    ds_yaml.write_text(ds_yaml.read_text().replace(str(REPO / "demo_data"), str(data)))

    other = kdir / "otherlab"
    other.mkdir()
    (other / "lab.yaml").write_text("name: Other Lab\nunix_group: no_such_group_codespringhub\n")
    (other / "context.md").write_text("# Other lab\n\nconfidential zebrafish secrets\n")

    subprocess.run(["git", "init", "-q", str(home)], check=True)
    subprocess.run(["git", "-C", str(home), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(home), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "init"],
                   check=True)

    monkeypatch.setattr(config, "HUB_HOME", home)
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", kdir)
    monkeypatch.setattr(config, "VAR_DIR", home / "var")
    monkeypatch.setattr(config, "DB_PATH", home / "var" / "hub.db")
    monkeypatch.setattr(config, "BROWSE_ROOTS", [str(tmp_path)])
    # The tester counts as BSR staff (for workflow edits) via their first group.
    monkeypatch.setattr(permissions, "BSR_GROUP", MY_GROUP)
    return tmp_path
