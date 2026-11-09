"""Portal API tests: login, editing, validation, history, and the Try-it console."""

import subprocess

import pytest
from fastapi.testclient import TestClient

from codespringhub import config, editing
from codespringhub.errors import HubError
from codespringhub.identity import LocalLinkAuth
from codespringhub.web import create_app

from conftest import USER


@pytest.fixture
def api(hub):
    auth = LocalLinkAuth(new_link=True)
    client = TestClient(create_app(auth))
    client.headers["Authorization"] = f"Bearer {auth.token}"
    return client


def git_log(hub):
    return subprocess.run(["git", "-C", str(hub / "hub"), "log", "--format=%an|%s"],
                          capture_output=True, text=True).stdout.splitlines()


def test_requires_the_private_link(hub):
    client = TestClient(create_app(LocalLinkAuth(new_link=True)))
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert (config.USER_DIR / "portal-token").stat().st_mode & 0o777 == 0o600
    assert config.USER_DIR.stat().st_mode & 0o777 == 0o700


def test_labs_and_permissions(api):
    # The tester is BSR staff (supports every lab) and a member of lab-a.
    labs = {l["lab_id"]: l for l in api.get("/api/labs").json()}
    assert sorted(labs) == ["bsr", "lab-a", "lab-b"] and labs["bsr"]["can_edit"]
    assert labs["lab-a"]["role"] == "member" and labs["lab-b"]["role"] == "bsr"
    assert api.get("/api/labs/nope").status_code == 404
    assert api.put("/api/labs/nope/docs/context.md", json={"text": "x"}).status_code == 404


def test_edit_context_is_live_for_claude_and_committed(api, hub):
    api.put("/api/labs/bsr/docs/context.md", json={"text": "# BSR\n\nWe use GRCm39 for mouse.\n"}).raise_for_status()
    r = api.post("/api/tools/get_lab_context", json={"arguments": {"lab_id": "bsr"}}).json()
    assert not r["is_error"] and "GRCm39" in r["content"][0]
    assert git_log(hub)[0].endswith("portal: edit bsr/context.md")


def test_create_edit_delete_document(api):
    rel = api.post("/api/labs/bsr/docs", json={"category": "protocols", "title": "Library prep QC"}).json()["rel_path"]
    assert rel == "protocols/library-prep-qc.md"
    assert "TODO" in api.get(f"/api/labs/bsr/docs/{rel}").json()["text"]
    hits = api.post("/api/tools/search_knowledge", json={"arguments": {"query": "Library prep"}}).json()
    assert "doc:bsr/protocols/library-prep-qc" in hits["content"][0]
    assert api.delete(f"/api/labs/bsr/docs/{rel}").status_code == 200
    assert api.get(f"/api/labs/bsr/docs/{rel}").status_code == 404


@pytest.mark.parametrize("bad", ["../lab.yaml", "guidance/../../x.md", "datasets.yaml", "guidance/Bad Name.md"])
def test_document_paths_are_validated(api, bad):
    # Over HTTP, "../" is collapsed by the client and lands on an unknown address (404).
    assert api.put(f"/api/labs/bsr/docs/{bad}", json={"text": "x"}).status_code in (400, 404)
    # Called directly, the editing layer refuses it too.
    with pytest.raises(HubError):
        editing.save_document(USER, "bsr", bad, "x")
    assert "unix_group" in (config.KNOWLEDGE_DIR / "bsr" / "lab.yaml").read_text()


def test_register_dataset_with_folder_browser(api, hub):
    root = api.get("/api/browse").json()
    assert root["folders"][0]["path"] == str(hub)
    listing = api.get("/api/browse", params={"path": str(hub / "data")}).json()
    assert "demo-rnaseq" in [f["name"] for f in listing["folders"]]

    body = {"title": "Second copy", "path": str(hub / "data" / "demo-rnaseq"), "project": "codespringhub-pilot",
            "assay": "rna-seq", "description": "test", "sensitivity": "sensitive"}
    ds_id = api.post("/api/labs/bsr/datasets", json=body).json()["id"]
    assert ds_id == "ds:bsr/second-copy"
    r = api.post("/api/tools/query_table", json={"arguments": {
        "dataset_id": ds_id, "asset": "counts.tsv", "sql": "SELECT 1"}}).json()
    assert r["is_error"] and "sensitive" in r["content"][0]
    assert git_log(hub)[0].endswith("register data bsr/second-copy")


