"""Core facilities (kind: core): a shared section for everyone using the Hub, an internal section
for the core's staff only, and templates for cores that aren't on the Hub yet."""

import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from codespringhub import acl, cli, config, editing, knowledge
from codespringhub import hub as hubsvc
from codespringhub.errors import Forbidden, HubError, NotFound
from codespringhub.identity import LocalLinkAuth
from codespringhub.web import create_app

from conftest import GROUPS, REAL_GROUP, USER


def _core(kdir: Path, core_id="seq", group="seq-grp", name="Sequencing Core"):
    d = kdir / core_id
    for sub in ("shared/protocols", "internal/notes", "members", "private"):
        (d / sub).mkdir(parents=True)
    (d / "lab.yaml").write_text(yaml.safe_dump({
        "kind": "core", "name": name, "unix_group": group, "website": "https://example.org/seq",
        "contacts": [{"name": "Sam Seq", "email": "sam@cshl.edu"}], "projects": []}))
    (d / "shared" / "context.md").write_text(f"# {name}\n\nSubmit libraries in 1.5 ml tubes.\n")
    (d / "shared" / "protocols" / "submission.md").write_text("# Sample submission\n\nPool barcodes evenly.\n")
    (d / "internal" / "context.md").write_text("# Internal\n\nNovaSeq flow cell pricing draft.\n")
    (d / "internal" / "notes" / "instrument.md").write_text("# Instrument log\n\nThe sequencer jammed on Tuesday.\n")
    (d / "shared" / "datasets.yaml").write_text("datasets: []\n")
    (d / "internal" / "datasets.yaml").write_text("datasets: []\n")
    return d


@pytest.fixture
def seq(hub):
    d = _core(config.KNOWLEDGE_DIR)
    GROUPS.update({"sam": {"seq-grp"}, "lea": {"seq-grp"}, "eve": {"nobody-grp"}})
    GROUPS[USER] = GROUPS[USER] | {"seq-grp"}
    (config.KNOWLEDGE_DIR / "roles.yaml").write_text("labs:\n  lab-a:\n    pis: [pia]\n  seq:\n    pis: [lea]\n")
    return d


def ids(user, query):
    return [h["doc_id"] for h in hubsvc.search(user, query)]


def test_everyone_sees_a_cores_shared_section_and_only_staff_its_internal_one(seq):
    for user in ("ann", "carl", "bsrstaff"):
        assert ids(user, "barcodes") == ["doc:seq/shared/protocols/submission"]
        assert ids(user, "sequencer jammed") == [] and ids(user, "pricing") == []
        with pytest.raises(NotFound):
            hubsvc.get_document(user, "doc:seq/internal/notes/instrument")
        ctx = hubsvc.lab_context(user, "seq")
        assert "1.5 ml tubes" in ctx["context"] and "internal_context" not in ctx
    assert ids("sam", "sequencer jammed") == ["doc:seq/internal/notes/instrument"]
    staff_ctx = hubsvc.lab_context("sam", "seq")
    assert "pricing draft" in staff_ctx["internal_context"] and "Never repeat internal" in staff_ctx["note"]
    # Not a Hub user at all: nothing.
    with pytest.raises(NotFound):
        hubsvc.lab_context("eve", "seq")


def test_listing_labs_and_cores(seq):
    rows = {r["lab_id"]: r for r in hubsvc.list_labs("ann")}
    assert list(rows) == ["lab-a", "bsr", "seq"]  # own labs, then BSR, then the other cores
    assert rows["seq"]["kind"] == "core" and rows["seq"]["sees"] == "shared" and rows["seq"]["role"] == "user"
    assert rows["seq"]["contacts"][0]["email"] == "sam@cshl.edu" and not rows["seq"]["can_edit"]
    staff = {r["lab_id"]: r for r in hubsvc.list_labs("lea")}
    assert staff["seq"]["role"] == "lead" and staff["seq"]["sees"] == "shared and internal"
    assert {r["lab_id"]: r for r in hubsvc.list_labs("sam")}["seq"]["role"] == "staff"


