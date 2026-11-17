"""Private items (only their owner, not the PI) and the GPFS NFSv4 ACLs /grid actually uses.

The private-item tests run anywhere. The GPFS tests run only where a GPFS folder with NFSv4 ACLs
is available (on the cluster: set CODESPRINGHUB_GPFS_TESTDIR, or it uses your home folder on
/grid), and are skipped elsewhere.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from codespringhub import acl, config, editing, knowledge, store
from codespringhub import hub as hubsvc
from codespringhub.errors import Forbidden, NotFound
from codespringhub.identity import LocalLinkAuth
from codespringhub.web import create_app

from conftest import REAL_GROUP, USER


@pytest.fixture
def api(hub):
    auth = LocalLinkAuth(new_link=True)
    client = TestClient(create_app(auth))
    client.headers["Authorization"] = f"Bearer {auth.token}"
    return client


def private_dataset(api, hub):
    body = {"title": "My secret pilot", "path": str(hub / "data" / "demo-rnaseq"), "scope": "private"}
    r = api.post("/api/labs/lab-a/datasets", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---- private items: who sees what ----------------------------------------------------------

def test_private_items_only_for_their_owner(api, hub):
    ds_id = private_dataset(api, hub)
    assert ds_id == f"ds:lab-a/~{USER}/private/my-secret-pilot"
    assert knowledge.is_private_id(ds_id) and knowledge.owner_of(ds_id) == USER
    visible = lambda u: [d["id"] for d in hubsvc.list_datasets(u, "lab-a")]  # noqa: E731
    assert ds_id in visible(USER)
    assert ds_id not in visible("pia")   # not even the PI
    assert ds_id not in visible("ann")
    for other in ("pia", "bsrstaff", "carl"):
        with pytest.raises(NotFound):
            hubsvc.get_dataset(other, ds_id)
    row = next(d for d in editing.datasets_for_portal(USER, "lab-a") if d["id"] == ds_id)
    assert row["scope"] == "private" and row["can_edit"]
    assert hubsvc.describe_dataset(USER, ds_id)["scope"] == "private"


def test_private_documents_search_and_suggestions(api, hub):
    rel = api.post("/api/labs/lab-a/docs", json={"category": "notes", "title": "Idea", "scope": "private"}).json()["rel_path"]
    assert rel == f"private/{USER}/notes/idea.md"
    api.put(f"/api/labs/lab-a/docs/{rel}", json={"text": "# Idea\n\nThe wombat hypothesis.\n"}).raise_for_status()
    doc_id = f"doc:lab-a/~{USER}/private/notes/idea"
    assert [h["doc_id"] for h in hubsvc.search(USER, "wombat")] == [doc_id]
    assert hubsvc.search("pia", "wombat") == []
    assert hubsvc.search("bsrstaff", "wombat") == []
    with pytest.raises(NotFound):
        editing.read_document_file("pia", "lab-a", rel)
    with pytest.raises(Forbidden):
        editing.save_document("ann", "lab-a", rel, "overwrite")
    assert editing.read_document_file(USER, "lab-a", rel)["scope"] == "private"
    # Claude's suggestions about it stay in the private folder.
    r = api.post("/api/tools/suggest_context_edit",
                 json={"arguments": {"lab_id": "lab-a", "suggestion": "add Y", "doc_id": doc_id}}).json()
    assert not r["is_error"], r
    assert len(list((config.KNOWLEDGE_DIR / "lab-a" / "private" / USER / "_suggestions").glob("*.yaml"))) == 1
    assert [s["scope"] for s in editing.inbox(USER)["suggestions"]] == ["private"]
    assert editing.inbox("pia")["suggestions"] == []


def test_private_folder_is_owner_only_with_its_own_history(api, hub):
    lab_dir = config.KNOWLEDGE_DIR / "lab-a"
    acl.grant_lab(lab_dir, REAL_GROUP)
    (config.KNOWLEDGE_DIR / "roles.yaml").write_text("labs:\n  lab-a:\n    pis: [root]\n")
    private_dataset(api, hub)
    folder = lab_dir / "private" / USER
    assert acl.personal_is_private(folder, []) == []  # no group, no PI, nobody else
    assert not acl.entries(folder)
    assert (folder / ".git").exists()  # separate from the personal history the PI can read
    assert not (lab_dir / "members" / USER / "private").exists()


def test_private_logs_withhold_details(api, hub):
    ds_id = private_dataset(api, hub)
    api.post("/api/tools/describe_dataset", json={"arguments": {"dataset_id": ds_id}})
    rows = [r for r in store.read_events("audit") if r["tool"] in ("describe_dataset", "portal:save_dataset")]
    assert rows and all(json.loads(r["args"]) == store.WITHHELD for r in rows)
    assert b"/~" not in (config.VAR_DIR / "events" / f"{USER}.db").read_bytes()


def test_unknown_folders_in_document_paths_are_refused(hub):
    from codespringhub.errors import HubError
    with pytest.raises(HubError):
        editing.read_document_file(USER, "lab-a", "private/notes/x.md")


# ---- GPFS NFSv4 ACLs (on /grid) ---------------------------------------------------------------

def _gpfs_dir():
    for cand in (os.environ.get("CODESPRINGHUB_GPFS_TESTDIR"), str(Path.home())):
        if cand and os.path.isdir(cand) and os.access(cand, os.W_OK) and acl.is_nfs4(Path(cand)):
            return cand
    return None


GPFS = _gpfs_dir()
gpfs_only = pytest.mark.skipif(GPFS is None, reason="needs a GPFS folder with NFSv4 ACLs (/grid)")


@pytest.fixture
def gdir():
    d = Path(tempfile.mkdtemp(prefix=".hub-acl-test-", dir=GPFS))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@gpfs_only
def test_gpfs_lab_folder_shares_with_lab_and_new_files_inherit(gdir):
    gid = __import__("grp").getgrnam(REAL_GROUP).gr_gid
    lab = gdir / "lab"
    lab.mkdir()
    (lab / "context.md").write_text("x\n")
    acl.grant_lab(lab, REAL_GROUP)
    assert {f"group:{gid}:rwx", f"default:group:{gid}:rwx"} <= acl.entries(lab)
    can_write = lambda p: any(e.startswith(f"group:{gid}:rw") for e in acl.entries(p))  # noqa: E731
    assert can_write(lab / "context.md")  # existing files too
    # The Hub's own save keeps the inherited access (no chmod on GPFS).
    editing._write_atomic(lab / "notes.md", "hello\n")
    assert can_write(lab / "notes.md")
    # So does git: no core.sharedRepository chmod in an inheriting folder.
    editing.init_history(lab, [])
    editing._git(lab, "add", "-A")
    editing._git(lab, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "t")
    obj = next(p for p in (lab / ".git" / "objects").rglob("*") if p.is_file())
    assert any(e.startswith(f"group:{gid}:r") for e in acl.entries(obj))
    # members/ and private/: the lab can add its own folders there, nothing is inherited inside.
    for sub in ("members", "private"):
        assert not [e for e in acl.entries(lab / sub) if e.startswith("default:")]
        assert (lab / sub).stat().st_mode & 0o1000  # sticky


@gpfs_only
def test_gpfs_personal_is_owner_and_pi_private_is_owner_only(gdir):
    lab = gdir / "lab"
    lab.mkdir()
    acl.grant_lab(lab, REAL_GROUP)
    personal = lab / "members" / "me"
    private = lab / "private" / "me"
    for f in (personal, private):
        f.mkdir()
        (f / "notes").mkdir()
        (f / "notes" / "a.md").write_text("x\n")
    acl.secure_personal(personal, ["root"], owner=USER)
    acl.secure_private(private)
    assert acl.personal_is_private(personal, ["root"]) == []
    assert "user:0:rwx" in acl.entries(personal) and "default:user:0:rwx" in acl.entries(personal)
    assert acl.personal_is_private(personal / "notes", ["root"]) == []
    assert acl.personal_is_private(private, []) == [] and not acl.entries(private)
    assert not acl.entries(private / "notes" / "a.md")
    # A later file in the private folder inherits owner-only access.
    editing._write_atomic(private / "b.md", "y\n")
    assert not acl.entries(private / "b.md") and acl.owning_group_entry(private / "b.md") == "group::---"


@gpfs_only
def test_gpfs_chmod_wipes_acls_and_the_check_notices(gdir):
    lab = gdir / "lab"
    lab.mkdir()
    acl.grant_lab(lab, REAL_GROUP)
    personal = lab / "members" / "me"
    personal.mkdir()
    acl.secure_personal(personal, ["root"], owner=USER)
    os.chmod(personal, 0o2750)  # someone "tidies up": GPFS replaces the ACL from the mode bits
    problems = acl.personal_is_private(personal, ["root"])
    assert problems and any("group" in p for p in problems)
