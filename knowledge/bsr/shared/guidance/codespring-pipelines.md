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

Optional, after the gene counts: **RSEM** (gene and transcript estimates from STAR's transcriptome
BAM, by gene_id) and **Kallisto** (transcript estimates from the reads). Both build TPM matrices in
`counts/`. Pathway analysis can use up to 6 gene-set databases in one run (one GSEApy job each).
A run can go **all at once** or **one step at a time** (it pauses after each step until the person
says to continue).

**References**: human GRCh38 / GENCODE v50 (default) or v42 (legacy); mouse GRCm39 / GENCODE M39
(default) or M29 (legacy). Maize references exist for RNA-seq in the app.

## Storage: say it at the start of every project

Before a project starts, tell the researcher how much space is free where results go
(`~/csl_results/`) and how much the pipeline is expected to write, as a range. From Claude,
`check_storage` gives both for a registered dataset, and `plan_analysis` repeats them in the plan.
Raw FASTQs are read in place, never copied.

**Bulk RNA-seq (BSR standard pipeline)**: about **5.6x-9.1x the gzipped FASTQ size, typically 7x**,
measured on a real BSR run (12 paired-end mouse FASTQs, 620 MB -> 4.4 GB). Per GB of gzipped FASTQ:

| Step | Kept on disk | Typical per GB of raw |
|---|---|---|
| Cutadapt | trimmed FASTQs (gzipped) | ~1.05 GB |
| STAR | sorted genome BAM + index (~1.0 GB) and transcriptome BAM for RSEM (~3.6 GB) | ~4.9 GB |
| featureCounts | per-sample count tables | ~1.0 GB |
| FastQC | reports | ~0.1 GB |
| Counts, DESeq2, GSEApy, logs | small tables and plots | ~50 MB in total |

The range (0.8x-1.3x the typical figure) covers read length, depth, multimapping and how well reads
compress. STAR also needs scratch space while sorting, so have **at least the high end free**. With
trimming off, take away the Cutadapt share. Example: 50 GB of FASTQs -> ~282-458 GB, typically
~352 GB.

**Other pipelines (run in CodeSpringApp)**: rough guidance only, not yet measured on full-size data.

- ATAC-seq and ChIP-seq: BAMs (Bowtie2) are roughly 1-2x the raw FASTQ size, plus trimmed FASTQs
  (~1x); plan for about 3-4x the raw data. TODO: measure on a full-size run.
- Single-cell RNA-seq (Cell Ranger): TODO: measure (the BAM alone is often 1-2x the FASTQs).
- Whole-genome variants (sarek): TODO: measure (CRAMs and the nf-core work folder are large).

When in doubt, or for very large projects, ask BSR (request_bsr_review).

## Starting a run from Claude (BSR's Agent)

Every run started from Claude follows the same steps, so researchers see the same thing each time:
a storage check, a greeting from BSR's Agent, four questions (pipeline, pathway gene sets, extra
quantification, run mode), a one-table plan preview with the full output folder and BSR support
contacts, then **Start pipeline** or **Ask for clarification**. Support contacts live in
`knowledge/support_contacts.yaml`.

While it runs:

- **QC after each step**: Cutadapt reads before/after, FastQC's own PASS/WARN/FAIL per module, STAR
  input reads and uniquely mapped / multi-mapped / unmapped %, featureCounts assigned %, Kallisto
  pseudoaligned %. Report the numbers; BSR hasn't set pass/fail cut-offs, so don't invent any.
  FastQC's FAIL flags are FastQC's judgement, and some (e.g. per-base sequence content, duplication)
  are normal for RNA-seq: say so, and suggest BSR if the researcher is unsure.
- **At a pause** (one step at a time) the researcher can continue, add RSEM or Kallisto, skip RSEM,
  Kallisto or GSEA, switch to all at once, or cancel. RSEM/Kallisto can also be added after the run.
- **Updates**: Claude can't message anyone between turns. The researcher can ask any time, or choose
  **email updates** when planning: to their own CSHL address only, when a step finishes, fails or
  pauses, plus an optional summary every 1-24 hours.
- **Cancel** stops the run's controller and every queued or running Slurm job; finished steps are
  kept and the run can be resumed.
- **Larger studies**: per-sample jobs are submitted in waves that stay under the person's Slurm job
  limit (16 at once on the default QOS), so a 40-sample run doesn't fail at submission.
- **When it finishes** they get a report packet: one HTML file with the app's plots (PCA, volcano,
  MA, GSEA dot plots), the QC table, DEG counts and lists, and methods text, all downloadable from
  the page. Big files (count matrices, BAMs) stay on the cluster; the report lists where.

## What to tell researchers

- Some pipelines are run by BSR for labs rather than started from a chat (single-cell and variant
  calling by default; BSR's list is `knowledge/bsr/shared/pipelines.yaml`). For those, offer a BSR
  review request instead of walking them through it.

- Each step uses the CPU, memory and time limits written into CodeSpringLab's Slurm scripts;
  nobody tunes them per run. If a step runs out of memory or time, that's a question for BSR.
- Starting from Claude, the run is identical to clicking through CodeSpringApp, and the project
  appears in their CodeSpringApp project list.
- The small example dataset (`ds:bsr/shared/codespringlab-rnaseq-example`: 6 mouse samples, control vs
  treated) finishes in roughly 10-20 minutes and is the best first try.

## Known limitations (November 2026)

- From Claude, only bulk RNA-seq can be started so far; other analyses run in CodeSpringApp.
- Several gene sets, RSEM/Kallisto and one-step-at-a-time runs from Claude need CodeSpringApp's
  hub bridge spec version 2 in the shared install; until then the Hub says so and plans without them.
- RSEM and Kallisto output size isn't measured yet (small next to STAR's BAMs). TODO: measure.
- In CodeSpringLab's notebooks, ATAC single-end peak calling and ChIP HOMER annotation are broken;
  CodeSpringApp's versions are the ones to use.
- Results from GSEApy for mouse depend on ortholog mapping; mention this when interpreting.
