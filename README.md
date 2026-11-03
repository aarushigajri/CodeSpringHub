# CodeSpringHub.v1

A governed connection between Claude and Cold Spring Harbor Laboratory lab knowledge and data,
built by the Bioinformatics Shared Resource (BSR). This is **Phase 0**: it runs locally on the
cluster inside Claude Code, as the logged-in user, with BSR as the first lab.

## What's here

```
knowledge/            ← the human-edited content (you edit this)
  workflows.yaml        BSR workflow registry, visible to every Hub user
  bsr/
    lab.yaml            lab name, Unix group, projects
    context.md          the page Claude reads first about this lab
    datasets.yaml       registered pointers to data on /grid/ (never copied)
    guidance/*.md       BSR guidance documents
    protocols/*.md      protocols / SOPs
server/codespringhub/ ← the MCP server
  server.py             the tools Claude can call (start reading here)
  permissions.py        who is asking, and what they may see
  knowledge.py          reads the knowledge/ folder
  inspect_data.py       summarizes data files without sending raw data
  store.py              search index, audit log, review tickets, suggestions (var/hub.db)
  cli.py                the `hub` admin command
tests/                ← end-to-end tests using a real MCP client
demo_data/            ← SYNTHETIC demo dataset (scripts/make_demo_data.py)
```

## Editing context: how it works

All context is plain text. To add something BSR-related:

1. **Small addition:** open `knowledge/bsr/context.md`, add a line or a section, and save.
2. **New guidance or protocol:** copy `knowledge/bsr/guidance/_TEMPLATE.md` to a new name, such as
   `guidance/scrna-qc.md`, fill it in and save. It gets a document ID like `doc:bsr/guidance/scrna-qc`.
3. **New dataset:** add an entry to `knowledge/bsr/datasets.yaml`, then run `hub check`.
4. **New workflow:** add an entry to `knowledge/workflows.yaml`.

**The change is live on Claude's next question.** Nothing is cached: each tool call re-reads the
files, and the search index rebuilds itself automatically whenever any file changes.

- Files starting with `_` are ignored, so use that prefix for drafts and templates.
- Write `TODO` for parts that aren't filled in yet. Claude is told not to treat them as guidance.
- If someone tells Claude something worth remembering, Claude can call `suggest_context_edit`.
  That only **queues** a suggestion, which you see with `hub suggestions`. A person decides whether
  to apply it by editing the file. Claude can never change the context itself.
- Use git to see who changed what: `git log -p knowledge/`.

## Using it

```bash
cd /grid/bsr/data/data/bsr_readable_data/CodeSpringHub
conda activate ./env

hub check            # validate knowledge/ (groups exist, dataset paths exist, …)
hub status           # what the Hub currently exposes
pytest -q            # run the tests
```

**In Claude Code:** start `claude` in this folder. `.mcp.json` registers the server and Claude Code
asks once whether to trust it. Then try asking: *"Using CodeSpringHub, what BSR datasets
are there and which workflow fits ds:bsr/demo-rnaseq?"*

**Click through the tools by hand** with the MCP Inspector:
```bash
npx @modelcontextprotocol/inspector ./env/bin/codespringhub
```

**Admin views:** `hub suggestions`, `hub reviews`, `hub audit -n 50`.

## Tools Claude sees

| Tool | What it does |
|---|---|
| `whoami`, `list_labs` | Who you are and which labs you can use (Unix group membership) |
| `get_lab_context`, `search_knowledge`, `read_document` | Read the curated context and documents |
| `suggest_context_edit` | Queue a proposed context change for human review |
| `list_datasets`, `describe_dataset` | Registered data, by Hub ID only (no paths) |
| `inspect_asset` | Summary of one file: table shape and preview, FASTQ stats, first lines of text |
| `query_table` | One read-only SQL SELECT on a table file, max 50 rows, no other file access |
| `list_workflows` | BSR workflow registry (`validated` / `candidate`) |
| `request_bsr_review` | Open a ticket for a human BSR expert |

## Safety rules built in

- Only labs whose Unix group you belong to are visible. Other labs look like they don't exist.
- Claude refers to data by Hub ID. Paths are resolved on the server, must stay inside the
  registered folder (no `../` or symlink escapes), and must be readable by you on the cluster.
- Every response is size-capped. Datasets marked `sensitive` give summaries only.
- Every tool call is written to the audit log.
- On disk, each `knowledge/<lab>/` folder is readable only by that lab's group, and `var/` (the audit
  log) only by BSR. `hub check` flags anything looser. Git doesn't keep permissions, so run
  `hub check` after any fresh clone.
- HTTP mode is refused unless you pass `--dev-no-auth`, because there's no login until Phase 2.

## Roadmap

- **Phase 0 (this):** local stdio server, BSR knowledge, file-based registry, tests.
- **Phase 1:** web portal for registering data and editing context; BAM/VCF/h5ad inspectors.
- **Phase 2:** HTTPS service with CSHL SSO (OAuth), per-user file access on a shared host, PostgreSQL,
  added as a custom connector in Claude Enterprise.
- **Phase 3:** the three pilot labs, BSR review loop, usage dashboard.
