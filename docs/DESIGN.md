# CodeSpringHub.v1: Design

*Bioinformatics Shared Resource (BSR), Cold Spring Harbor Laboratory. Status: Phase 0 built; Phase 1 portal built; multi-lab pilot support built (November 2026).
This document describes what is built today and how the later phases are meant to extend it.*

---

## 1. Purpose and scope

CodeSpringHub is a controlled layer between Claude and CSHL's laboratory knowledge and data.
Claude can use only what a person has deliberately put into the Hub. It sees that material through
a small set of tools, and every tool checks who is asking.

**In scope for v1**
- An MCP server that Claude (Claude Code now, Claude Enterprise later) connects to.
- Lab knowledge: lab context, BSR guidance, protocols and notes as plain, editable text files.
- Registered data: pointers to existing folders on `/grid/`, which Claude can summarize but not copy.
- A BSR workflow registry, so Claude recommends validated workflows before writing new pipelines.
- Escalation: Claude can open a review ticket for a human BSR expert.
- An audit log of every tool call, which supplies the pilot's evaluation data.
- *(Phase 1)* A web portal for registering data and editing context.

**Out of scope for v1**
- Submitting or running jobs on Slurm.
- The CodeSpringAgent chat application, or calling the Claude API directly.
- Automatically crawling or indexing lab storage.
- Controlled-access human genomic data.

---

## 2. Vocabulary

| Term | Meaning | Example |
|---|---|---|
| **Lab** | A group whose members share access. Membership = a Unix group. | `bsr` → group `bioinformatics` |
| **Project** | A named piece of work inside a lab. | `codespringhub-pilot` |
| **Dataset** | A registered pointer to a file or folder on `/grid/`. The data is never copied. | `ds:bsr/shared/demo-rnaseq` |
| **Asset** | One file inside a dataset, named relative to the dataset folder. | `reads/S1_R1.fastq.gz` |
| **Document** | A Markdown knowledge file (context, guidance, protocol, note). | `doc:bsr/shared/guidance/ai-assisted-analysis` |
| **Workflow** | A BSR-registered analysis pipeline with a status. | `nf-core-rnaseq` (`candidate`) |
| **Hub ID** | The only handle Claude ever sees for a dataset or document. | `ds:…`, `doc:…` |

The hierarchy follows the CodeSpringSuite deck: **Lab → Project → Resource (dataset or document) → Asset**.

---

## 3. Architecture

### 3.1 Phase 0 (built)

```
 Researcher ── Claude Code (on the cluster) ──stdio──► codespringhub process
                                                     (runs as the researcher)
                                                             │
           ┌─────────────────────────────────────────────────┼──────────────────────────┐
           │                                                 │                          │
   knowledge/ (text files)                           var/hub.db (SQLite)        /grid/ storage
   context, guidance, datasets.yaml,                 search index, audit,       registered datasets
   workflows.yaml (edited by people)                 reviews, suggestions       (read only)
```

- **One process per user session.** Claude Code starts the server as a child process and talks to
  it over stdin/stdout (the stdio transport). The process runs as the logged-in user, so cluster
  file permissions apply automatically.
- **The files are the source of truth.** Knowledge is never stored only in the database. The
  database holds machine state that can always be rebuilt or is append-only.

### 3.2 Phase 1: the portal (built)

```
 Lab member / BSR ── browser on laptop ──ssh tunnel──► codespringhub-portal (on the cluster, as that user)
                     private link (#token=…)            │  /api  → hub.py (read) + editing.py (write)
                                                        │  /     → built React app
                                                        └─ /api/tools/* → the same MCP server, in-process
```

- **One codebase, two front doors.** The MCP server (`server.py`) and the portal API (`web.py`)
  both call the same service layer: `hub.py` for reads and `editing.py` for writes. Every service
  function takes the acting `user` explicitly, so permissions are identical however a request arrives.
- **Built for HTTPS.** The only part tied to "local" is `identity.py`, which turns a request's bearer
  token into a username. Today that token is a private link created at start-up and kept in the user's own home folder
  (`~/.codespringhub/portal-token`, 700/600), so each BSR member running the portal has their own link.
  In Phase 2 it becomes a CSHL SSO token, and nothing downstream changes. The acting user reaches the
  MCP tools through a context variable (`permissions.acting_as`), which a test verifies. The same
  mechanism will carry the SSO user into the HTTPS MCP endpoint.
- **"Try it as Claude"** calls the real MCP tools through an in-process MCP client as the portal user,
  so what the portal shows is exactly what Claude would receive.

### 3.3 Target (Phase 2+)

```
 Researcher ─► Claude Enterprise ─HTTPS + OAuth─► CSHL reverse proxy ─► CodeSpringHub service
                (Anthropic cloud)                 (Anthropic IPs only)     │  token → CSHL user
 Lab / BSR ──► Hub Portal (SSO) ──────────────────────────────────────────►│
                                                                           ├─ PostgreSQL (registry, audit)
                                                                           ├─ knowledge/ (git)
                                                                           └─ per-user file helper ─► /grid/
```

Section 10 covers what changes between the two.

### 3.4 Code layout

| File | Responsibility |
|---|---|
| `server/codespringhub/server.py` | MCP server, Claude instructions, the 12 tools (thin wrappers over `hub.py`), 1 prompt, the audit/limit wrapper |
| `server/codespringhub/hub.py` | Read operations shared by MCP and portal; each takes `user` and checks permissions |
| `server/codespringhub/editing.py` | Portal-only writes: validation, atomic file writes, per-save git commit, audit; folder browser; inbox |
| `server/codespringhub/web.py` | Portal API under `/api`, security headers, serves `portal/dist` |
| `server/codespringhub/identity.py` | Bearer token → username. `local` (private link) now, `oidc` (CSHL SSO) placeholder |
| `server/codespringhub/errors.py` | `HubError` / `NotFound` / `Forbidden`: plain-language messages for both Claude and the portal |
| `server/codespringhub/permissions.py` | Acting user, Unix-group lookup, view/edit/BSR checks, safe path resolution |
| `server/codespringhub/knowledge.py` | Reads `knowledge/` into Lab / Project / Dataset / Document objects |
| `server/codespringhub/inspect_data.py` | File summaries (table, FASTQ, text) and sandboxed SQL |
| `server/codespringhub/store.py` | SQLite: full-text index, audit, reviews, suggestions |
| `server/codespringhub/config.py` | Paths and output limits |
| `server/codespringhub/cli.py` | `hub` admin command |
| `portal/src/` | React + TypeScript web app (pages: Home, Lab page, Documents, Datasets, Workflows, Inbox, Try it, Activity) |
| `portal.sh` | Builds the web app if needed and starts the portal |
| `tests/test_server.py`, `tests/test_portal.py` | End-to-end tests through a real MCP client and the portal API (33 tests) |

