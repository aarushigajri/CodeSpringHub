"""Quality numbers from a BSR RNA-seq run's own output, step by step, for BSR's Agent to report.

When a step finishes, the Hub reads the small text reports that step leaves in the run's results
folder (never the reads, BAMs or counts themselves) and turns them into a few numbers per sample,
a short headline for the status table and plain-word flags where a number looks off.

Where each step's numbers come from (CodeSpringLab's layout under <results>/):
  Cutadapt       log/output_cutadapt_<sample>.txt            reads in, reads with adapter, kept
  FastQC         data/fastqc_cutadapt/*_fastqc.zip, *_screen.txt   module PASS/WARN/FAIL, FastQ Screen
  STAR           data/star/<sample>/<sample>Log.final.out    uniquely / multi-mapped, unmapped
  featureCounts  data/featurecounts/<sample>/*_counts.txt.summary   reads assigned to genes
  DESeq2         data/deseq2*/DEG_fdr0.05_*.txt               genes at FDR 0.05, up and down
  GSEA           data/gseapy/<comparison>/<set>/report.gseapy.*.csv   gene sets at FDR 0.25

The thresholds below are rough rules of thumb for bulk RNA-seq, not pass/fail gates. They only
decide when the Agent points something out; BSR can tune them here.
"""

from __future__ import annotations

import csv
import re
import zipfile
from pathlib import Path

# Rules of thumb (percent). A sample below a "min" or above a "max" gets a flag.
KEPT_AFTER_TRIMMING_MIN = 90.0
UNIQUELY_MAPPED_MIN = 70.0
ASSIGNED_TO_GENES_MIN = 60.0
SPECIES_SCREEN_MIN = 70.0
RRNA_SCREEN_MAX = 10.0
GSEA_FDR = 0.25
# FastQC modules that commonly warn or fail on good RNA-seq libraries, so they aren't flagged.
FASTQC_EXPECTED_FOR_RNASEQ = {"Per base sequence content", "Sequence Duplication Levels",
                              "Overrepresented sequences", "Sequence Length Distribution"}

SPECIES_SCREEN_NAME = {"mouse": "Mouse", "human": "Human"}


def _num(text: str) -> float | None:
    m = re.search(r"-?[\d,]*\.?\d+", text or "")
    return float(m.group(0).replace(",", "")) if m else None


def _pct(a: float, b: float) -> float | None:
    return round(100.0 * a / b, 1) if b else None


def _range(values: list[float], unit: str = "%") -> str:
    values = [v for v in values if v is not None]
    if not values:
        return ""
    lo, hi = min(values), max(values)
    return f"{lo:.1f}{unit}" if abs(hi - lo) < 0.05 else f"{lo:.1f}-{hi:.1f}{unit}"


def _below(rows: list[dict], key: str, limit: float) -> list[str]:
    return [r["sample"] for r in rows if r.get(key) is not None and r[key] < limit]


# ---- one parser per step -------------------------------------------------------------------------

def cutadapt(results: Path) -> dict | None:
    rows = []
    for log in sorted((results / "log").glob("output_cutadapt_*.txt")):
        text = log.read_text(errors="replace")
        total = re.search(r"Total read pairs processed:\s+([\d,]+)", text) or \
            re.search(r"Total reads processed:\s+([\d,]+)", text)
        if not total:
            continue
        paired = "read pairs" in total.group(0)
        kept = re.search(r"(?:Pairs|Reads) written \(passing filters\):\s+([\d,]+)", text)
        r1 = re.search(r"Read 1 with adapter:\s+[\d,]+ \(([\d.]+)%\)", text) or \
            re.search(r"Reads with adapters:\s+[\d,]+ \(([\d.]+)%\)", text)
        r2 = re.search(r"Read 2 with adapter:\s+[\d,]+ \(([\d.]+)%\)", text)
        bp = re.search(r"Total written \(filtered\):\s+[\d,]+ bp \(([\d.]+)%\)", text)
        n = _num(total.group(1))
        rows.append({
            "sample": log.stem.removeprefix("output_cutadapt_"),
            "reads_in": int(n), "unit": "read pairs" if paired else "reads",
            "kept_pct": _pct(_num(kept.group(1)), n) if kept else None,
            "adapter_r1_pct": float(r1.group(1)) if r1 else None,
            "adapter_r2_pct": float(r2.group(1)) if r2 else None,
            "bases_kept_pct": float(bp.group(1)) if bp else None,
        })
    if not rows:
        return None
    flags = [f"{s} kept under {KEPT_AFTER_TRIMMING_MIN:.0f}% of its reads after trimming"
             for s in _below(rows, "kept_pct", KEPT_AFTER_TRIMMING_MIN)]
    adapters = [r["adapter_r1_pct"] for r in rows] + [r["adapter_r2_pct"] for r in rows]
    headline = f"{_range([r['kept_pct'] for r in rows])} of {rows[0]['unit']} kept"
    if any(a is not None for a in adapters):
        headline += f"; adapters in {_range(adapters)}"
    return {"samples": rows, "headline": headline, "flags": flags}


