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
| **Dataset** | A registered pointer to a file or folder on `/grid/`. The data is never copied. | `ds:bsr/demo-rnaseq` |
| **Asset** | One file inside a dataset, named relative to the dataset folder. | `reads/S1_R1.fastq.gz` |
| **Document** | A Markdown knowledge file (context, guidance, protocol, note). | `doc:bsr/guidance/ai-assisted-analysis` |
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
2. **Claude sends the call:** `query_table(dataset_id="ds:bsr/demo-rnaseq", asset="samplesheet.csv",
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
  - id: demo-rnaseq                 # becomes ds:bsr/demo-rnaseq
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
| File permission check | `os.access` as the user | **open decision:** (a) a small helper run as the user through a narrowly scoped `sudo` rule, which is strongest; or (b) the service account's read access plus an explicit POSIX ACL check for the user, which is simpler but weaker |
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

| | Lab-shared items | Personal items | Usage logs |
|---|---|---|---|
| Lab member | read and edit | only their own | their own |
| Lab PI (`knowledge/roles.yaml`) | read and edit | their own **and their members'** (read) | their own |
| BSR staff (`bioinformatics`) | read and edit **every** lab's (support role) | **no** (not authorised) | **everyone's**, with personal details withheld |
| Other labs | nothing | nothing | nothing |

- **Personal items** (datasets, code, documents) live in `knowledge/<lab>/members/<user>/` and have
  Hub IDs containing `/~<user>/`. The structure is ready for richer PI and lab-member account types later.
- **PIs come only from `knowledge/roles.yaml`**, which only BSR can write (`hub set-pis`). A lab member
  editing their own `lab.yaml` cannot make themselves PI.
- **Code resources** (`kind: code`): Claude can read source files in full (`read_code_file`, size-capped),
  plus the git branch and last commit. Data resources still give summaries only.
- **Consent:** connecting means usage is shared with BSR. The Connect page, the `whoami`/`list_labs`
  replies and Claude's instructions all say so; the first time it's shown is recorded per person.

### 12.2 How the filesystem enforces it (POSIX ACLs on GPFS)

| Path | Access |
|---|---|
| `knowledge/` | BSR rwx; each pilot lab group r-x (list labs, read `workflows.yaml`, `roles.yaml`) |
| `knowledge/<lab>/` | BSR (owning group) + lab group rwx, inherited by everything created inside |
| `knowledge/<lab>/.git` | the lab's own history; inherits the lab folder's access; never pushed |
| `knowledge/<lab>/members/<user>/` | `2700` with inherited ACLs **removed**; plus `u:<pi>:rwx` per PI. Its own `.git` inside. Re-secured on every personal save, so PI changes take effect on the owner's next save |
| `var/` | BSR; pilot lab groups `--x` only |
| `var/events/` | `3770` (setgid + **sticky**); pilot lab groups `-wx`, so each person can create their log but can't list, read, delete or rename anyone else's. No default ACLs |
| `var/events/<user>.db` | `640`, no ACLs. Readers trust a row only if the file's owner *is* that user (a forged `ann.db` is ignored) |

**Why per-lab git repositories:** git stores full copies of every file. One shared repository would let
anyone who can read `.git` read every lab's files, including personal ones. It would also push lab
knowledge to GitHub. The main repository's `.gitignore` now keeps all labs except BSR out
(`knowledge/*/`, `!knowledge/bsr/`, `members/`, `_suggestions/`, `roles.yaml`).

**Why logs are withheld at write time:** BSR can read the log files directly, so personal details are
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

### 12.4 Limits of this model

- **Same trust as the cluster.** Everything runs as the researcher. A determined member could run their
  own code against the shared folders, but only within what their Unix account can already do.
- **Logs are trust-based.** A person's log is written by their own process, so they could stop it
  (e.g. by deleting their log file). It's good enough to evaluate the pilot, but it's not a security audit
  trail; Phase 2 makes it one.
- **BSR can't see personal data, but `root` can.** As with everything on the cluster.

---

## 13. Open decisions

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