---

## 4. Life of a request

Example: a researcher asks *"Which samples in the demo RNA-seq data are treated?"*

1. **Claude chooses a tool.** At connection time Claude received each tool's name, description and
   input schema, plus the server instructions. It picks `query_table`.
2. **Claude sends the call:** `query_table(dataset_id="ds:bsr/shared/demo-rnaseq", asset="samplesheet.csv",
   sql="SELECT sample FROM data WHERE condition='treated'")`.
3. **The `@hub_tool` wrapper** records the current user and opens the store.
4. **Identity and lab check:** the lab ID `bsr` is parsed from the dataset ID. The user's Unix groups
   must include the lab's `unix_group`. If not, the reply is "No accessible lab", the same message
   as for a lab that doesn't exist.
5. **Registration check:** the dataset must be listed in `knowledge/bsr/datasets.yaml`.
6. **Path check:** `samplesheet.csv` is resolved, symlinks included, and must stay inside the dataset
   folder and be readable by the user.
7. **Policy checks:** `sensitive` datasets refuse row-level queries. The SQL must be a single
   SELECT or WITH statement, and the file must be under 500 MB.
8. **Work:** the file is loaded into an in-memory DuckDB table called `data`. External file access is
   then switched off and locked before the user's SQL runs. At most 50 rows come back.
9. **Output cap:** any reply over 20,000 characters is truncated with a visible note.
10. **Audit:** a row records time, user, tool, arguments, success or failure, and characters returned.
11. **Reply:** JSON goes back to Claude, which writes the answer for the researcher.

Any failure in steps 4–8 comes back to Claude as a tool error with a plain-language message. The
failure is still audited.

---

## 5. Data model

### 5.1 Human-edited files (`knowledge/`)

```
knowledge/
  workflows.yaml               global BSR workflow registry
  <lab_id>/                    folder name = lab ID
    lab.yaml
    context.md
    datasets.yaml
    <category>/<name>.md       category = folder name (guidance, protocols, grants, notes…)
    _anything                  ignored (templates, drafts)
```

**`lab.yaml`**
```yaml
name: Bioinformatics Shared Resource (BSR)
unix_group: bioinformatics          # required: lab membership
description: …
projects:
  - {id: codespringhub-pilot, name: CodeSpringHub pilot, description: …}
```

**`datasets.yaml`**
```yaml
datasets:
  - id: demo-rnaseq                 # becomes ds:bsr/shared/demo-rnaseq
    title: Demo bulk RNA-seq (synthetic)
    project: codespringhub-pilot    # must match a project in lab.yaml (hub check)
    path: /grid/…/demo-rnaseq       # file or folder; never shown to Claude
    assay: rna-seq
    description: …
    sensitivity: standard           # standard | sensitive
    registered_by: gajri
    registered_on: "2026-11-03"
```

**`workflows.yaml`**
```yaml
workflows:
  - id: nf-core-rnaseq
    name: nf-core/rnaseq
    assays: [rna-seq]
    status: candidate               # validated | candidate
    version: TODO
    url: https://nf-co.re/rnaseq
    summary: …
```

**Documents** are Markdown. The first `# Heading` is the title. The ID is
`doc:<lab>/<path without .md>`, and `context.md` is category `context`.

### 5.2 Machine state

Since the multi-lab pilot (section 12), machine state is split so that no file holds more than its readers may see:

| Where | Contents | Who can read it |
|---|---|---|
| `var/events/<user>.db` | that person's usage log (`audit`: ts, user, lab_id, tool, args, ok, chars_out, error), their BSR review requests (`reviews`), notices shown (`notices`) | the person and BSR |
| `~/.codespringhub/index.db` | that person's full-text index (FTS5), built only from documents they may see | the person |
| `~/.codespringhub/portal-token` | the person's private portal link | the person |
| `var/hub.db` | `review_status`: BSR's progress on each review request | BSR |
| `knowledge/<lab>/_suggestions/*.yaml`, `members/<user>/_suggestions/` | Claude's suggested edits, as files next to what they're about | whoever can see the document |

The index rebuilds itself. Deleting `var/` loses usage history and review status.

---

## 6. Context editing

This is a core requirement: lab members and BSR must be able to add and correct context easily.

**Model:** context is plain files, and the server has no cache.
- Every tool call re-reads the YAML and Markdown it needs, so an edit is visible on the next call.
- Search uses an index, so it needs a freshness check. On every search the server computes a
  **fingerprint** of the knowledge folder: the path, size and modification time of every file.
  If the fingerprint differs from the stored one, the index is rebuilt before searching. With
  dozens to hundreds of documents this takes milliseconds.

**Who can change context, and how**

| Actor | Can do | Mechanism |
|---|---|---|
| Lab member | Edit their lab's details, context, documents and datasets | Portal (or the files directly) |
| BSR staff | All of the above for BSR, plus the shared workflow registry and review tickets | Portal |
| Claude | **Suggest** a change only | `suggest_context_edit` → `suggestions` table |
| Reviewer | Apply or dismiss suggestions | Portal Inbox: "Open document to apply" shows the suggestion beside the editor |

Claude can never write to `knowledge/`. That keeps a person responsible for everything the AI is told
is "lab knowledge".

**Conventions**
- `_`-prefixed files are drafts or templates and are never shown to Claude.
- `TODO` marks unfinished text. The server instructions tell Claude not to treat it as guidance.
  *This relies on the model following instructions; it is not enforced in code.*
- Git provides history and attribution (`git log -p knowledge/`).

**The portal (Phase 1, built)** edits the same files. Each save:
1. checks the user can edit that lab (member of its group, and the lab folder is writable);
2. validates input with plain-language errors (document names, dataset paths under `/grid/` and
   readable by the user, project IDs, workflow statuses);
3. writes the file atomically, keeping its permissions;
4. makes a **local git commit for just that file** with the user as author (never pushed), which
   feeds the History tab and makes any change reversible;
5. writes a `portal:*` row to the audit log.

YAML files written by the portal lose hand-written comments; a header line marks them as managed.
*Not yet built:* a BSR approval step for changes to BSR guidance (today any BSR member can edit it).

---

## 7. Tool catalogue

All tools run through `@hub_tool`, which handles identity, the 20,000-character cap and auditing.