def fastqc(results: Path, species: str = "") -> dict | None:
    folder = results / "data" / "fastqc_cutadapt"
    rows = []
    for z in sorted(folder.glob("*_fastqc.zip")):
        name = z.name.removesuffix("_fastqc.zip")
        try:
            with zipfile.ZipFile(z) as zf:
                summary = next((n for n in zf.namelist() if n.endswith("/summary.txt")), None)
                data = next((n for n in zf.namelist() if n.endswith("/fastqc_data.txt")), None)
                lines = zf.read(summary).decode(errors="replace").splitlines() if summary else []
                info = zf.read(data).decode(errors="replace") if data else ""
        except (OSError, zipfile.BadZipFile, KeyError):
            continue
        modules = [ln.split("\t")[:2] for ln in lines if ln.count("\t") >= 2]
        gc = re.search(r"^%GC\t(\d+)", info, re.M)
        row = {"sample": name,
               "fail": [m for s, m in modules if s == "FAIL"],
               "warn": [m for s, m in modules if s == "WARN"],
               "gc_pct": int(gc.group(1)) if gc else None}
        screen = folder / f"{name}_screen.txt"
        if screen.is_file():
            row.update(_screen(screen, species))
        rows.append(row)
    if not rows:
        return None
    unexpected = sorted({m for r in rows for m in r["fail"] if m not in FASTQC_EXPECTED_FOR_RNASEQ})
    flags = [f"FastQC failed '{m}' in " + ", ".join(r["sample"] for r in rows if m in r["fail"]) for m in unexpected]
    flags += [f"{r['sample']}: only {r['species_pct']:.1f}% of screened reads hit the {species} genome"
              for r in rows if r.get("species_pct") is not None and r["species_pct"] < SPECIES_SCREEN_MIN]
    flags += [f"{r['sample']}: {r['rrna_pct']:.1f}% of screened reads look like rRNA"
              for r in rows if r.get("rrna_pct") is not None and r["rrna_pct"] > RRNA_SCREEN_MAX]
    headline = ("no unexpected FastQC failures" if not unexpected
                else f"FastQC failures: {', '.join(unexpected)}")
    species_hits = [r.get("species_pct") for r in rows]
    if any(v is not None for v in species_hits):
        headline += f"; {_range(species_hits)} {species or 'expected species'}"
    expected = sorted({m for r in rows for m in r["fail"] + r["warn"] if m in FASTQC_EXPECTED_FOR_RNASEQ})
    return {"samples": rows, "headline": headline, "flags": flags,
            "usual_for_rnaseq": expected,
            "note": ("Warnings or failures on the modules in usual_for_rnaseq are normal for RNA-seq libraries "
                     "(random-hexamer priming, highly expressed genes, trimmed read lengths).") if expected else ""}


def _screen(path: Path, species: str) -> dict:
    """FastQ Screen: share of reads hitting the expected genome, and rRNA."""
    out = {}
    target = SPECIES_SCREEN_NAME.get((species or "").lower())
    for ln in path.read_text(errors="replace").splitlines():
        cols = ln.split("\t")
        if len(cols) < 4 or cols[0].startswith("#") or cols[0] == "Genome":
            continue
        unmapped = _num(cols[3])
        if unmapped is None:
            continue
        if target and cols[0] == target:
            out["species_pct"] = round(100.0 - unmapped, 1)
        elif cols[0] == "rRNA":
            out["rrna_pct"] = round(100.0 - unmapped, 1)
    return out


def star(results: Path) -> dict | None:
    rows = []
    for log in sorted((results / "data" / "star").glob("*/*Log.final.out")):
        fields = {}
        for ln in log.read_text(errors="replace").splitlines():
            if "|" in ln:
                k, v = ln.split("|", 1)
                fields[k.strip()] = v.strip()
        if "Uniquely mapped reads %" not in fields:
            continue
        too_short = _num(fields.get("% of reads unmapped: too short", ""))
        other = _num(fields.get("% of reads unmapped: other", ""))
        mismatch = _num(fields.get("% of reads unmapped: too many mismatches", ""))
        rows.append({
            "sample": log.parent.name,
            "input_reads": int(_num(fields.get("Number of input reads", "0")) or 0),
            "uniquely_mapped_pct": _num(fields["Uniquely mapped reads %"]),
            "multi_mapped_pct": _num(fields.get("% of reads mapped to multiple loci", "")),
            "too_many_loci_pct": _num(fields.get("% of reads mapped to too many loci", "")),
            "unmapped_pct": round(sum(v for v in (too_short, other, mismatch) if v is not None), 2),
            "unmapped_too_short_pct": too_short,
            "mismatch_rate_pct": _num(fields.get("Mismatch rate per base, %", "")),
        })
    if not rows:
        return None
    flags = [f"{s} has under {UNIQUELY_MAPPED_MIN:.0f}% uniquely mapped reads"
             for s in _below(rows, "uniquely_mapped_pct", UNIQUELY_MAPPED_MIN)]
    return {"samples": rows, "headline": f"{_range([r['uniquely_mapped_pct'] for r in rows])} uniquely mapped",
            "flags": flags}