@pytest.mark.parametrize("path,msg", [
    ("/etc", "Only data under"),
    ("relative/path", "Choose a folder"),
    ("{hub}/data/nope", "doesn't exist"),
    ("{hub}/hub/knowledge", "part of the Hub"),
])
def test_dataset_path_checks(api, hub, path, msg):
    body = {"title": "x", "path": path.format(hub=hub)}
    r = api.post("/api/labs/bsr/datasets", json=body)
    assert r.status_code >= 400 and msg in r.json()["detail"]


def test_browse_cannot_leave_roots(api):
    assert api.get("/api/browse", params={"path": "/etc"}).status_code == 403


def test_workflow_edit_and_lab_details(api):
    wf = {"name": "nf-core/methylseq", "assays": ["methylation"], "status": "candidate", "version": "2.6.0"}
    assert api.post("/api/workflows", json=wf).json()["id"] == "nf-core-methylseq"
    assert api.post("/api/workflows", json=wf | {"status": "maybe"}).status_code == 400

    lab = {"name": "BSR", "description": "core", "projects": [{"name": "Pilot Two"}]}
    api.put("/api/labs/bsr", json=lab).raise_for_status()
    assert api.get("/api/labs/bsr").json()["projects"] == [{"id": "pilot-two", "name": "Pilot Two", "description": ""}]


def test_inbox_flow(api):
    api.post("/api/tools/suggest_context_edit", json={"arguments": {"lab_id": "bsr", "suggestion": "Add GRCm39"}})
    api.post("/api/tools/request_bsr_review", json={"arguments": {"lab_id": "bsr", "summary": "Help"}})
    inbox = api.get("/api/inbox").json()
    sid, rid = inbox["suggestions"][0]["id"], inbox["reviews"][0]["key"]
    api.post(f"/api/suggestions/{sid}", json={"status": "applied"}).raise_for_status()
    api.post(f"/api/reviews/{rid}", json={"status": "closed"}).raise_for_status()
    inbox = api.get("/api/inbox").json()
    assert inbox["suggestions"][0]["status"] == "applied" and inbox["reviews"][0]["status"] == "closed"
    tools = [a["tool"] for a in api.get("/api/activity").json()]
    assert "portal:review_status" in tools and "suggest_context_edit" in tools


def test_tool_list_for_try_it(api):
    names = {t["name"] for t in api.get("/api/tools").json()}
    assert {"get_lab_context", "query_table", "request_bsr_review"} <= names


def test_save_works_for_a_user_without_git_setup(api, hub, monkeypatch):
    # Simulates a BSR member who has never run `git config user.name`.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    api.put("/api/labs/bsr/docs/context.md", json={"text": "# BSR\n\nNew text.\n"}).raise_for_status()
    assert git_log(hub)[0].endswith("portal: edit bsr/context.md")


def test_git_failure_never_blocks_a_save(api, hub):
    # Break the repository: the save must still succeed and the problem is recorded.
    (hub / "hub" / ".git" / "HEAD").write_text("garbage")
    r = api.put("/api/labs/bsr/docs/context.md", json={"text": "# BSR\n\nSaved anyway.\n"})
    assert r.status_code == 200
    assert "Saved anyway" in (config.KNOWLEDGE_DIR / "bsr" / "context.md").read_text()
    assert "portal:history_not_recorded" in [a["tool"] for a in api.get("/api/activity").json()]


def test_new_files_are_group_editable(api):
    rel = api.post("/api/labs/bsr/docs", json={"category": "notes", "title": "Shared note"}).json()["rel_path"]
    assert (config.KNOWLEDGE_DIR / "bsr" / rel).stat().st_mode & 0o777 == 0o660