| Tool | Inputs | Returns | Access check |
|---|---|---|---|
| `whoami` | – | user, accessible lab IDs | – |
| `list_labs` | – | accessible labs with projects and counts | group membership filter |
| `get_lab_context` | lab_id | context.md text + document index | lab |
| `search_knowledge` | query, lab_id? | up to 8 hits: doc_id, title, category, snippet | only accessible labs searched |
| `read_document` | doc_id | full Markdown | lab of the document |
| `suggest_context_edit` | lab_id, suggestion, doc_id? | ticket number | lab |
| `list_datasets` | lab_id, project? | dataset metadata, **no paths** | lab |
| `describe_dataset` | dataset_id | metadata + up to 200 assets with sizes | lab + path |
| `inspect_asset` | dataset_id, asset | type-specific summary (below) | lab + path |
| `query_table` | dataset_id, asset, sql | ≤ 50 rows | lab + path + not sensitive |
| `list_workflows` | assay? | workflow registry entries | – (global) |
| `request_bsr_review` | lab_id, summary, dataset_ids? | ticket number | lab + each dataset |

Plus one **prompt**, `plan_analysis(dataset_id, question)`: a template that walks Claude through
context → dataset → workflow → QC → escalation.

**`inspect_asset` summaries**

| File type | Summary | Sensitive datasets |
|---|---|---|
| `.csv .tsv .txt .parquet` | row count, up to 100 column names/types, 5-row preview | no preview |
| `.fastq(.gz) .fq(.gz)` | first 2,000 reads: length min/mean/max, GC % | same (statistics only) |
| `.md .yaml .json .log .sh .nf .config …` | first 80 lines | refused |
| anything else (BAM, VCF, h5ad…) | name and size, "no inspector yet" | same |

**Claude's instructions**, sent once per connection, tell it to:
1. Read lab context before lab-specific advice.
2. Cite document IDs.
3. Use Hub IDs only.
4. Prefer `validated` workflows and flag `candidate` ones as not yet signed off.
5. Offer `request_bsr_review` for novel, uncertain or high-impact work.
6. Ignore TODO text.
7. Offer `suggest_context_edit` when the user shares something worth keeping.

---

## 8. Security model

**Principle:** nobody gets anything through the Hub that they could not already read on the
cluster, and Claude gets only what was deliberately registered.

| Threat | Mitigation in Phase 0 | Tested |
|---|---|---|
| User sees another lab's knowledge | Lab membership = Unix group; other labs are filtered out of every listing and search; same error for "forbidden" and "doesn't exist" | ✓ |
| Claude reads an unregistered file | Data is addressed only by Hub ID, and paths come from `datasets.yaml`, not from Claude | ✓ |
| Path traversal (`../`, absolute paths, symlinks) | `realpath` of the target must be inside the realpath of the dataset folder | ✓ |
| User lacks file permission | `os.access(R_OK)` as the running user (a real check in Phase 0) | – |
| SQL used to read other files or write | Single SELECT/WITH only; data loaded first, then DuckDB external access disabled and config locked | ✓ |
| Large or bulk data leaving CSHL | 20,000-character cap per reply, 50-row cap, 2,000-read FASTQ sample, 200-file listings | partly |
| Sensitive data previews | `sensitivity: sensitive` → summaries only, no row queries | – |
| Claude rewrites lab knowledge | No write tool; suggestions are queued for a person | ✓ |
| Unauthenticated network access | HTTP mode refused without `--dev-no-auth` and bound to localhost | – |
| No record of what happened | Every call audited, including failures | ✓ |
| Another cluster user drives your portal | Portal binds to localhost only, **and** every API call needs the private link token (constant-time compare). Other users on the node can reach the port but not use it | ✓ |
| Portal writes outside a lab | Document paths must be `context.md` or `<category>/<slug>.md`; dataset paths must be under `BROWSE_ROOTS`, exist, be readable, and not be inside the Hub itself | ✓ |
| Folder browser used to explore the cluster | Limited to `BROWSE_ROOTS` (`/grid`) and to folders the user can already open | ✓ |
| Script injection through Markdown | Previews are sanitized with DOMPurify; `X-Frame-Options: DENY`, `nosniff`, no referrer | – |

**What leaves CSHL:** whatever a tool returns is sent to Anthropic as part of the conversation:
summaries, previews, query rows and document text. Raw files are not transferred, but returned
content is. That is why output caps and the `sensitive` flag matter, and why controlled-access
human data stays out of the pilot.

---

## 9. Phase 0 limitations

These are deliberate simplifications, or gaps found while writing this document:

1. **On-disk permissions must match Hub permissions.** The Hub's group check only governs what flows
   *through Claude*, so the folders are locked down on disk too (done 2026-11-03):

   | Path | Mode | Who can read |
   |---|---|---|
   | `knowledge/` | `2751` | others may pass through but not list it |
   | `knowledge/workflows.yaml` | `664` | everyone reads; BSR edits (the portal also checks BSR membership) |
   | `knowledge/<lab>/` | `2770` dirs / `660` files, group = lab's `unix_group` | that lab reads and edits |
   | `.git/` | group-writable, `core.sharedRepository=group` | BSR (so any member's portal saves are recorded) |
   | `~/.codespringhub/portal-token` | `700` / `600` | only that person |
   | `var/`, `var/hub.db` | `2770` / `660`, group `bioinformatics` | BSR only |

   The server creates `var/` and `hub.db` with these modes automatically, and `hub check` fails if
   any lab folder or `var/` is open to others, or if a lab folder's group doesn't match its
   `unix_group`. **Git does not store these permissions**, so a fresh clone must be re-locked; run
   `hub check`. In Phase 0 every BSR member who runs the server writes to the same audit log, so it
   is readable by BSR but not tamper-proof. Phase 2 fixes that with a single service account.
2. **Identity is "whoever started the process"** for stdio and the local portal. The code is ready
   for per-request users (`acting_as`), but file checks (`os.access`) still test the process user,
   which is only correct when the process *is* the user (see Phase 2).
3. **Reaching the portal needs an SSH tunnel.** That's fine for BSR staff, but too technical for most
   researchers. HTTPS hosting (Phase 2) removes it; until then BSR runs the portal for pilot labs or
   sits with them. Display names come from the cluster account (`gecos`), which may be just a surname.
4. **Keyword search only** (SQLite FTS5). It doesn't understand synonyms or meaning; pgvector is planned.
5. **Applying a suggestion is manual:** the Inbox opens the document beside the suggestion, a person
   edits and saves, then marks it applied.
6. **No BAM/VCF/h5ad inspectors** yet (planned with pysam / anndata).
7. **`query_table` loads up to 500 MB into memory** for each call.
8. **The `TODO` convention depends on the model following instructions.**
9. **Workflow statuses are placeholders.** All five are `candidate` with version `TODO` until BSR reviews them.

---

**Several people, one shared folder (fixed 2026-11-09).** The first version assumed one user: the
portal link was a single file in `var/` owned by its creator, the BSR folder was writable only by its
creator, and git refused to work in a repository owned by someone else. Now each person's link is in
their own home folder, lab folders and `.git` are group-writable, new files are created `660`, git runs
with `safe.directory` and an explicit committer (so it works for members who never configured git),
and a git failure is logged (`portal:history_not_recorded`) instead of blocking a save.

---

## 10. Phase 2 design: shared service for Claude Enterprise

| Concern | Phase 0 | Phase 2 |
|---|---|---|
| Transport | stdio, one process per user | Streamable HTTP, one service (portal + MCP together) |
| Identity | process owner; portal: private link | OAuth 2.1 / OIDC token from CSHL SSO → CSHL username: implement `OIDCAuth` in `identity.py` for the portal, and the MCP SDK's `token_verifier` for `/mcp`; both set `acting_as(user)` |
| Portal access | SSH tunnel + private link | `https://codespringhub.cshl.edu` (or similar), SSO login, no tunnel |
| MCP endpoint | stdio | mounted on the same app at `/mcp` |
| Group lookup | `getgrouplist` | same, via the host's LDAP/NSS |
| File permission check | `os.access` as the user | **open decision:** (a) a small helper run as the user through a narrowly scoped `sudo` rule, which is strongest; or (b) the service account's read access plus an explicit ACL check for the user (NFSv4 on /grid), which is simpler but weaker |
| Database | SQLite | PostgreSQL (+ pgvector for meaning-based search) |
| Network | none | HTTPS behind a CSHL reverse proxy, only Anthropic's published IP ranges allowed |
| Enterprise setup | – | org Owner adds a custom connector pointing to the Hub URL |
| Hosting | `bamdev1` dev node | a BSR VM or container (Apptainer/Podman), not a login node |

**Secrets** (OAuth client secret, database password) never go in the repository folder, because it is
group-readable. They live in a file with mode 600, owned by the service account, outside the repo.

---

## 11. Audit and pilot metrics

From the `audit` table, per lab, user and week:
- active users, calls per tool, failure rate and top error messages;
- characters returned (a stand-in for data leaving CSHL and for token load);
- which datasets and documents are used, and which never are;
- searches with no results (gaps in lab context);
- review tickets and suggestions raised, and how they were resolved.

These match the evaluation questions in the proposal: adoption, useful use cases, permission needs,
token usage and expert-review needs. The Phase 1 portal turns them into a dashboard.

---

## 12. Multi-lab pilot model (built November 2026)

The pilot runs inside CSHL without IT: each researcher's own Claude Desktop starts the Hub on the cluster
over **their own SSH login** (stdio MCP over SSH). The Hub therefore runs *as that researcher*, and
cluster permissions apply exactly. Nothing listens on a port, nothing is exposed, and no data is copied
to laptops. The portal's **Connect Claude** page gives each person their ready-to-paste Claude Desktop
setup, plus SSH-key steps for Mac and Windows (Claude can't type a password).

### 12.1 Who sees what

| | Lab-shared items | Personal items (me + PI) | Private items (only me) | Usage logs |
|---|---|---|---|---|
| Lab member | read and edit | only their own | only their own | their own |
| Lab PI (`knowledge/roles.yaml`) | read and edit | their own **and their members'** (read) | only their own | their own |
| BSR staff (`bioinformatics`) | read and edit **every** lab's (support role) | **no** (not authorised) | **no** | **everyone's**, with personal and private details withheld |
| Other labs | nothing | nothing | nothing | nothing |

- **Personal items** (datasets, code, documents) live in `knowledge/<lab>/members/<user>/` and have
  Hub IDs containing `/~<user>/`. The structure is ready for richer PI and lab-member account types later.
- **Private items** (added 17 Nov 2026) are for things a member wants to keep even from their PI. They
  live in `knowledge/<lab>/private/<user>/`, a separate folder with its own git history (a PI who can
  read `members/<user>/.git` can't reach them), and have Hub IDs containing `/~<user>/private/`. The
  portal offers three choices wherever something is created: the whole lab, me and my PI, only me.
- **PIs come only from `knowledge/roles.yaml`**, which only BSR can write (`hub set-pis`). A lab member
  editing their own `lab.yaml` cannot make themselves PI.
- **Code resources** (`kind: code`): Claude can read source files in full (`read_code_file`, size-capped),
  plus the git branch and last commit. Data resources still give summaries only.
- **Consent:** connecting means usage is shared with BSR. The Connect page, the `whoami`/`list_labs`
  replies and Claude's instructions all say so; the first time it's shown is recorded per person.

### 12.2 How the filesystem enforces it (NFSv4 ACLs on GPFS)

`/grid` is GPFS with **NFSv4 ACL semantics** (`mmlsfs grid -k` → `nfs4`), so `setfacl` fails there with
"Operation not supported". The Hub sets ACLs with GPFS's own `mmputacl`/`mmgetacl`, which any file owner
may run (`acl.py`); on other filesystems (and in most tests) it uses POSIX ACLs. Found and fixed 17 Nov
2026; before that only BSR's own folder existed, so nothing had gone wrong.

**GPFS catch:** a `chmod` *replaces* an NFSv4 ACL with one built from the mode bits. This always fails
closed (extra access disappears; nothing is opened up) and `hub check` reports it. The Hub's own code
therefore never chmods inside a folder that passes on an ACL (`acl.inherits`), and git runs there
without `core.sharedRepository` (whose chmod would do the same). Researchers should not chmod inside
`knowledge/`; if one does, the owner re-saving in the portal re-applies their own folder's ACL.

| Path | Access |
|---|---|
| `knowledge/` | BSR rwx; each pilot lab group r-x (list labs, read `workflows.yaml`, `roles.yaml`) |
| `knowledge/<lab>/` | BSR (owning group) + lab group rwx, inherited by everything created inside |
| `knowledge/<lab>/.git` | the lab's own history; inherits the lab folder's access; never pushed |
| `knowledge/<lab>/members/`, `private/` | lab group and BSR may **add** a folder (no delete or rename: sticky, and no delete right), nothing inherited inside |
| `knowledge/<lab>/members/<user>/` | owner + `user:<pi>:rwx` per PI, inherited; the lab's and BSR's access **removed**. Its own `.git` inside. Re-secured on every personal save, so PI changes take effect on the owner's next save |
| `knowledge/<lab>/private/<user>/` | owner only, inherited; no PI, no lab, no BSR. Its own `.git` inside |
| `var/` | BSR; pilot lab groups `--x` only |
| `var/events/` | `3770` (setgid + **sticky**); pilot lab groups `-wx`, so each person can create their log but can't list, read, delete or rename anyone else's. No default ACLs |
| `var/events/<user>.db` | `640`, no ACLs. Readers trust a row only if the file's owner *is* that user (a forged `ann.db` is ignored) |

**Why per-lab git repositories:** git stores full copies of every file. One shared repository would let
anyone who can read `.git` read every lab's files, including personal ones. It would also push lab
knowledge to GitHub. The main repository's `.gitignore` now keeps all labs except BSR out
(`knowledge/*/`, `!knowledge/bsr/`, `members/`, `private/`, `_suggestions/`, `roles.yaml`).

**Why logs are withheld at write time:** BSR can read the log files directly, so personal and private details are
replaced with `{"details": "withheld: personal resource"}` *before* they're written, not just hidden in
the portal. Lab-shared usage keeps full details (search words, dataset IDs) for the pilot evaluation.

### 12.3 Running a pilot lab

```bash
hub add-lab smith-lab --group "smith lab staff" --name "Smith Lab" --pi jsmith   # BSR, once per lab
hub set-pis smith-lab jsmith                                                    # BSR, when PIs change
hub check                                                                       # BSR: every permission
hub doctor                                                                      # anyone: does it work for me?
```

`add-lab` creates the folder with the right ACLs, a context template, an empty registry, the
`members/` folder, the lab's own history, and the group's access to `knowledge/` and `var/events/`.

**Researchers don't clone anything.** Everyone runs the one installed copy in the BSR folder, as
themselves, over SSH: Claude Desktop starts `env/bin/codespringhub` on the login node when it opens,
and the process ends when Claude closes. So a change to the live folder reaches every lab at once:
develop in a separate clone and update the live folder only after tests pass.

**Staying connected.** A laptop sleeping or changing network cuts the SSH link, and Claude Desktop
doesn't restart a tool server by itself. On Mac, the Connect page's setup command copies
`laptop/connect.py` to `~/.codespringhub/` and starts SSH through it. When the link dies, the helper
answers any request that was waiting with a "reconnecting, try again" error, starts SSH again with
growing pauses (2 s up to 60 s), and repeats Claude's original opening handshake so the conversation
carries on. If the very first connection fails (no key, wrong host), it exits like plain SSH so the
error shows up in Claude. Python standard library only; tested in `tests/test_connect_helper.py`
and over real SSH. Windows has no helper yet: restart Claude Desktop after a drop.

**Opening the portal from Claude.** The Claude Desktop setup's SSH command also carries
`-L 127.0.0.1:<portal port>:localhost:<portal port>` and the same for the person's CodeSpringApp port
(`41000 + uid % 20000` and `20000 + uid % 20000`), with `ExitOnForwardFailure=no` so a busy laptop port
never breaks the Hub. The `open_portal` tool (`portal_launch.py`) finds the person's portal on this login
node from `~/.codespringhub/portal-<node>.json` (written by the running portal), starts `portal.sh`
detached if there is none, and returns `http://localhost:<port>/#ticket=…`. The ticket is a single-use,
15-minute file in `~/.codespringhub/portal-tickets/` (700/600); the web app swaps it at `/api/redeem`
for the portal secret, which only the browser keeps. So the long-lived link never enters a chat, as with
CodeSpringApp's tickets in §13. Works the same on Windows (built-in `ssh.exe`) and through the Mac
reconnect helper. A portal started this way keeps running after Claude closes.

### 12.4 Limits of this model

- **Same trust as the cluster.** Everything runs as the researcher. A determined member could run their
  own code against the shared folders, but only within what their Unix account can already do.
- **Logs are trust-based.** A person's log is written by their own process, so they could stop it
  (e.g. by deleting their log file). It's good enough to evaluate the pilot, but it's not a security audit
  trail; Phase 2 makes it one.
- **BSR can't see personal data, but `root` can.** As with everything on the cluster.

---

## 13. Running BSR pipelines from Claude (built November 2026)

**Goal.** A researcher asks in Claude, in plain language, for an analysis. Claude learns from BSR's
own pipelines (CodeSpringApp driving CodeSpringLab's scripts) and runs them exactly as BSR does,
through CodeSpringApp's own code, then opens CodeSpringApp's Results Explorer. nf-core pipelines
are a secondary suggestion only. v1 said "no Slurm execution"; this section changes that, for bulk
RNA-seq first.

### 13.1 The pieces

```
Claude ──MCP──▶ Hub (runs.py) ──▶ codespring-run (CodeSpringApp, shared BSR install)
                 │  plan → user confirms → start          │  CodeSpringApp's own functions:
                 │  status, resume, open_results          │  new_project_from_inputs, submit_*_jobs,
                 │                                         │  job_history  → CodeSpringLab qsub_*.sh
                 ▼                                         ▼
        ~/.codespringhub/runs/ (plans, run list)    ~/csl_results/<project>/  +  ~/.codespringweb/
```

- **One BSR copy** of both repositories at `/grid/bsr/data/data/bsr_readable_data/CodeSpringSuite/`
  (`CodeSpringApp/`, `CodeSpringLab/`, and a `CODESPRING_MANAGED_INSTALL` marker). Everyone runs it
  under their own account; nobody needs their own clone. CodeSpringApp accepts this install outside
  home only because of the marker (it still refuses other people's home folders).
- **`bin/codespring-run`** (new in CodeSpringApp, branch `feature/hub-bridge`): `catalog`, `check`,
  `start`, `status`, `resume`. It sources `app.R` and calls the same functions the buttons call, so
  references, CodeSpringLab scripts, `#SBATCH` resources, folders and `jobs.tsv` records are identical
  to a run started in the app. The project appears in the person's CodeSpringApp project list.
- **The controller**: CodeSpringApp submits one step per button press, so a Hub run has a small
  detached process on the login node (like the app's own Sarek and nf-core CUT&RUN controllers)
  that submits each step when the previous one has finished and judges success by the step's
  expected output files. It uses no Slurm allocation (compute nodes don't have the app's R
  packages). If it stops (login node restart), `resume` continues; finished steps are kept.
- **Deep links + one-time tickets** (CodeSpringApp): `?project=rna/<name>&view=results#ticket=…`
  opens that project on the Results Explorer. It works like the portal's links: the ticket is a
  single-use file the Hub writes in `~/.codespringweb/open_tickets/` (600, 15-minute expiry), so the
  app's long-lived access token never enters a chat, and it sits in the `#fragment`, which browsers
  never send, so link previews and prefetches can't use it up. The app's page moves it into the
  address, spends it, and answers with a redirect that gives the browser an HttpOnly key cookie
  named for the user. As with the portal's saved secret, the key is kept in the person's own
  `~/.codespringweb/browser-key` (600) and the cookie lasts 30 days, so reopening the app, reloads,
  reconnects, a closed browser and an app restart all keep working; deleting that file signs every
  browser out. The Hub starts the shared app with idle shutdown off, so like the portal it keeps
  running after the last tab closes.
  A shared app started before its code changed is restarted by the Hub when it next needs one, as
  the portal is. Only the person's own saved projects can be opened.

### 13.2 Guardrails (who enforces what)

| Rule | Where |
|---|---|
| Only BSR pipelines, references, adapter presets, gene sets and options; no custom commands, runner paths, adapters or result folders | `codespring-run check` (CodeSpringApp) |
| Results only in the person's CodeSpringApp results root; an existing results folder is never cleared | CodeSpringApp |
| Sample names, column names and file names validated (no shell characters, no paths); every read file must exist | CodeSpringApp |
| Max 96 samples per run (BSR can change it: `CSL_HUB_MAX_SAMPLES` in the shared install); DESeq2 needs ≥2 replicates per group | CodeSpringApp |
| Per-sample jobs are submitted in **waves** under the person's Slurm submit limit (read from their QOS; 2 slots kept free) | CodeSpringApp controller |
| Extra quantification only from the app's own RSEM and Kallisto; at most 6 gene sets, all from BSR's list; only RSEM, Kallisto and GSEA can be skipped, and only before they run | CodeSpringApp + Hub |
| Email updates only if the person chose them, only to `<their user>@cshl.edu` (no address can be given), at most hourly summaries | CodeSpringApp |
| Cancel stops only this run: the controller is signalled only after checking `/proc/<pid>/cmdline` names this run's spec file (never a reused pid), and only job ids recorded in this project's job history are cancelled (no job-name sweep: `a.b` and `a_b` share a prefix) | CodeSpringApp |
| Nothing from the status file reaches a shell; the stored request is re-checked by the controller before it runs | CodeSpringApp |
| Waves fail closed: if `squeue` can't be asked, nothing is submitted; after 12 h without free slots, or an hour without a readable job list, the run stops with a message (resume later) | CodeSpringApp controller |
| Starting, continuing and resuming all count against the 3-runs limit; a run on another login node being cancelled shows `cancelling` until its controller stops | Hub + CodeSpringApp |
| Continuing and resuming (submit more work) and cancelling need the person's go-ahead in the chat (`user_confirmed`), like starting | Hub |
| Paused runs don't count towards the 3-runs limit (they hold no cluster resources) | Hub |
| Inputs must be Hub-registered **data** the person can see (lab-shared, or their own personal); files can't escape the dataset | Hub |
| Nothing runs without a plan the user saw and confirmed; plans are single-use, expire after 2 h, are tamper-checked, and belong to one person | Hub |
| A run can only start under the account of the person asking | Hub |
| At most 3 Hub-started runs per person at once | Hub |
| `CSL_*` environment overrides are ignored; the shared install decides | Hub |
| Claude must follow BSR pipelines, never substitute tools or write ad-hoc Slurm commands | Server instructions + `list_workflows` rule |
| Every plan, start, status and open is logged to BSR like other tools (personal datasets withheld) | Hub |

Claude Desktop also asks the person before each tool call unless they allow it permanently.

### 13.3 Step by step, optional steps, QC, cancel, emails (built 17 Nov 2026)

- **Modes.** `run_mode` `all` runs every step in turn; `step` makes the controller stop after each step with
  state `paused` (it exits: a pause holds nothing and can last days). `continue_analysis` relaunches
  it, optionally adding RSEM/Kallisto (`quantifiers`), skipping RSEM, Kallisto or GSEA, or switching
  run mode. RSEM/Kallisto can also be added to a finished run. Steps run in BSR's order (Cutadapt,
  FastQC, STAR, featureCounts, DESeq2, GSEA, then RSEM and Kallisto). Several gene sets (up to 6)
  run as one GSEApy job each. These names come from main's spec version 2 (James, 17 Nov), which
  this work was merged onto.
- **Waves.** For per-sample steps the controller submits as many samples as the person's free Slurm
  submit slots allow (QOS `MaxSubmitPU` or the association's `MaxSubmit`, minus jobs already queued,
  minus 2), then more as slots free up. CodeSpringApp's own submit functions skip samples that are
  done or already queued, so a wave never duplicates work. A matrix-build job held back while earlier
  waves were still running is submitted by one finishing pass once every sample is done.
- **QC** (`codespring-run qc`, `analysis_qc`) reads each tool's own output: Cutadapt and featureCounts
  through the app's metric readers, STAR's `Log.final.out`, FastQC's `summary.txt` inside each report
  zip, Kallisto's `run_info.json`. No thresholds are applied (BSR to decide; see §14).
- **Cancel** writes a cancel flag (a controller on another node stops at its next check and marks
  the run `cancelled`; until then it shows `cancelling`), stops a local controller's process group
  after verifying it really is this run's controller, and cancels each step's recorded active jobs
  with the app's `cancel_active_step_jobs`. Every controller exit clears its pid. `resume` carries on.
- **Reviewed** 17 Nov 2026 by an independent review pass: stale-pid kills, the job-name-prefix
  sweep, shell injection via the status file, double controllers after a remote cancel, the run
  limit being bypassable through continue/resume, and waves that could wait forever were all found
  and fixed, with tests.
- **Emails** (optional) are sent by the controller with the cluster's `mail` to the person's own
  address: when a step finishes, fails or pauses, when the run ends or is cancelled, and optionally
  a summary every N hours. Claude itself can't send updates between turns.

### 13.4 The post-run report packet (built 17 Nov 2026)

- `codespring-run report` (CodeSpringApp `R/hub_report.R`) writes one self-contained HTML page to
  `<results>/report/<project>_codespring_report.html`. The controller makes it when a run finishes
  (and the "finished" email gives its path); `analysis_report` rebuilds it on request, also mid-run.
- It only gathers CodeSpringApp's own outputs, recalculating nothing: steps, a per-sample QC table
  (the same numbers as `analysis_qc`), the DESeq2 step's PNGs (PCA, volcano, MA), DEG counts and
  top genes from DESeq2's FDR < 0.05 list, GSEApy's top pathways and dot plots, and the app's
  methods text and tool table.
- Plots and small files (DEG lists, design, GSEA tables, normalized counts, featureCounts summary,
  QC CSV) are embedded as downloads. Size caps: 5 MB per data file, 15 MB per page, 30 plots
  (`CSL_HUB_REPORT_FILE_MB`, `CSL_HUB_REPORT_MAX_MB`). Anything bigger (count/Kallisto/RSEM matrices)
  is listed with its cluster path. All text from result files is HTML-escaped; the page loads
  nothing from the internet.
- In the chat, `analysis_report` returns a summary, up to 6 of the plots as images, and a
  **one-time download link** for the HTML served by the person's own portal
  (`/api/download/<ticket>`: the ticket file, in their 700 `~/.codespringhub/download-tickets/`,
  names the file; it works once, expires with other links, and only `*_codespring_report.html`
  files are ever served). `open_results` remains the way into the full, interactive results.

### 13.5 Updating the shared install

BSR updates `CodeSpringSuite` deliberately, after tests pass (`tests/run_all.sh` in CodeSpringApp;
`pytest` here). During development it was synced from the `feature/hub-bridge` worktree; once James
merges the branch, replace it with clones pinned to a commit of each repository.

### 13.6 Not yet

- ATAC, ChIP, CUT&RUN, single-cell, Sarek and FetchNGS still start from CodeSpringApp only.
- The report packet isn't linked from CodeSpringApp's own Results Explorer yet.
- The controller lives on the login node it was started on (`bamdev1`), like the app's own.

---

## 14. Open decisions

| # | Decision | Owner | Needed by |
|---|---|---|---|
| 1 | Which workflows are BSR-validated, and at which versions | BSR | Phase 1 |
| 2 | Fill in the TODOs in BSR context.md (references, Slurm conventions, contacts) | BSR | Phase 1 |
| 3 | Where uploaded lab documents (grants, notes) live, and their permissions | BSR + labs | Phase 1 |
| 4 | Keyword search vs. meaning-based search, and which embedding model (local vs. external) | BSR | Phase 1 |
| 5 | CSHL identity provider and LDAP access | IT | Phase 2 |
| 6 | Per-user file check: sudo helper vs. ACL check | BSR + IT | Phase 2 |
| 7 | Hosting and Anthropic reachability | IT | Phase 2 |
| 8 | PostgreSQL: existing server or self-hosted | IT | Phase 2 |
| 9 | Three pilot labs: groups, paths, sensitivity classes (not Koo or Tuveson) | BSR + PIs | before onboarding |
| 11 | Which SSH host researchers' Claude Desktop connects to (default `bamdev1.cshl.edu`; set `CODESPRINGHUB_CONNECT_HOST`) | BSR (+ HPC admins) | before onboarding |
| 12 | Confirm CSHL's Claude Enterprise setup allows local MCP servers in Claude Desktop | AI admin | before onboarding |
| 10 | Git remote: personal GitHub vs. CSHL/BSR organization | Aarushi | now |
| 13 | James reviews and merges CodeSpringApp `feature/hub-bridge`; then pin the shared install to that commit | James + BSR | before onboarding |
| 14 | HPC admins OK with small Hub run controllers on the login node (same pattern as CodeSpringApp's Sarek) | BSR + HPC admins | before onboarding |
| 15 | QC cut-offs to flag in chat (e.g. STAR uniquely mapped %), if any; today only tools' own numbers and FastQC's flags are shown | BSR | before pilot |
| 16 | Largest run from Claude (now 96 samples, batched) | BSR | before pilot |

---

## 15. Core facilities (built 17 Nov 2026)

CSHL's core facilities (BSR, Sequencing, Microscopy, Flow Cytometry, ...) are shared resources:
they have things every lab should be able to read (protocols, submission guidelines, example data,
scripts) and things only their own staff should see. A core is a folder in `knowledge/` whose
`lab.yaml` says `kind: core`.

### 15.1 Layout and who sees what

| Folder | Hub IDs | Who sees it |
|---|---|---|
| `<core>/lab.yaml` | | everyone using the Hub (name, website, contacts) |
| `<core>/shared/` (context.md, datasets.yaml, documents) | `doc:<core>/shared/...`, `ds:<core>/shared/...` | everyone using the Hub: every lab group, every core's staff, BSR |
| `<core>/internal/` | `doc:<core>/internal/...`, `ds:<core>/internal/...` | the core's staff (its Unix group) only: not BSR, not other labs |
| `<core>/members/<user>/`, `<core>/private/<user>/` | as for labs | staff member + the core's lead / only the owner |

- The shared section is explicit: nothing is shared unless a staff member puts it there, so an
  item can't become visible to everyone by accident. A core's top folder holds only `lab.yaml` and
  these four folders, so other labs can't even see the names of internal documents.
- Only the core's staff edit its pages (no sign-off step). Others can't suggest edits through the
  Hub; Claude points them to the core's contacts instead.
- `list_labs` returns the person's labs, then the cores (BSR first) with `kind`, `sees`
  ("shared" or "shared and internal"), `on_hub`, `website` and `contacts`. Search covers every
  core's shared section; a core's internal section only for its staff.
- Every lab's context (`get_lab_context`) ends with a guide to all the cores: each one's name, one
  line, whether it's on the Hub, and its shared documents. So Claude knows what each core offers
  (and which protocols to cite) in every conversation, without being asked.
- Portal: a core's own staff find it under "Your labs" (full view: shared, internal, personal,
  history). The BSR link and the "Cores" dropdown open the public view (shared section only,
  read-only) for everyone, the core's own staff included.
- Each section has its own git history (`shared/.git`, `internal/.git`), so reading the shared
  history reveals nothing internal. BSR's shared section stays in the Hub's own repository; every
  `internal/` folder is git-ignored and never reaches GitHub.

### 15.2 On disk

`acl.grant_core` (GPFS NFSv4 or POSIX): the core folder and `lab.yaml` give the staff group rwx and
every Hub group r-x (nothing inherited); `shared/` gives staff rwx and every Hub group r-x,
inherited; `internal/` gives only the staff group rwx, inherited, and closes the owning (BSR) group.
`hub add-lab` opens every core's shared section to a new lab (`grant_core_reader`); `hub check`
reports any internal file another group can read. As with labs, only a file's owner can change its
ACL, and the BSR member who ran `hub add-core` stays the owner of the folders it created (that
person can still read `internal/`; IT can hand ownership to the core if wanted).

### 15.3 Templates for cores not on the Hub yet

`hub seed-cores` makes a starter page for every core in `knowledge/cshl_cores.yaml` (17 cores,
taken from CSHL's public core facilities list on 17 Nov 2026; the intranet page needs a login).
Each gets `kind: core`, no staff group (`on_hub: false`), its website and one-line description,
and a shared context page with TODO sections (services, how to submit, protocols, data, contacts).
Everyone sees it, Claude says details may be incomplete, and BSR keeps it until the core joins
(BSR edits only its shared page; nobody gets an internal view of a core without staff):
`hub add-core <id> --group <staff group> --lead <user> --contact "Name <email>"`, which hands the
pages to the core's staff and takes BSR out of the internal section.

### 15.4 BSR as a core

BSR moved from a lab to a core: its guidance, protocols, example datasets and context page are now
its shared section (`doc:bsr/shared/guidance/codespring-pipelines`,
`ds:bsr/shared/codespringlab-rnaseq-example`), which every lab can finally read (before, only the
bioinformatics group could). BSR still supports every pilot lab as before. In the portal, BSR has
its own sidebar link and the other cores sit in a "Cores" dropdown, for every user in every lab.

### 15.5 Not yet

- Per-core workflow lists and pipelines run from Claude (today only BSR's RNA-seq pipeline).
- A core lead approving shared changes (the user chose direct edits for now).
- Real contacts and staff groups for the 17 templates: each core has to agree to join.

---

## 16. Which CodeSpringApp pipelines labs may use (built 17 Nov 2026)

The Hub reaches CodeSpringApp through `bin/codespring-run` in BSR's shared install (§13): the
app's catalog lists its pipelines, and bulk RNA-seq can be started from a chat. BSR decides, per
pipeline, who may use it through the Hub, in `knowledge/bsr/shared/pipelines.yaml`:

| Access | Who | Shipped for |
|---|---|---|
| `public` | every lab using the Hub: Claude offers it and may start it from the chat (where the app allows) | bulk RNA-seq, ATAC, ChIP, CUT&RUN, FetchNGS (running standard tools) |
| `internal` | BSR staff, plus any labs listed under `labs:`; everyone else is told BSR runs it for them and offered `request_bsr_review` | single-cell, Sarek (analysis that needs BSR's judgement) |

- A pipeline not in the file gets `default: internal`, and an unknown access value counts as
  internal, so nothing becomes public by accident.
- Enforced in `list_workflows` (each pipeline carries `access` and `you_can_use`), `analysis_options`,
  `plan_analysis` and again in `start_analysis` (a change after planning still applies).
- BSR staff change it on the portal's BSR workflows page ("CodeSpringApp pipelines: who can use
  them": open to every lab, make BSR-only, or choose labs). Each change is committed and logged.
- This governs what Claude offers and starts. It doesn't lock the CodeSpringApp install itself:
  someone who opens CodeSpringApp directly can still run any pipeline. Locking the app per group
  would be a CodeSpringApp change (James).

---

## 17. BSR's Agent: one name, a voice, and people (built 17 Nov 2026)

- **One name.** Everything says *BSR's Agent*. "Bioinformatics Shared Resource" and "CodeSpring"
  describe it rather than being names of their own (three names would make people wonder which
  tool they're trusting). Claude Desktop shows the connection as **BSR Agent** (new setups add it
  and remove the old `codespringhub` entry); the server declares the title "BSR's Agent:
  Bioinformatics Shared Resource (CodeSpring)", BSR's website and a BSR icon for apps that show them.
- **Voice: a bench colleague.** Warm, plain, "let's"/"we" where it fits. Every reply that used a Hub
  tool ends with `— BSR's Agent · Bioinformatics Shared Resource, CSHL · CodeSpring`; replies that
  didn't use the Hub have none, so people can tell when BSR's configured tools are behind an answer.
- **Offers to bring in a person** (`bsr_check_in` in a tool result; Claude adds one line above the
  sign-off): every 6 Hub calls (counted by the Hub, since Claude can't count reliably), when a run
  finishes or fails, when someone isn't in any lab on the Hub yet (offer to set their lab up), and,
  per the instructions, when someone asks about interpretation or which analysis to choose.
- **`email_bsr`**: a question, setting up CodeSpringHub for a lab, or a meeting about an analysis,
  optionally with Claude's short summary of the chat. It returns the exact draft first and sends
  only with `user_confirmed`; it goes through the cluster's `mail` to BSR's support contacts
  (`knowledge/support_contacts.yaml`), copy and reply-to the person, and is logged as a BSR review
  ticket (portal Inbox). It refuses other people's personal items and other cores' internal items;
  the usage log keeps only the purpose, never the draft text.

---

## 18. Bulk ATAC-seq and ChIP-seq from Claude (built 17 Nov 2026)

The same plan -> confirm -> start, step-by-step, cancel, waves, emails, QC and report as RNA-seq
(§13), for CodeSpringApp's ATAC-seq and ChIP-seq pipelines. CodeSpringApp `R/hub_peaks.R` checks,
plans and runs them by calling the app's own functions; the run engine in `R/hub_bridge.R` now
takes the pipeline from the request (`rnaseq`, `atac`, `chip`).

| | ATAC-seq | ChIP-seq |
|---|---|---|
| Steps | Cutadapt, FastQC, Bowtie2, MACS2 Peaks, Differential Peaks (DiffBind) | Cutadapt, FastQC, Bowtie2, MACS2 Peaks (each target against its matched input), Differential Peaks (DiffBind on targets), Peak Annotation (HOMER) |
| App defaults kept | Nextera adapters, MACS2 q 0.05, paired-end only | TruSeq adapters, MACS2 q 0.01, narrow peaks (broad on request) |
| Design | sample, grouping columns, filename | plus `reference` (chip / input) and `control_sample` (each target's input row) |
| QC | Bowtie2 mapped reads and % kept after deduplication, MACS2 peaks per sample (each tool's own summary files) | the same, with each sample's role |
| Report | DiffBind's plots (PCA, correlation heatmaps, volcano, ...), its differential-peak table and counts (higher / lower by DiffBind's Fold), QC table | the same plus HOMER's annotation summary |

- Claude asks only the app's own choices: run mode, email updates and, for ChIP, narrow or broad
  peaks. The comparison, species and design come from the dataset. Pathway gene sets and RSEM /
  Kallisto are refused for these pipelines.
- Storage: the plan shows free space and raw read size, and says the output size hasn't been
  measured for ATAC/ChIP yet (the RNA-seq estimate isn't reused).
- BSR's public/internal setting (§16) applies per pipeline; both ship public.
- Tested end to end on CodeSpringLab's example data (4 ATAC samples; 4 ChIP targets + 1 input).