def featurecounts(results: Path) -> dict | None:
    rows = []
    for summ in sorted((results / "data" / "featurecounts").glob("*/*_counts.txt.summary")):
        counts = {}
        for ln in summ.read_text(errors="replace").splitlines()[1:]:
            cols = ln.split("\t")
            if len(cols) >= 2 and (n := _num(cols[1])) is not None:
                counts[cols[0]] = n
        total = sum(counts.values())
        if not total:
            continue
        rows.append({
            "sample": summ.parent.name,
            "assigned": int(counts.get("Assigned", 0)),
            "assigned_pct": _pct(counts.get("Assigned", 0), total),
            "multimapping_pct": _pct(counts.get("Unassigned_MultiMapping", 0), total),
            "no_feature_pct": _pct(counts.get("Unassigned_NoFeatures", 0), total),
            "ambiguous_pct": _pct(counts.get("Unassigned_Ambiguity", 0), total),
        })
    if not rows:
        return None
    flags = [f"{s} has under {ASSIGNED_TO_GENES_MIN:.0f}% of reads assigned to genes"
             for s in _below(rows, "assigned_pct", ASSIGNED_TO_GENES_MIN)]
    return {"samples": rows, "headline": f"{_range([r['assigned_pct'] for r in rows])} of reads assigned to genes",
            "flags": flags}


def deseq2(results: Path) -> dict | None:
    out = []
    for f in sorted((results / "data").glob("deseq2*/DEG_fdr0.05_*.txt")):
        with f.open(errors="replace") as fh:
            header = fh.readline().rstrip("\n").split("\t")
            if "log2FoldChange" not in header:
                continue
            col = header.index("log2FoldChange") + 1  # rows carry the gene name before the header's columns
            genes = []
            for ln in fh:
                cols = ln.rstrip("\n").split("\t")
                if len(cols) > col and (lfc := _num(cols[col])) is not None:
                    genes.append((cols[0], lfc))
        comparison = f.stem.removeprefix("DEG_fdr0.05_").replace("(ref)", "")
        up = [g for g in genes if g[1] > 0]
        down = [g for g in genes if g[1] < 0]
        out.append({"comparison": comparison, "significant": len(genes), "up": len(up), "down": len(down),
                    "top_up": [g for g, _ in sorted(up, key=lambda x: -x[1])[:5]],
                    "top_down": [g for g, _ in sorted(down, key=lambda x: x[1])[:5]]})
    if not out:
        return None
    c = out[0]
    headline = f"{c['significant']} genes at FDR 0.05 ({c['up']} up, {c['down']} down)"
    flags = [f"No genes reach FDR 0.05 for {x['comparison'].replace('_', ' ')}" for x in out if not x["significant"]]
    return {"comparisons": out, "headline": headline, "flags": flags}


def gsea(results: Path) -> dict | None:
    out = []
    for f in sorted((results / "data" / "gseapy").glob("*/*/report.gseapy.*.csv")):
        try:
            with f.open(newline="", errors="replace") as fh:
                rows = list(csv.DictReader(fh))
        except OSError:
            continue
        hits = [(r.get("Term", ""), _num(r.get("NES", ""))) for r in rows
                if (q := _num(r.get("FDR q-val", ""))) is not None and q < GSEA_FDR]
        hits = [h for h in hits if h[1] is not None]
        out.append({"comparison": f.parent.parent.name, "gene_set_library": f.parent.name,
                    "significant": len(hits),
                    "top_up": [t for t, _ in sorted((h for h in hits if h[1] > 0), key=lambda x: -x[1])[:3]],
                    "top_down": [t for t, _ in sorted((h for h in hits if h[1] < 0), key=lambda x: x[1])[:3]]})
    if not out:
        return None
    headline = "; ".join(f"{x['significant']} {x['gene_set_library'].replace('MSigDB_', '').replace('_', ' ')} "
                         f"sets at FDR {GSEA_FDR}" for x in out)
    return {"libraries": out, "headline": headline, "flags": []}


PARSERS = {"Cutadapt": cutadapt, "FastQC": fastqc, "STAR": star, "featureCounts": featurecounts,
           "DESeq2": deseq2, "GSEA": gsea}


def species_of(results: Path) -> str:
    try:
        import json
        return json.loads((results / "log" / "codespring_hub_spec.json").read_text()).get("species", "")
    except (OSError, ValueError):
        return ""


def step_qc(results_dir, step: str) -> dict | None:
    """Quality numbers for one finished step, or None if the step leaves none we read (or none yet)."""
    if not results_dir or step not in PARSERS:
        return None
    results = Path(results_dir)
    if not results.is_dir():
        return None
    try:
        if step == "FastQC":
            return fastqc(results, species_of(results))
        return PARSERS[step](results)
    except (OSError, ValueError, UnicodeError):
        return None
