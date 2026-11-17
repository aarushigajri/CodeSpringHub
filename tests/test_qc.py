"""Per-step quality numbers read from a run's own reports (qc.py), and how analysis_status reports them."""

import io
import json
import zipfile

from codespringhub import qc, runs
from conftest import USER
from test_runs import _no_symbols, _start, suite  # noqa: F401  (fixture)

CUTADAPT = """This is cutadapt 4.4 with Python 3.10.8
=== Summary ===

Total read pairs processed:          1,000,000
  Read 1 with adapter:                  27,655 (2.8%)
  Read 2 with adapter:                  36,839 (3.7%)

== Read fate breakdown ==
Pairs that were too short:                  51 (0.0%)
Pairs written (passing filters):       {kept} ({pct}%)

Total written (filtered):    151,678,837 bp (99.8%)
"""

STAR_LOG = """                          Number of input reads |\t999949
                        Uniquely mapped reads % |\t{unique}%
             % of reads mapped to multiple loci |\t5.16%
             % of reads mapped to too many loci |\t2.87%
       % of reads unmapped: too many mismatches |\t0.00%
                 % of reads unmapped: too short |\t10.42%
                     % of reads unmapped: other |\t0.13%
                      Mismatch rate per base, % |\t0.20%
"""

FC_SUMMARY = "Status\tsample.bam\nAssigned\t{assigned}\nUnassigned_MultiMapping\t100000\nUnassigned_NoFeatures\t50000\n"

SCREEN = ("#Fastq_screen version: 0.15.1\n"
          "Genome\t#Reads_processed\t#Unmapped\t%Unmapped\n"
          "Mouse\t99994\t6346\t6.35\nrRNA\t99994\t97951\t97.96\n")


def _fastqc_zip(path, fails):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        mods = ["Basic Statistics", "Per base sequence quality", "Per base sequence content", "Adapter Content"]
        zf.writestr("x_fastqc/summary.txt", "".join(
            f"{'FAIL' if m in fails else 'PASS'}\t{m}\tx.fastq.gz\n" for m in mods))
        zf.writestr("x_fastqc/fastqc_data.txt", "%GC\t45\n")
    path.write_bytes(buf.getvalue())


def make_results(root, low_sample=False):
    """A results folder laid out like CodeSpringLab's, with two samples (the second can look poor)."""
    (root / "log").mkdir(parents=True)
    (root / "log" / "codespring_hub_spec.json").write_text(json.dumps({"species": "mouse"}))
    fq = root / "data" / "fastqc_cutadapt"
    fq.mkdir(parents=True)
    for i, s in enumerate(("control_1", "treated_1")):
        poor = low_sample and i == 1
        (root / "log" / f"output_cutadapt_{s}.txt").write_text(
            CUTADAPT.format(kept="800,000" if poor else "999,949", pct="80.0" if poor else "100.0"))
        star = root / "data" / "star" / s
        star.mkdir(parents=True)
        (star / f"{s}Log.final.out").write_text(STAR_LOG.format(unique="55.10" if poor else "81.42"))
        fc = root / "data" / "featurecounts" / s
        fc.mkdir(parents=True)
        (fc / f"{s}_counts.txt.summary").write_text(FC_SUMMARY.format(assigned=100000 if poor else 750000))
        stem = s.replace("_", "") + "_R1_001"
        _fastqc_zip(fq / f"{stem}_fastqc.zip", ["Per base sequence content"] + (["Adapter Content"] if poor else []))
        (fq / f"{stem}_screen.txt").write_text(SCREEN)
    de = root / "data" / "deseq2"
    de.mkdir(parents=True)
    (de / "DEG_fdr0.05_treated_vs_control(ref).txt").write_text(
        "baseMean\tlog2FoldChange\tlfcSE\tpvalue\tpadj\n"
        "S100a9\t575\t2.6\t0.1\t1e-169\t3e-165\nGpx1\t300\t-1.2\t0.1\t1e-10\t1e-8\nNgp\t200\t3.1\t0.1\t1e-9\t1e-7\n")
    gs = root / "data" / "gseapy" / "treated_vs_control" / "MSigDB_Hallmark_2020"
    gs.mkdir(parents=True)
    (gs / "report.gseapy.MSigDB_Hallmark_2020.csv").write_text(
        "Name,Term,ES,NES,NOM p-val,FDR q-val\n"
        "gsea,Myc Targets V1,-0.5,-2.3,0,0.0\ngsea,Complement,0.6,2.3,0,0.03\ngsea,Apoptosis,0.2,1.0,0.4,0.6\n")
    return root