def test_only_staff_edit_and_suggestions_point_outsiders_to_the_core(seq):
    with pytest.raises(Forbidden, match="Sam Seq"):
        hubsvc.suggest_edit("ann", "seq", "Mention the 2 nM loading concentration")
    with pytest.raises(NotFound):
        editing.save_document("ann", "seq", "shared/protocols/submission.md", "x")
    with pytest.raises(NotFound):  # BSR doesn't get into another core's internal section either
        editing.read_document_file("bsrstaff", "seq", "internal/notes/instrument.md")
    assert editing.read_document_file("ann", "seq", "shared/protocols/submission.md")["can_edit"] is False
    assert editing.read_document_file(USER, "seq", "shared/protocols/submission.md")["scope"] == "shared"


def test_staff_choose_shared_or_internal_when_creating(seq):
    shared = editing.create_document(USER, "seq", "protocols", "Library QC", "shared")
    internal = editing.create_document(USER, "seq", "notes", "Pricing", "lab")
    assert shared == "shared/protocols/library-qc.md" and internal == "internal/notes/pricing.md"
    assert "doc:seq/shared/protocols/library-qc" in ids("ann", "Library QC")
    assert not [i for i in ids("ann", "Pricing") if i.startswith("doc:seq/")]
    with pytest.raises(HubError, match="Only core facilities have a shared section"):
        editing.create_document(USER, "lab-a", "notes", "x", "shared")
    with pytest.raises(HubError, match="shared/ or internal/"):
        editing.save_document(USER, "seq", "protocols/loose.md", "x")


def test_shared_and_internal_datasets(seq, hub):
    body = {"title": "Example run", "path": str(hub / "data" / "demo-rnaseq"), "scope": "shared"}
    shared = editing.save_dataset(USER, "seq", body)
    internal = editing.save_dataset(USER, "seq", body | {"title": "QC archive", "scope": "lab"})
    assert shared == "ds:seq/shared/example-run" and internal == "ds:seq/internal/qc-archive"
    assert [d["id"] for d in hubsvc.list_datasets("carl", "seq")] == [shared]
    with pytest.raises(NotFound):
        hubsvc.get_dataset("carl", internal)
    assert {d["id"] for d in hubsvc.list_datasets("sam", "seq")} == {shared, internal}
    editing.delete_dataset(USER, "seq", shared)
    assert hubsvc.list_datasets("carl", "seq") == []


def test_editing_core_details_keeps_kind_and_contacts(seq):
    editing.update_lab(USER, "seq", "Sequencing Technologies", "DNA and RNA sequencing", [])
    data = yaml.safe_load((seq / "lab.yaml").read_text())
    assert data["kind"] == "core" and data["contacts"][0]["name"] == "Sam Seq" and data["unix_group"] == "seq-grp"


def test_portal_lists_cores_and_shows_shared_pages(seq):
    auth = LocalLinkAuth(new_link=True)
    api = TestClient(create_app(auth))
    api.headers["Authorization"] = f"Bearer {auth.token}"
    labs = {r["lab_id"]: r for r in api.get("/api/labs").json()}
    assert labs["seq"]["kind"] == "core" and labs["bsr"]["kind"] == "core"
    detail = api.get("/api/labs/seq").json()
    assert {d["rel_path"] for d in detail["documents_list"]} >= {"shared/protocols/submission.md", "internal/notes/instrument.md"}
    r = api.post("/api/labs/seq/docs", json={"category": "protocols", "title": "Pooling", "scope": "shared"})
    assert r.json()["rel_path"] == "shared/protocols/pooling.md"


