# How BSR runs analyses: CodeSpringApp and CodeSpringLab

BSR's standard pipelines are **CodeSpringLab** (James Rouse's analysis scripts: one Slurm wrapper
per tool, fixed modules, references and resource requests) driven by **CodeSpringApp** (the Shiny
control centre that creates projects, submits each step, tracks jobs and shows Results Explorers).
Recommend these first, and follow them as they are. nf-core pipelines are a secondary option, for
when no BSR pipeline fits; say so when you suggest one.

Everyone runs one BSR-managed copy of both (`CodeSpringSuite` on /grid), under their own account.
Results go to the person's own CodeSpringApp results folder (`~/csl_results/<project>/`).

## Which pipeline for which data

| Data | BSR pipeline | Start it from |
|---|---|---|
| Bulk RNA-seq (FASTQ) | CodeSpringLab RNA-seq | **Claude** (plan_analysis) or CodeSpringApp |
| Bulk ATAC-seq | CodeSpringLab ATAC (Bowtie2, MACS2, DiffBind) | CodeSpringApp |
| Bulk ChIP-seq | CodeSpringLab ChIP (Bowtie2, MACS2 with matched input, HOMER) | CodeSpringApp |
| CUT&RUN | nf-core/cutandrun 3.2.2 run by CodeSpringApp (paired-end), or native SEACR/MACS2 | CodeSpringApp |
| Single-cell RNA-seq | Seurat (default) or Scanpy, Cell Ranger 9 for FASTQs | CodeSpringApp |
| Whole-genome variants (human) | nf-core/sarek 3.9.0 run by CodeSpringApp (germline, tumour-only, tumour/normal) | CodeSpringApp |
| Public data (SRA/ENA/GEO) | nf-core/fetchngs 1.12.0 run by CodeSpringApp | CodeSpringApp |

## Bulk RNA-seq, as BSR runs it

1. **Design matrix** (tab-separated): `sample`, grouping columns (e.g. `treatment`), and `filename`
   last. Paired-end files are written `R1,R2`; several lanes are separated by `;`.
2. **Cutadapt**: adapter preset (Illumina Universal TruSeq RNA by default; Nextera, small-RNA 3'/5'),
   minimum length 20.
3. **FastQC** on the trimmed reads (with FastQ Screen).
4. **STAR 2.7.10a** alignment; per job 4 CPUs, 200 GB memory.
5. **featureCounts** (strandedness detected automatically with RSeQC), counting by `gene_name`
   (or `gene_id`), then one count matrix.
6. **DESeq2** for one comparison: a grouping column, a reference level and a comparison level,
   at least 2 replicates per group. Optional extra covariate.
7. **GSEApy** pathway enrichment (MSigDB Hallmark 2020 by default; KEGG, GO BP 2025, Reactome 2024
   and others). Mouse genes are mapped to human orthologs.
8. **Results Explorer** (CodeSpringApp): QC, counts, PCA, volcano, heatmaps, GSEA, files.

Optional (the app's own, run exactly as the app runs them): **Kallisto** transcript abundance (TPM)
from the same reads, and **RSEM** gene/transcript quantification from STAR's transcriptome
alignments. From Claude they can be chosen when planning, added at any pause, or added after the
run finishes.

## Running from Claude, step by step

- **Two ways to run**: straight through, or **step by step** (pauses after each step so the
  researcher can look at that step's QC and decide: continue, add or skip an optional step, switch
  to straight through, or cancel). Ask which they want when planning.
- **QC while it runs**: Cutadapt reads before/after, FastQC's own PASS/WARN/FAIL per module, STAR
  input reads and uniquely mapped / multi-mapped / unmapped %, featureCounts assigned %, Kallisto
  pseudoaligned %. Report the numbers; BSR hasn't set pass/fail cut-offs, so don't invent any.
  FastQC's FAIL flags are FastQC's judgement, and some (e.g. per-base sequence content, duplication)
  are normal for RNA-seq: say so, and suggest BSR if the researcher is unsure.
- **Updates**: Claude can't message anyone between turns. The researcher can ask for an update
  any time, or turn on **email updates** when planning: to their own CSHL address only, when a step
  finishes, fails or pauses, plus an optional summary every 1-24 hours.
- **Cancel** stops the run's controller and every queued or running Slurm job; finished steps are
  kept and the run can be resumed.
- **Larger studies**: per-sample jobs are submitted in waves that stay under the person's Slurm job
  limit (16 at once on the default QOS), so a 40-sample run doesn't fail at submission.

**References**: human GRCh38 / GENCODE v50 (default) or v42 (legacy); mouse GRCm39 / GENCODE M39
(default) or M29 (legacy). Maize references exist for RNA-seq in the app.

## What to tell researchers

- Each step uses the CPU, memory and time limits written into CodeSpringLab's Slurm scripts;
  nobody tunes them per run. If a step runs out of memory or time, that's a question for BSR.
- Starting from Claude, the run is identical to clicking through CodeSpringApp, and the project
  appears in their CodeSpringApp project list.
- When a run finishes they get a report packet: one HTML file with the app's plots (PCA, volcano,
  MA, GSEA dot plots), the QC table, DEG counts and lists, and methods text, all downloadable from
  the page. Big files (count matrices, BAMs) stay on the cluster; the report lists where.
- The small example dataset (`ds:bsr/codespringlab-rnaseq-example`: 6 mouse samples, control vs
  treated) finishes in roughly 10-20 minutes and is the best first try.

## Known limitations (November 2026)

- From Claude, only bulk RNA-seq can be started so far; other analyses run in CodeSpringApp.
- The post-run report packet (HTML with plots and small data files) is still being built.
- In CodeSpringLab's notebooks, ATAC single-end peak calling and ChIP HOMER annotation are broken;
  CodeSpringApp's versions are the ones to use.
- Results from GSEApy for mouse depend on ortholog mapping; mention this when interpreting.