def test_each_step_reports_its_key_numbers(tmp_path):
    res = make_results(tmp_path / "res")
    assert qc.step_qc(res, "Cutadapt")["headline"] == "100.0% of read pairs kept; adapters in 2.8-3.7%"
    star = qc.step_qc(res, "STAR")
    assert star["headline"] == "81.4% uniquely mapped" and star["samples"][0]["multi_mapped_pct"] == 5.16
    assert qc.step_qc(res, "featureCounts")["headline"] == "83.3% of reads assigned to genes"
    fastqc = qc.step_qc(res, "FastQC")
    assert fastqc["headline"] == "no unexpected FastQC failures; 93.7% mouse"
    assert fastqc["usual_for_rnaseq"] == ["Per base sequence content"] and not fastqc["flags"]
    de = qc.step_qc(res, "DESeq2")
    assert de["headline"] == "3 genes at FDR 0.05 (2 up, 1 down)"
    assert de["comparisons"][0]["top_up"] == ["Ngp", "S100a9"] and de["comparisons"][0]["top_down"] == ["Gpx1"]
    gsea = qc.step_qc(res, "GSEA")
    assert gsea["headline"] == "2 Hallmark 2020 sets at FDR 0.25"
    assert gsea["libraries"][0]["top_up"] == ["Complement"] and gsea["libraries"][0]["top_down"] == ["Myc Targets V1"]


def test_samples_that_look_off_are_flagged(tmp_path):
    res = make_results(tmp_path / "res", low_sample=True)
    assert qc.step_qc(res, "Cutadapt")["flags"] == ["treated_1 kept under 90% of its reads after trimming"]
    assert qc.step_qc(res, "STAR")["flags"] == ["treated_1 has under 70% uniquely mapped reads"]
    assert qc.step_qc(res, "STAR")["headline"] == "55.1-81.4% uniquely mapped"
    assert qc.step_qc(res, "featureCounts")["flags"] == ["treated_1 has under 60% of reads assigned to genes"]
    assert qc.step_qc(res, "FastQC")["flags"] == ["FastQC failed 'Adapter Content' in treated1_R1_001"]


def test_missing_or_unknown_steps_give_nothing(tmp_path):
    assert qc.step_qc(tmp_path / "nowhere", "STAR") is None
    assert qc.step_qc(tmp_path, "STAR") is None
    assert qc.step_qc(make_results(tmp_path / "res"), "RSEM") is None


def test_every_run_gets_the_same_checks_against_bsr_guides(tmp_path):
    res = make_results(tmp_path / "res")
    star = qc.checks("STAR", qc.step_qc(res, "STAR"))
    assert star[0] == {"check": "Uniquely mapped", "result": "81.4%", "guide": "70% or more", "status": "OK"}
    assert [r["check"] for r in star] == ["Uniquely mapped", "Mapped to several places", "Unmapped"]
    fq = qc.step_qc(res, "FastQC")
    assert [r["check"] for r in qc.checks("FastQC", fq, fq["species"])] == [
        "Unexpected FastQC failures", "Reads from mouse (FastQ Screen)", "rRNA (FastQ Screen)", "GC content"]
    assert qc.checks("DESeq2", qc.step_qc(res, "DESeq2"))[0]["result"] == "3 (2 up, 1 down)"
    poor = make_results(tmp_path / "poor", low_sample=True)
    rows = qc.checks("STAR", qc.step_qc(poor, "STAR"))
    assert rows[0]["status"] == "Check" and qc.verdict(rows) == "Worth a look"


def test_status_shows_one_line_per_step_then_the_standard_report(suite, tmp_path):  # noqa: F811
    run_id = _start(suite)
    res = make_results(tmp_path / "res")
    status = suite / "CodeSpringApp" / "bin" / "status.json"
    status.write_text(json.dumps({"state": "running", "results_dir": str(res), "steps": [
        {"step": "Cutadapt", "state": "complete"}, {"step": "STAR", "state": "running", "samples_done": 1,
                                                    "samples_total": 2}, {"step": "featureCounts", "state": "waiting"}]}))
    st = runs.analysis_status(USER, run_id, wait_seconds=0)
    assert st["just_finished"] == ["Cutadapt"]
    assert st["say"] == ("**Step 1 of 3 done: Trimming (Cutadapt).** Read pairs kept after trimming: 100.0% "
                         "(BSR guide 90% or more). Looks good.")
    assert "quality" not in st and "table" not in st  # the payload stays small: say is all Claude shows
    assert _no_symbols(json.dumps(st, ensure_ascii=False))
    # Asked how it's going: the status table and the quality so far.
    again = runs.analysis_status(USER, run_id)
    assert again["just_finished"] == [] and "| 2 | Alignment | STAR | Running (1 of 2 samples) |  |" in again["say"]
    assert "| Trimming | Input read pairs per sample | 1.00 M |  |  |" in again["say"]
    # Following and nothing changed: nothing to show.
    quiet = runs.analysis_status(USER, run_id, wait_seconds=1)
    assert quiet["say"] == "" and "show nothing" in quiet["next"]
    status.write_text(json.dumps({"state": "complete", "results_dir": str(res), "steps": [
        {"step": "Cutadapt", "state": "complete"}, {"step": "STAR", "state": "complete"},
        {"step": "featureCounts", "state": "complete"}]}))
    done = runs.analysis_status(USER, run_id)
    assert done["just_finished"] == ["STAR", "featureCounts"]
    assert done["say"].startswith(f"**All 3 steps are done.** Your results are in `{res}`.")
    assert "| Alignment | Uniquely mapped | 81.4% | 70% or more | OK |" in done["say"]
    assert "| Gene counts | Reads assigned to genes |" in done["say"]
    assert "Need help? Contact BSR support" in done["say"] and "analysis_report" in done["next"]
