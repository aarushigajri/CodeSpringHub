# CodeSpringHub.v1

A governed connection between Claude and Cold Spring Harbor Laboratory lab knowledge and data,
built by the Bioinformatics Shared Resource (BSR). It runs on the cluster as the logged-in user,
with BSR as the first lab. There are two ways in:

- **The portal**, a web page where anyone can edit lab context, add datasets and check what Claude
  sees, with no files or commands involved.
- **The MCP server**, which Claude connects to (Claude Code today, Claude Enterprise in Phase 2).

## Start the portal (the easy way)

**From Claude Desktop:** ask *"Open CodeSpringHub"* (or pick **Open the CodeSpringHub portal** from the
**+** menu). Claude starts your portal on the cluster and gives you a link that works once, for 15
minutes. The Connect page's Claude Desktop setup also forwards your portal and CodeSpringApp ports over
the same SSH login, so the link opens directly, on Mac and Windows. Set up before this existed? Run the
Connect page's step 3 again.

**Without Claude:** from your laptop (on CSHL Wi-Fi or VPN), one command. The **Connect Claude** page shows it with
your username and port already filled in:
```bash
ssh -t -L <port>:localhost:<port> <you>@bamdev1.cshl.edu /grid/bsr/data/data/bsr_readable_data/CodeSpringHub/portal.sh
```
It logs you in, starts your own portal, and prints a private link (`http://localhost:<port>/#token=…`).
Open it in your browser and keep the terminal open while you use the portal.

Already logged in to the cluster? Run `portal.sh` there instead; it also prints the `ssh -N -L …`
command to run on your laptop.

Everyone (BSR and lab members) runs their own copy and gets their own link, kept in
`~/.codespringhub/` in their home folder. The link is personal: anyone holding it can use the portal
as you, so don't share it. Each person has a fixed port (worked out from their account number), so
two people's portals never collide. `portal.sh --new-link` makes the old link stop working. Stop the
portal with Ctrl+C.

In the portal:
- **Lab page:** lab name, projects, and the context page Claude reads first (editor with live preview).
- **Documents:** guidance, protocols, notes and grant aims. "New document" gives you a template.
- **Datasets:** "Add a dataset" lets you click through folders, describe the data, choose how much
  Claude may see, and then shows exactly what Claude gets.
- **BSR workflows:** the shared workflow list (only BSR can edit it).
- **Inbox:** Claude's suggested context edits and requests sent to BSR.
- **Try it as Claude:** ask the Hub what Claude would ask, and see the answer it would get.
- **Activity:** every Claude request and portal edit.

Every save is a local git commit with your name (**History** tab), so nothing is lost.

## Running the three-lab pilot

Researchers use **their own Claude Desktop** (CSHL Enterprise account). Claude Desktop starts the Hub
on the cluster through **their own SSH login**, so it runs as them and sees only what they can already see.
IT doesn't need to be involved.

1. **BSR sets up the lab (once):**
   ```bash
   hub add-lab smith-lab --group "smith lab staff" --name "Smith Lab" --pi jsmith
   hub add-core sequencing --group "seq core staff" --lead jdoe --contact "Jane Doe <jdoe@cshl.edu>"
   hub seed-cores    # starter pages for CSHL cores not on the Hub yet (see docs/DESIGN.md §15)
   hub check
   ```
2. **With the lab, in the portal:** fill in the lab page and register shared **data** and **code**.
   Members can add **personal** items (only they and their PI can see them, not BSR).
3. **Each researcher connects Claude:** portal → **Connect Claude** gives them SSH-key steps (Mac or
   Windows) and a one-command Claude Desktop setup (on Mac it reconnects by itself after the laptop sleeps
   or the network drops). `hub doctor` checks that it works for them.
4. **BSR follows usage** on the Activity page. Connecting means usage is shared with BSR; requests about
   personal items are logged without details.

| | Lab-shared | Personal | Usage logs |
|---|---|---|---|
| Member | read + edit | own only | own |
| PI | read + edit | own + members' | own |
| BSR | read + edit (all labs) | no | everyone's (personal details withheld) |