def test_templates_are_kept_by_bsr_until_the_core_joins(hub, monkeypatch):
    monkeypatch.setattr(config, "BSR_GROUP", REAL_GROUP)
    GROUPS[USER] = GROUPS[USER] | {REAL_GROUP}
    GROUPS["bsrstaff"] = {REAL_GROUP}
    monkeypatch.setattr(cli, "ME", USER)
    monkeypatch.setattr(cli, "_hub_groups", lambda: [REAL_GROUP])
    cli.cmd_seed_cores(None)
    labs = knowledge.load_labs()
    seq = labs["sequencing"]
    assert seq.is_core and not seq.onboarded and "sequencing" in seq.website
    assert len([l for l in labs.values() if l.is_core]) == 18  # 17 templates + BSR
    # Everyone sees the starter page; BSR may edit it until the core joins.
    ctx = hubsvc.lab_context("ann", "sequencing")["context"]
    assert "DNA and RNA sequencing" in ctx and "isn't on CodeSpringHub yet" in ctx
    assert hubsvc.lab_context("bsrstaff", "sequencing")["internal_context"]
    assert (seq.dir / "shared" / ".git").exists() and (seq.dir / "internal" / ".git").exists()
    cli.cmd_seed_cores(None)  # running again changes nothing
    # Onboarding: the core's own group takes over, and BSR is out of its internal section.
    args = type("A", (), {"core_id": "sequencing", "group": REAL_GROUP, "name": None, "lead": [USER],
                          "website": None, "contact": ["Sam Seq <sam@cshl.edu>"]})()
    monkeypatch.setattr(config, "BSR_GROUP", "bsr-grp")
    GROUPS["bsrstaff"] = {"bsr-grp"}
    cli.cmd_add_core(args)
    seq = knowledge.load_labs()["sequencing"]
    assert seq.onboarded and seq.contacts == [{"name": "Sam Seq", "email": "sam@cshl.edu"}] and seq.pis == [USER]
    assert "internal_context" not in hubsvc.lab_context("bsrstaff", "sequencing")


# ---- on disk (GPFS NFSv4 where available; POSIX ACLs elsewhere) ------------------------------------

def _dir_for_acl():
    for cand in (os.environ.get("CODESPRINGHUB_GPFS_TESTDIR"), str(Path.home())):
        if cand and os.path.isdir(cand) and os.access(cand, os.W_OK) and acl.is_nfs4(Path(cand)):
            return cand
    return None


@pytest.mark.skipif(_dir_for_acl() is None, reason="needs a GPFS folder with NFSv4 ACLs (/grid)")
def test_gpfs_core_internal_is_staff_only_and_shared_is_readable_by_labs(monkeypatch):
    import grp as _grp
    real = sorted(n for n in (_grp.getgrgid(g).gr_name for g in os.getgroups()) if " " not in n)
    staff, reader = real[0], (real[1] if len(real) > 1 else None)
    gdir = Path(tempfile.mkdtemp(prefix=".hub-core-test-", dir=_dir_for_acl()))
    try:
        core = gdir / "core"
        core.mkdir()
        (core / "lab.yaml").write_text("kind: core\n")
        for sub in ("shared", "internal"):
            (core / sub).mkdir()
            (core / sub / "a.md").write_text("x\n")
        monkeypatch.setattr(config, "BSR_GROUP", staff)
        acl.grant_core(core, staff, [reader] if reader else [])
        sgid = __import__("grp").getgrnam(staff).gr_gid
        assert f"group:{sgid}:rwx" in acl.entries(core / "internal")
        assert all(e.endswith(":rwx") and str(sgid) in e for e in acl.entries(core / "internal"))
        assert acl.owning_group_entry(core / "internal") == "group::---"  # the folder's own group (BSR) is out
        if reader:
            rgid = __import__("grp").getgrnam(reader).gr_gid
            assert f"group:{rgid}:r-x" in acl.entries(core / "shared")
            assert f"default:group:{rgid}:r-x" in acl.entries(core / "shared")
            assert not any(str(rgid) in e for e in acl.entries(core / "internal"))
        (core / "shared" / "b.md").write_text("new\n")  # new files inherit
        if reader:
            assert any(e.startswith(f"group:{rgid}:r") for e in acl.entries(core / "shared" / "b.md"))
        assert acl.core_problems(core, staff) == []
    finally:
        shutil.rmtree(gdir, ignore_errors=True)
