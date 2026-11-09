"""Look at registered data locally and return small summaries, not the data itself.

Raw files stay on /grid/. What goes back to the model is a capped summary: column
names and a few rows, read-length statistics, the first lines of a text file.
"""

import gzip
import os
from pathlib import Path

import duckdb
from mcp.server.mcpserver.exceptions import ToolError

from . import config

TABLE_EXT = {".csv", ".tsv", ".txt", ".parquet"}
TEXT_EXT = {".md", ".yaml", ".yml", ".json", ".log", ".txt", ".html", ".sh", ".nf", ".config"}
# Source files Claude may read in full from a registered *code* resource.
CODE_EXT = TEXT_EXT | {".py", ".r", ".rmd", ".qmd", ".ipynb", ".pl", ".jl", ".sql", ".smk", ".wdl", ".cwl",
                       ".toml", ".cfg", ".ini", ".c", ".cpp", ".h", ".java", ".js", ".ts", ".bash", ".zsh",
                       ".slurm", ".sbatch", ".awk", ".m", ".rs", ".go", ".csv", ".tsv"}
CODE_NAMES = {"Snakefile", "Makefile", "Dockerfile", "Singularity", "Apptainer", "nextflow.config",
              "README", "LICENSE", "environment.yml", "requirements.txt", "DESCRIPTION", "NAMESPACE"}
SKIP_DIRS = {"__pycache__", "node_modules", ".snakemake", "work", ".nextflow", "renv", ".Rproj.user"}
FASTQ_EXT = {".fastq", ".fq"}


def _ext(path: Path) -> str:
    suffixes = path.suffixes
    if suffixes and suffixes[-1] == ".gz":
        suffixes = suffixes[:-1]
    return suffixes[-1].lower() if suffixes else ""


def _human(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def list_assets(root: Path) -> list[dict]:
    if root.is_file():
        return [{"asset": root.name, "size": _human(root.stat().st_size)}]
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            out.append({"asset": str(p.relative_to(root)), "size": _human(p.stat().st_size)})
            if len(out) >= config.MAX_LISTED_FILES:
                return out
    return out


def _duck_source(path: Path) -> str:
    # Path comes from resolve_asset (already validated); quote it for SQL.
    quoted = str(path).replace("'", "''")
    return f"read_parquet('{quoted}')" if _ext(path) == ".parquet" else f"read_csv_auto('{quoted}')"


def summarize_table(path: Path, preview: bool) -> dict:
    con = duckdb.connect()
    src = _duck_source(path)
    cols = con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()
    n_rows = con.execute(f"SELECT count(*) FROM {src}").fetchone()[0]
    out = {
        "kind": "table",
        "rows": n_rows,
        "columns": [{"name": c[0], "type": c[1]} for c in cols[:100]],
        "n_columns": len(cols),
    }
    if preview:
        rows = con.execute(f"SELECT * FROM {src} LIMIT 5").fetchall()
        out["preview_first_5_rows"] = [[str(v) for v in r[:20]] for r in rows]
    return out


def summarize_fastq(path: Path, max_reads: int = 2000) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    lengths, gc, n = [], 0, 0
    with opener(path, "rt") as fh:
        while n < max_reads:
            header = fh.readline()
            if not header:
                break
            seq = fh.readline().strip()
            fh.readline()
            fh.readline()
            lengths.append(len(seq))
            gc += seq.count("G") + seq.count("C")
            n += 1
    total = sum(lengths)
    return {
        "kind": "fastq",
        "reads_sampled": n,
        "note": f"statistics from the first {n} reads only" if n == max_reads else "all reads",
        "read_length_min": min(lengths) if lengths else 0,
        "read_length_max": max(lengths) if lengths else 0,
        "read_length_mean": round(total / n, 1) if n else 0,
        "gc_percent": round(100 * gc / total, 1) if total else 0,
    }


def summarize_text(path: Path, max_lines: int = 80) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    lines = []
    with opener(path, "rt", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= max_lines:
                break
            lines.append(line.rstrip("\n"))
    text = "\n".join(lines)[: config.MAX_OUTPUT_CHARS]
    return {"kind": "text", "first_lines": text, "truncated": len(lines) == max_lines}


def read_source(path: Path) -> str:
    """Full text of a source file from a code resource, capped like every other reply."""
    if _ext(path) not in CODE_EXT and path.name not in CODE_NAMES:
        raise ToolError(f"'{path.name}' doesn't look like a source file; use inspect_asset for a summary.")
    if path.stat().st_size > 2 * config.MAX_OUTPUT_CHARS * 4:
        raise ToolError(f"'{path.name}' is very large ({_human(path.stat().st_size)}); ask for a specific part, or BSR.")
    text = path.read_text(errors="replace")
    if len(text) > config.MAX_OUTPUT_CHARS:
        text = text[: config.MAX_OUTPUT_CHARS] + f"\n… [file continues: {len(text):,} characters in total]"
    return text


def summarize(path: Path, sensitive: bool) -> dict:
    base = {"asset": path.name, "size": _human(path.stat().st_size)}
    ext = _ext(path)
    if ext in FASTQ_EXT:
        return base | summarize_fastq(path)
    if ext in TABLE_EXT:
        try:
            return base | summarize_table(path, preview=not sensitive)
        except duckdb.Error:
            if ext != ".txt":
                raise
    if sensitive:
        return base | {"kind": "restricted", "note": "Dataset is marked sensitive: no content previews."}
    if ext in TEXT_EXT:
        return base | summarize_text(path)
    return base | {
        "kind": "unsupported",
        "note": f"No Phase 0 inspector for '{ext or 'this file type'}' yet (BAM/VCF/h5ad planned).",
    }


def query_table(path: Path, sql: str) -> dict:
    """Run one read-only SQL query against a single table file, exposed as `data`.

    The file is loaded into memory first, then DuckDB's file access is switched off
    and locked, so the query cannot read any other file on the system.
    """
    if _ext(path) not in TABLE_EXT:
        raise ToolError("query_table works on .csv, .tsv, .txt or .parquet files.")
    if path.stat().st_size > config.MAX_QUERY_FILE_BYTES:
        raise ToolError(f"File is larger than {_human(config.MAX_QUERY_FILE_BYTES)}; ask BSR for help.")
    stripped = sql.strip().rstrip(";")
    if not stripped.lower().startswith(("select", "with")) or ";" in stripped:
        raise ToolError("Only a single SELECT (or WITH … SELECT) statement is allowed.")

    con = duckdb.connect()
    con.execute(f"CREATE TABLE data AS SELECT * FROM {_duck_source(path)}")
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    try:
        cur = con.execute(stripped)
    except duckdb.Error as e:
        raise ToolError(f"SQL error: {e}") from None
    cols = [d[0] for d in cur.description]
    rows = cur.fetchmany(config.MAX_TABLE_ROWS + 1)
    return {
        "columns": cols,
        "rows": [[str(v) for v in r] for r in rows[: config.MAX_TABLE_ROWS]],
        "truncated": len(rows) > config.MAX_TABLE_ROWS,
    }