Lab knowledge stays on `/grid` in each lab's own history and is never pushed to GitHub. Details are in
[docs/DESIGN.md](docs/DESIGN.md) section 12.

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
server/codespringhub/ ← the Python backend
  server.py             MCP server: the tools Claude can call
  hub.py                read operations shared by the MCP server and the portal
  editing.py            portal-only changes: validate, write, git-commit, audit
  web.py                portal web API (/api) + serves the built web app
  identity.py           who is using the portal (private link now, CSHL SSO later)
  permissions.py        who is asking, and what they may see or change
  knowledge.py          reads the knowledge/ folder
  inspect_data.py       summarizes data files without sending raw data
  store.py              search index, audit log, review tickets, suggestions (var/hub.db)
  cli.py                the `hub` admin command
portal/               ← the web app (React + TypeScript; built automatically by portal.sh)
portal.sh             ← start the portal
tests/                ← end-to-end tests (MCP client + portal API)
demo_data/            ← SYNTHETIC demo dataset (scripts/make_demo_data.py)
```

## Editing context: how it works

The easiest way is the portal (above). Underneath, everything is plain text, so you can also
edit the files directly:

1. **Small addition:** open `knowledge/bsr/context.md`, add a line or a section, and save.
2. **New guidance or protocol:** copy `knowledge/bsr/guidance/_TEMPLATE.md` to a new name, such as
   `guidance/scrna-qc.md`, fill it in and save. It gets a document ID like `doc:bsr/shared/guidance/scrna-qc`.
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
are there and which workflow fits ds:bsr/shared/demo-rnaseq?"*

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
| `read_code_file` | Full source of a script/notebook/pipeline in a registered **code** resource |
| `query_table` | One read-only SQL SELECT on a table file, max 50 rows, no other file access |
| `list_workflows` | How BSR runs analyses: CodeSpringApp/CodeSpringLab pipelines first, nf-core as secondary |
| `plan_analysis`, `start_analysis` | Plan a bulk RNA-seq run with BSR's pipeline, show it, start it only after the user confirms |
| `analysis_status`, `analysis_qc`, `list_analyses` | Follow runs you started from the Hub: step and job progress, per-sample QC |
| `continue_analysis`, `cancel_analysis`, `resume_analysis` | Go on after a pause (adding or skipping optional steps), stop a run, or restart one |
| `analysis_report` | The post-run packet: one HTML page with the app's plots, QC, DEG lists and methods; plots shown in the chat, one-time download link |
| `open_results` | One-time link to the run in CodeSpringApp's Results Explorer, plus the SSH tunnel command |
| `open_portal` | Starts your portal if needed and gives a one-time link to it (the portal secret never enters the chat) |
| `request_bsr_review` | Open a ticket for a human BSR expert |

## Safety rules built in

- Only labs whose Unix group you belong to are visible. Other labs look like they don't exist.
- Claude refers to data by Hub ID. Paths are resolved on the server, must stay inside the
  registered folder (no `../` or symlink escapes), and must be readable by you on the cluster.
- Every response is size-capped. Datasets marked `sensitive` give summaries only.
- Every tool call is written to the audit log.
- On disk, each `knowledge/<lab>/` folder can be read and edited only by that lab's group, and `var/` (the audit
  log) only by BSR. `hub check` flags anything looser. Git doesn't keep permissions, so run
  `hub check` after any fresh clone.
- HTTP mode is refused unless you pass `--dev-no-auth`, because there's no login until Phase 2.

## Roadmap

- **Phase 0 (done):** local stdio server, BSR knowledge, file-based registry, tests.
- **Phase 1 (in progress):** web portal for editing context, registering data and testing (done);
  BAM/VCF/h5ad inspectors (next).
- **Phase 2:** HTTPS service with CSHL SSO (OAuth), per-user file access on a shared host, PostgreSQL,
  added as a custom connector in Claude Enterprise.
- **Phase 3:** the three pilot labs, BSR review loop, usage dashboard.
