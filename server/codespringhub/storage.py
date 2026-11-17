"""Disk space for BSR pipeline runs: how much is free where results go, and how much a run will need.

Claude tells the person both numbers at the start of every new project (check_storage) and again
in the plan (plan_analysis), and start_analysis refuses a run that clearly won't fit.

The bulk RNA-seq estimate scales with the size of the raw FASTQs, using what each step of BSR's
pipeline actually wrote for a measured run (hub_test_1, 17 Nov 2026: 12 paired-end mouse FASTQs,
620 MB gzipped -> 4.4 GB of results, about 7x). Per byte of gzipped input:

    cutadapt       1.05   trimmed FASTQs (gzipped), kept
    STAR           4.94   coordinate-sorted BAM + index (~1.0) and the transcriptome BAM from
                          --quantMode TranscriptomeSAM (~3.6, kept for RSEM), plus small logs
    featureCounts  0.96   per-sample count tables (one line per read pair)
    FastQC         0.09   reports
    + a fixed ~50 MB for count matrix, DESeq2, GSEApy and logs

Because that is one measured run, the estimate is given as a range around it: low 0.8x and high
1.3x the typical figure (~5.6x-9.1x the raw data). Read length, depth, multimapping and how well the
reads compress move it within that range; STAR also needs scratch space while it sorts. We recommend
having at least the high end free.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .errors import HubError

FASTQ_SUFFIXES = (".fastq.gz", ".fq.gz", ".fastq", ".fq")
# An uncompressed FASTQ is about 4x its gzipped size; the factors below are per gzipped byte.
UNCOMPRESSED_TO_GZ = 0.25

RNASEQ_FACTORS = {"Cutadapt": 1.05, "STAR": 4.94, "featureCounts": 0.96, "FastQC": 0.09}
RNASEQ_FIXED_BYTES = 50 * 1024**2
RANGE = (0.8, 1.3)  # low / high, as multiples of the typical estimate; recommend the high end free


def results_root() -> Path:
    """Where CodeSpringApp puts this person's results (its default, ~/csl_results)."""
    return Path(os.environ.get("CODESPRINGHUB_RESULTS_ROOT", Path.home() / "csl_results"))


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def free_space(path: Path) -> dict:
    """Free/total space on the filesystem holding `path` (or its nearest existing parent)."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    usage = shutil.disk_usage(p)
    return {
        "free_bytes": usage.free, "free": human(usage.free),
        "total": human(usage.total), "used_percent": round(100 * usage.used / usage.total, 1) if usage.total else None,
    }


def _location_label(path: Path) -> str:
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)


def raw_fastq_bytes(fastq_dir: Path, filenames: list[str] | None = None) -> tuple[int, int]:
    """(gzip-equivalent bytes, number of files) of the FASTQs a run will read. With `filenames`
    (from the design), only those files count; otherwise every FASTQ under the folder."""
    wanted = None
    if filenames:
        wanted = {n.strip() for f in filenames for lane in str(f).split(";") for n in lane.split(",") if n.strip()}
        wanted = {Path(n).name for n in wanted}
    total, count = 0, 0
    for f in Path(fastq_dir).rglob("*"):
        name = f.name
        if not name.endswith(FASTQ_SUFFIXES) or not f.is_file():
            continue
        if wanted is not None and name not in wanted:
            continue
        size = f.stat().st_size
        total += size if name.endswith(".gz") else int(size * UNCOMPRESSED_TO_GZ)
        count += 1
    return total, count


def estimate_rnaseq(raw_bytes: int, trim: bool = True) -> dict:
    steps = {k: v for k, v in RNASEQ_FACTORS.items() if trim or k != "Cutadapt"}
    by_step = {k: int(raw_bytes * v) for k, v in steps.items()}
    typical = sum(by_step.values()) + RNASEQ_FIXED_BYTES
    low, high = int(typical * RANGE[0]), int(typical * RANGE[1])
    factor = sum(steps.values())
    return {
        "range": f"{human(low)} to {human(high)}",
        "typical": human(typical),
        "low_bytes": low, "typical_bytes": typical, "high_bytes": high,
        "recommended_free": human(high),
        "typical_by_step": {k: human(v) for k, v in by_step.items()} | {"counts, DESeq2, GSEA, logs": human(RNASEQ_FIXED_BYTES)},
        "basis": (f"BSR's standard bulk RNA-seq pipeline writes about {factor * RANGE[0]:.1f}x-{factor * RANGE[1]:.1f}x "
                  f"the gzipped FASTQ size (typically {factor:.1f}x, measured on a real BSR run). STAR's two BAM "
                  "files (sorted genome BAM and the transcriptome BAM kept for RSEM) are most of it. Read length, "
                  "depth and multimapping move it within the range, and STAR needs scratch space while sorting, "
                  "so have at least the high end free."),
    }


def report(raw_bytes: int | None = None, n_files: int = 0, trim: bool = True, where: Path | None = None) -> dict:
    """Free space where results go and, if the raw input is known, the RNA-seq estimate and a verdict."""
    where = Path(where) if where else results_root()
    out = {"results_location": _location_label(where), **free_space(where)}
    out["note"] = ("Free space is for the whole filesystem; a group or fileset quota, if any, may be lower. "
                   "Raw FASTQs are not copied: results are written next to your other CodeSpringApp projects.")
    if raw_bytes is None:
        return out
    est = estimate_rnaseq(raw_bytes, trim)
    out["raw_input"] = {"fastq_files": n_files, "size": human(raw_bytes)}
    out["rnaseq_estimate"] = est
    if raw_bytes == 0:
        out["verdict"] = "unknown: no FASTQ files were found to size"
    elif out["free_bytes"] < est["typical_bytes"]:
        out["verdict"] = "not enough space"
    elif out["free_bytes"] < est["high_bytes"]:
        out["verdict"] = "tight: fits the typical estimate but not the high end of the range"
    else:
        out["verdict"] = "enough space"
    return out


def require_room(estimated_bytes: int, where: Path):
    """Refuse to start a run whose typical size doesn't fit in what is free right now."""
    free = free_space(where)["free_bytes"]
    if estimated_bytes and free < estimated_bytes:
        raise HubError(f"Not started: this run needs about {human(estimated_bytes)} but only {human(free)} is free "
                       f"where results go ({_location_label(where)}). Free up space or ask BSR, then plan again.")


def tool_text_rule() -> str:
    return ("Tell the user, in plain words: how much space is free where results go, how big the raw data is, "
            "the range this pipeline is expected to write (low to high, with the typical figure; STAR's BAMs are most "
            "of it), and the verdict. "
            "If it is tight or not enough, say so before anything else and suggest freeing space or asking BSR.")

