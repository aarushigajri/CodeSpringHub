"""Several labs on one Hub: lab-shared vs personal, PIs, BSR's reach, code, logs, and lab setup."""

import argparse
import json
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from codespringhub import acl, cli, config, editing, knowledge, store
from codespringhub import hub as hubsvc
from codespringhub.errors import Forbidden, HubError, NotFound
from codespringhub.identity import LocalLinkAuth
from codespringhub.web import create_app

from conftest import REAL_GROUP, USER


@pytest.fixture
def api(hub):
    auth = LocalLinkAuth(new_link=True)
    client = TestClient(create_app(auth))
    client.headers["Authorization"] = f"Bearer {auth.token}"
    return client


def scripts(hub):
    return str(hub / "data" / "scripts")


def personal_dataset(api, hub, **extra):
    body = {"title": "My pilot RNA-seq", "path": str(hub / "data" / "demo-rnaseq"), "scope": "personal"} | extra
    r = api.post("/api/labs/lab-a/datasets", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---- who sees what -------------------------------------------------------------------

def test_personal_items_only_for_owner_and_pi(api, hub):
    ds_id = personal_dataset(api, hub)
    assert ds_id == f"ds:lab-a/~{USER}/my-pilot-rna-seq"
    visible = lambda u: [d["id"] for d in hub_list(u)]  # noqa: E731
    assert ds_id in visible(USER)      # owner
    assert ds_id in visible("pia")     # PI of lab-a
    assert ds_id not in visible("ann")  # another member
    with pytest.raises(NotFound):
        hubsvc.get_dataset("bsrstaff", ds_id)  # BSR: not authorised for personal items
    with pytest.raises(NotFound):
        hubsvc.get_dataset("carl", ds_id)  # other lab


def hub_list(user):
    return hubsvc.list_datasets(user, "lab-a")


def test_pi_role_comes_only_from_bsr_roles_file(api, hub):
    ds_id = personal_dataset(api, hub)
    lab_yaml = config.KNOWLEDGE_DIR / "lab-a" / "lab.yaml"
    lab_yaml.write_text(lab_yaml.read_text() + "pis: [ann]\n")  # a member trying to promote themselves
    assert ds_id not in [d["id"] for d in hub_list("ann")]


def test_personal_documents_and_search(api, hub):
    rel = api.post("/api/labs/lab-a/docs", json={"category": "notes", "title": "Failed library", "scope": "personal"}).json()["rel_path"]
    assert rel == f"members/{USER}/notes/failed-library.md"
    api.put(f"/api/labs/lab-a/docs/{rel}", json={"text": "# Failed library\n\nThe quokka batch failed QC.\n"}).raise_for_status()
    doc_id = f"doc:lab-a/~{USER}/notes/failed-library"
    assert [h["doc_id"] for h in hubsvc.search(USER, "quokka")] == [doc_id]
    assert [h["doc_id"] for h in hubsvc.search("pia", "quokka")] == [doc_id]
    assert hubsvc.search("ann", "quokka") == []
    assert hubsvc.search("bsrstaff", "quokka") == []
    with pytest.raises(Forbidden):
        editing.save_document("ann", "lab-a", rel, "overwrite")
    with pytest.raises(NotFound):
        editing.read_document_file("bsrstaff", "lab-a", rel)


def test_bsr_without_membership_gets_no_personal_area(hub):
    with pytest.raises(Forbidden):
        editing.create_document("bsrstaff", "lab-a", "notes", "x", scope="personal")


# ---- file permissions behind it ---------------------------------------------------------

def test_personal_folder_is_private_on_disk(api, hub):
    lab_dir = config.KNOWLEDGE_DIR / "lab-a"
    acl.grant_lab(lab_dir, REAL_GROUP)  # lab folder shares with the lab + BSR, inherited by default
    (config.KNOWLEDGE_DIR / "roles.yaml").write_text("labs:\n  lab-a:\n    pis: [root]\n")
    personal_dataset(api, hub)
    folder = lab_dir / "members" / USER
    assert folder.stat().st_mode & 0o7007 == 0o2000       # nothing for "others"
    assert acl.owning_group_entry(folder) == "group::---"  # nothing for the folder's group (BSR)
    entries = acl.entries(folder)
    assert not [e for e in entries if "group:" in e]  # the lab's and BSR's inherited access is gone
    assert "user:0:rwx" in entries                    # the PI (root here) is in
    assert acl.personal_is_private(folder, ["root"]) == []
    # The personal history lives inside the private folder, not in a shared repository.
    assert (folder / ".git").exists()


def test_add_lab_sets_up_everything(hub):
    args = argparse.Namespace(lab_id="smith-lab", group=REAL_GROUP, name="Smith Lab", pi=[USER])
    cli.cmd_add_lab(args)
    lab_dir = config.KNOWLEDGE_DIR / "smith-lab"
    gid = __import__("grp").getgrnam(REAL_GROUP).gr_gid
    assert f"group:{gid}:rwx" in acl.entries(lab_dir)
    assert f"default:group:{gid}:rwx" in acl.entries(lab_dir)
    assert (lab_dir / ".git").exists() and (lab_dir / "members").is_dir()
    assert f"group:{gid}:r-x" in acl.entries(config.KNOWLEDGE_DIR)
    events = acl.entries(config.VAR_DIR / "events")
    assert f"group:{gid}:-wx" in events and not [e for e in events if e.startswith("default:")]
    assert knowledge.load_labs()["smith-lab"].pis == [USER]
    assert "TODO" in (lab_dir / "context.md").read_text()


# ---- code ---------------------------------------------------------------------------------

def test_code_resources_can_be_read_in_full(api, hub):
    r = api.post("/api/labs/lab-a/datasets", json={"title": "DE scripts", "path": scripts(hub), "kind": "code"})
    code_id = r.json()["id"]
    described = hubsvc.describe_dataset("ann", code_id)
    assert described["kind"] == "code" and {"R/de.R", "run.sh"} <= {a["asset"] for a in described["assets"]}
    out = api.post("/api/tools/read_code_file", json={"arguments": {"resource_id": code_id, "path": "R/de.R"}}).json()
    assert not out["is_error"] and "DESeq2" in out["content"][0]
    # Data resources never give full file contents.
    out = api.post("/api/tools/read_code_file", json={"arguments": {"resource_id": "ds:bsr/shared/demo-rnaseq", "path": "counts.tsv"}}).json()
    assert out["is_error"] and "summary" in out["content"][0]


# ---- logs shared with BSR --------------------------------------------------------------------

def test_logs_withhold_personal_details(api, hub):
    ds_id = personal_dataset(api, hub)
    api.post("/api/tools/describe_dataset", json={"arguments": {"dataset_id": ds_id}})
    api.post("/api/tools/search_knowledge", json={"arguments": {"query": "limb", "lab_id": "lab-a"}})
    rows = store.read_events("audit")
    personal = [r for r in rows if r["tool"] in ("describe_dataset", "portal:save_dataset")]
    assert personal and all(json.loads(r["args"]) == store.WITHHELD for r in personal)
    assert all(r["lab_id"] == "lab-a" for r in personal)
    shared = next(r for r in rows if r["tool"] == "search_knowledge")
    assert json.loads(shared["args"])["query"] == "limb"  # lab-shared usage keeps full details
    # Raw log file: the personal ID never appears anywhere in it.
    raw = (config.VAR_DIR / "events" / f"{USER}.db").read_bytes()
    assert b"/~" not in raw


def test_bsr_sees_everyones_logs_members_only_their_own(api):
    api.post("/api/tools/list_workflows", json={"arguments": {}})
    assert [r["tool"] for r in editing.activity("bsrstaff")] == ["list_workflows"]
    assert editing.activity("ann") == []


def test_log_rows_must_come_from_their_owner(hub):
    store.log_call(USER, "whoami", {}, True, 1)
    forged = config.VAR_DIR / "events" / "ann.db"  # a file claiming to be ann's, owned by USER
    conn = sqlite3.connect(forged)
    conn.executescript(store.EVENTS_SCHEMA)
    conn.execute("INSERT INTO audit (ts,user,tool,args,ok,chars_out) VALUES ('x','ann','forged','{}',1,0)")
    conn.commit()
    conn.close()
    assert "forged" not in [r["tool"] for r in store.read_events("audit")]


def test_review_requests_private_to_requester_and_bsr(api):
    api.post("/api/tools/request_bsr_review", json={"arguments": {"lab_id": "lab-a", "summary": "Help with design"}})
    assert len(editing.inbox("bsrstaff")["reviews"]) == 1
    assert editing.inbox("ann")["reviews"] == []


def test_suggestions_on_personal_documents_stay_personal(api):
    rel = api.post("/api/labs/lab-a/docs", json={"category": "notes", "title": "Mine", "scope": "personal"}).json()["rel_path"]
    doc_id = f"doc:lab-a/~{USER}/notes/mine"
    r = api.post("/api/tools/suggest_context_edit", json={"arguments": {"lab_id": "lab-a", "suggestion": "add X", "doc_id": doc_id}}).json()
    assert not r["is_error"], r
    folder = config.KNOWLEDGE_DIR / "lab-a" / "members" / USER / "_suggestions"
    assert len(list(folder.glob("*.yaml"))) == 1
    assert [s["doc_id"] for s in editing.inbox(USER)["suggestions"]] == [doc_id]
    assert editing.inbox("ann")["suggestions"] == []
    assert rel


# ---- connecting ---------------------------------------------------------------------------------

def test_connect_info_gives_a_ready_claude_desktop_setup(api):
    info = api.get("/api/connect").json()
    server = info["claude_desktop"]["mcpServers"]["BSR Agent"]
    assert server["command"] == "ssh"
    assert server["args"][-2].startswith(f"{USER}@") and server["args"][-1].endswith("env/bin/codespringhub")
    assert "shared with BSR" in info["notice"]
    # first connection must not stop at SSH's "are you sure?" question, which nobody can answer
    assert "StrictHostKeyChecking=accept-new" in server["args"] and "BatchMode=yes" in server["args"]


def test_setup_command_adds_the_hub_without_losing_other_settings(api, tmp_path):
    import json as _json, subprocess
    info = api.get("/api/connect").json()
    cfg = tmp_path / "Library/Application Support/Claude/claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(_json.dumps({"preferences": {"x": 1}, "mcpServers": {"other": {"command": "y"}, "codespringhub": {"command": "old"}}}))
    real_helper = Path(__file__).resolve().parents[1] / "laptop" / "connect.py"
    (config.HUB_HOME / "laptop").mkdir(exist_ok=True)
    (config.HUB_HOME / "laptop" / "connect.py").write_text(real_helper.read_text())
    # stand-in for scp: "copy" the helper from the cluster path named in the command
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    (fakebin / "scp").write_text('#!/bin/bash\nsrc="${@: -2:1}"; cp "${src#*:}" "${@: -1}"\n')
    (fakebin / "scp").chmod(0o755)
    subprocess.run(["bash", "-c", info["setup_mac"]], env={"HOME": str(tmp_path), "PATH": f"{fakebin}:/usr/bin:/bin"},
                   check=True)
    c = _json.loads(cfg.read_text())
    assert c["preferences"] == {"x": 1} and set(c["mcpServers"]) == {"other", "BSR Agent"}
    helper = tmp_path / ".codespringhub" / "connect.py"
    assert helper.read_text() == (config.HUB_HOME / "laptop" / "connect.py").read_text()
    direct = info["claude_desktop"]["mcpServers"]["BSR Agent"]
    assert c["mcpServers"]["BSR Agent"]["args"] == [str(helper), "--", direct["command"], *direct["args"]]
    assert cfg.with_name(cfg.name + ".bak").exists()
    # Claude Code on the laptop gets the "use BSR's Agent first" preference, once, keeping the person's own lines.
    md = tmp_path / ".claude" / "CLAUDE.md"
    assert info["preference"] in md.read_text()
    md.write_text(md.read_text() + "- my own rule\n")
    subprocess.run(["bash", "-c", info["setup_mac"]], env={"HOME": str(tmp_path), "PATH": f"{fakebin}:/usr/bin:/bin"},
                   check=True)
    text = md.read_text()
    assert text.count(info["preference"]) == 1 and "- my own rule" in text
    assert info["preference"] in info["setup_windows"] and "BSR''s" not in info["setup_windows"]
    assert "$env:APPDATA" in info["setup_windows"] and "WriteAllText" in info["setup_windows"]


def test_main_repo_never_holds_lab_knowledge():
    ignore = (config.HUB_HOME / ".gitignore").read_text()
    assert "knowledge/*/" in ignore and "!knowledge/bsr/" in ignore and "members/" in ignore


def test_connect_page_can_prepare_a_researchers_setup(api):
    """BSR types the researcher's username; every command uses theirs, not the signed-in user's."""
    import pwd as _pwd
    other = _pwd.getpwuid(0).pw_name  # an account that exists on every machine
    info = api.get("/api/connect", params={"for_user": other}).json()
    server = info["claude_desktop"]["mcpServers"]["BSR Agent"]
    assert info["user"] == other and server["args"][-2].startswith(f"{other}@")
    assert f"{other}@" in info["portal_command"] and str(info["portal_port"]) in info["portal_command"]
    assert USER not in info["portal_command"].split("@")[0]
    assert api.get("/api/connect", params={"for_user": "no-such-person-xyz"}).status_code == 404
    assert api.get("/api/connect", params={"for_user": "bad name; rm"}).status_code == 400
