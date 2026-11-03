# Bioinformatics Shared Resource (BSR): lab context

> Claude reads this page first whenever it works with BSR. Edit it freely and save;
> the change is live on Claude's next question. Lines marked TODO are ignored as guidance.

## Who we are
The Bioinformatics Shared Resource (BSR) is a core facility at Cold Spring Harbor Laboratory.
BSR supports CSHL laboratories with bioinformatics analysis, reusable workflows, training,
and guidance on the responsible use of AI in bioinformatics.

## How BSR wants AI-assisted analysis to work
- Prefer BSR-registered workflows over newly generated pipelines (see list_workflows).
- Generated code and analytical recommendations must be reviewed by a person before results
  are relied on, especially for sensitive or high-impact analyses.
- Novel, uncertain or scientifically complex requests should be routed to BSR for review.
- Do not ask users to paste confidential lab, project or genomic data into chat; use
  registered datasets through the Hub instead.

## Computing environment
- Data lives on the CSHL `/grid/` storage; jobs run through the Slurm scheduler.
- TODO: preferred partitions, module/conda conventions, where shared references live.

## Conventions
- TODO: reference genomes and annotation versions BSR uses by default.
- TODO: naming conventions for samples, projects and output folders.

## Contacts
- TODO: how users reach BSR (email, ticket system, office hours).
