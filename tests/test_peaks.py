"""Planning bulk ATAC-seq and ChIP-seq runs from Claude (the same plan -> confirm -> start as RNA-seq)."""

import pytest

from codespringhub import pipelines, runs
from codespringhub.errors import Forbidden, HubError

from conftest import GROUPS, USER
from test_runs import calls, plan, suite  # noqa: F401  (fixture)


def checked_spec(suite):  # noqa: F811
    return [c for c in calls(suite) if c["cmd"] == "check"][-1]["spec"]


def test_atac_and_chip_need_the_updated_codespringapp(suite):  # noqa: F811
    with pytest.raises(HubError, match="CodeSpringApp update"):
        plan(pipeline="atac")


def test_atac_plan_uses_the_apps_defaults_and_diffbind(suite):  # noqa: F811
    (suite / "CodeSpringApp" / "bin" / "peaks_runnable").write_text("")
    p = plan(pipeline="atac", run_mode="step")
    spec = checked_spec(suite)
    assert spec["pipeline"] == "atac" and spec["diffbind"] == {"column": "group", "reference": "x", "comparison": "y"}
    assert "adapter_r1" not in spec["trim"] and "macs2" not in spec  # the app's own defaults (Nextera, q 0.05)
    assert "feature" not in spec and "deseq2" not in spec and "gsea" not in spec
    assert "your ATAC-seq plan is ready" in p["preview"] and "DiffBind" in p["preview"]
    assert "hasn't been measured" in p["preview"]  # no RNA-seq size estimate passed off as ATAC's
    assert runs.start_analysis(USER, p["plan_id"], user_confirmed=True)["run_id"] == "atac/exp1"
    assert runs.analysis_status(USER, "atac/exp1")["state"]  # status, resume and cancel accept ATAC/ChIP run ids


def test_chip_options_and_refusals(suite):  # noqa: F811
    (suite / "CodeSpringApp" / "bin" / "peaks_runnable").write_text("")
    plan(pipeline="chip", peak_type="broad", macs2_qvalue=0.05, annotate=False)
    spec = checked_spec(suite)
    assert spec["macs2"] == {"qvalue": 0.05, "peak_type": "broad"} and spec["annotate"] is False
    with pytest.raises(HubError, match="RNA-seq options"):
        plan("x2", pipeline="chip", pathway_genesets=["MSigDB_Hallmark_2020"])
    with pytest.raises(HubError, match="pipeline must be"):
        plan("x3", pipeline="scrna")


def test_bsr_can_keep_a_peak_pipeline_internal(suite):  # noqa: F811
    (suite / "CodeSpringApp" / "bin" / "peaks_runnable").write_text("")
    GROUPS[USER] = GROUPS[USER] - {"bsr-grp"}  # an ordinary lab member
    pipelines.settings_path().write_text("default: internal\npipelines:\n  rnaseq: public\n  atac: internal\n  chip: public\n")
    with pytest.raises(Forbidden, match="request_bsr_review"):
        plan(pipeline="atac")
    assert plan("ok-chip", pipeline="chip")["plan_id"]


def test_analysis_options_for_atac_and_chip(suite):  # noqa: F811
    from test_server import call
    (suite / "CodeSpringApp" / "bin" / "peaks_runnable").write_text("")
    chip = call("analysis_options", assay="ChIP-seq")
    assert chip["pipeline"] == "chip" and chip["greeting"].startswith("Hi, this is BSR's Agent!") and "ChIP-seq" in chip["greeting"]
    assert [q["id"] for q in chip["questions"]] == ["project_name", "peak_type", "run_mode", "email_updates"]
    atac = call("analysis_options", assay="atac")
    assert [q["id"] for q in atac["questions"]] == ["project_name", "run_mode", "email_updates"] and "pipeline='atac'" in atac["next"]
    assert call("analysis_options", assay="scrna")["runnable"] is False
