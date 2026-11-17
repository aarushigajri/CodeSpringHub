"""Which CodeSpringApp pipelines each lab may use through the Hub (knowledge/bsr/shared/pipelines.yaml)."""

import pytest
from fastapi.testclient import TestClient

from codespringhub import config, pipelines, runs
from codespringhub.errors import Forbidden, HubError
from codespringhub.identity import LocalLinkAuth
from codespringhub.web import create_app

from conftest import GROUPS, USER
from test_runs import plan, suite  # noqa: F401  (fixture)

CATALOG = [{"id": "rnaseq", "name": "Bulk RNA-seq", "runnable_from_hub": True},
           {"id": "scrna", "name": "Single-cell RNA-seq", "runnable_from_hub": False},
           {"id": "newthing", "name": "Something new", "runnable_from_hub": True}]


def test_shipped_settings_bulk_tools_public_advanced_analysis_internal():
    s = pipelines.load()
    assert s["default"] == "internal"
    assert {p for p, r in s["pipelines"].items() if r["access"] == "public"} == {"rnaseq", "atac", "chip", "cutrun", "fetchngs"}
    assert s["pipelines"]["scrna"]["access"] == s["pipelines"]["sarek"]["access"] == "internal"


def test_labs_see_internal_pipelines_as_run_by_bsr():
    rows = {p["id"]: p for p in pipelines.annotate("carl", CATALOG)}
    assert rows["rnaseq"]["you_can_use"] and rows["rnaseq"]["runnable_from_hub"]
    assert not rows["scrna"]["you_can_use"] and "request_bsr_review" in rows["scrna"]["how"]
    # Not listed in the settings: BSR only, even if the app could run it from the Hub.
    assert rows["newthing"]["access"] == "internal" and not rows["newthing"]["runnable_from_hub"]
    assert all(p["you_can_use"] for p in pipelines.annotate("bsrstaff", CATALOG))


def test_bsr_can_open_an_internal_pipeline_to_chosen_labs():
    pipelines.save("bsrstaff", "scrna", "internal", ["lab-a"])
    assert pipelines.can_use("ann", "scrna") and not pipelines.can_use("carl", "scrna")
    text = pipelines.settings_path().read_text()
    assert text.startswith("# Who can use each CodeSpringApp pipeline") and "default: internal" in text
    pipelines.save("bsrstaff", "scrna", "public", ["lab-a"])
    assert pipelines.can_use("carl", "scrna") and pipelines.load()["pipelines"]["scrna"]["labs"] == []


def test_only_bsr_changes_access_and_bad_values_are_refused():
    with pytest.raises(Forbidden):
        pipelines.save("ann", "scrna", "public")
    with pytest.raises(HubError, match="public"):
        pipelines.save("bsrstaff", "scrna", "everyone")
    with pytest.raises(HubError, match="Not labs"):
        pipelines.save("bsrstaff", "scrna", "internal", ["no-such-lab", "bsr"])


def test_a_typo_in_the_file_fails_closed():
    path = pipelines.settings_path()
    path.write_text("default: internal\npipelines:\n  rnaseq: publik\n")
    assert pipelines.rule_for("rnaseq")["access"] == "internal" and not pipelines.can_use("carl", "rnaseq")


def test_planning_and_starting_respect_the_setting(suite):  # noqa: F811
    GROUPS[USER] = GROUPS[USER] - {"bsr-grp"}  # the tester as an ordinary lab member
    p = plan()
    pipelines.settings_path().write_text("default: internal\npipelines:\n  rnaseq: internal\n")
    with pytest.raises(Forbidden, match="request_bsr_review"):
        runs.start_analysis(USER, p["plan_id"], user_confirmed=True)  # changed after planning: still refused
    with pytest.raises(Forbidden):
        plan("e2")
    pipelines.settings_path().write_text("default: internal\npipelines:\n  rnaseq:\n    access: internal\n    labs: [lab-a]\n")
    assert plan("e3")["plan_id"]  # the tester is in lab-a


def test_portal_shows_and_saves_access():
    auth = LocalLinkAuth(new_link=True)
    api = TestClient(create_app(auth))
    api.headers["Authorization"] = f"Bearer {auth.token}"
    data = api.get("/api/pipelines").json()
    assert data["can_edit"] and {"rnaseq", "scrna"} <= {p["id"] for p in data["pipelines"]}
    api.put("/api/pipelines/sarek", json={"access": "internal", "labs": ["lab-a"]}).raise_for_status()
    assert pipelines.rule_for("sarek") == {"access": "internal", "labs": ["lab-a"]}
    assert api.put("/api/pipelines/sarek", json={"access": "maybe"}).status_code == 400
