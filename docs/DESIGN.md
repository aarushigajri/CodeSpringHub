# CodeSpringHub.v1: Design

*Bioinformatics Shared Resource (BSR), Cold Spring Harbor Laboratory. Status: Phase 0 built (November 2026).
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

### 3.2 Target (Phase 2+)

```
 Researcher ─► Claude Enterprise ─HTTPS + OAuth─► CSHL reverse proxy ─► CodeSpringHub service
                (Anthropic cloud)                 (Anthropic IPs only)     │  token → CSHL user
 Lab / BSR ──► Hub Portal (SSO) ──────────────────────────────────────────►│
                                                                           ├─ PostgreSQL (registry, audit)
                                                                           ├─ knowledge/ (git)
                                                                           └─ per-user file helper ─► /grid/
```

Section 10 covers what changes between the two.

### 3.3 Code layout

| File | Responsibility |
|---|---|
| `server/codespringhub/server.py` | MCP server, Claude instructions, the 12 tools, 1 prompt, the audit/limit wrapper |
| `server/codespringhub/permissions.py` | Current user, Unix-group lookup, lab access check, safe path resolution |
| `server/codespringhub/knowledge.py` | Reads `knowledge/` into Lab / Project / Dataset / Document objects |
| `server/codespringhub/inspect_data.py` | File summaries (table, FASTQ, text) and sandboxed SQL |
| `server/codespringhub/store.py` | SQLite: full-text index, audit, reviews, suggestions |
| `server/codespringhub/config.py` | Paths and output limits |
| `server/codespringhub/cli.py` | `hub` admin command |
| `tests/test_server.py` | End-to-end tests through a real MCP client |

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

### 5.2 Machine state (`var/hub.db`, SQLite)

| Table | Contents | Lifecycle |
|---|---|---|
| `docs_fts` | Full-text index (FTS5): doc_id, lab_id, category, title, body | Rebuilt automatically from files |
| `meta` | `fingerprint` of the knowledge folder | Updated on rebuild |
| `audit` | ts, user, tool, args (JSON), ok, chars_out, error | Append-only |
| `reviews` | ts, user, lab_id, summary, resource_ids, status (`open`) | Created by Claude, handled by BSR |
| `suggestions` | ts, user, lab_id, doc_id, suggestion, status (`pending`) | Created by Claude, handled by a person |

Deleting `var/` loses only the audit history, reviews and suggestions. The index rebuilds itself.

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
| Lab member / BSR | Add, edit or remove context, documents, datasets | Edit the files (Phase 0), portal editor (Phase 1) |
| Claude | **Suggest** a change only | `suggest_context_edit` → `suggestions` table |
| Reviewer | Accept or reject suggestions | `hub suggestions` to view, then edit the file |

Claude can never write to `knowledge/`. That keeps a person responsible for everything the AI is told
is "lab knowledge".

**Conventions**
- `_`-prefixed files are drafts or templates and are never shown to Claude.
- `TODO` marks unfinished text. The server instructions tell Claude not to treat it as guidance.
  *This relies on the model following instructions; it is not enforced in code.*
- Git provides history and attribution (`git log -p knowledge/`).

**Phase 1 portal:** an editor over the same files, plus a review step: lab edits go live immediately,
and changes to BSR guidance need BSR approval. Saving writes the file and makes a git commit as the
editing user.

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
   | `knowledge/workflows.yaml` | `644` | everyone (global registry) |
   | `knowledge/<lab>/` | `2750`, group = lab's `unix_group` | that lab only |
   | `var/`, `var/hub.db` | `2770` / `660`, group `bioinformatics` | BSR only |

   The server creates `var/` and `hub.db` with these modes automatically, and `hub check` fails if
   any lab folder or `var/` is open to others, or if a lab folder's group doesn't match its
   `unix_group`. **Git does not store these permissions**, so a fresh clone must be re-locked; run
   `hub check`. In Phase 0 every BSR member who runs the server writes to the same audit log, so it
   is readable by BSR but not tamper-proof. Phase 2 fixes that with a single service account.
2. **Identity is "whoever started the process".** That is correct for stdio, but it cannot serve
   several users from one shared service (see Phase 2).
3. **Keyword search only** (SQLite FTS5). It doesn't understand synonyms or meaning; pgvector is planned.
4. **Suggestions have no "apply" command** yet. A person reads them and edits the file by hand.
5. **No BAM/VCF/h5ad inspectors** yet (planned with pysam / anndata).
6. **`query_table` loads up to 500 MB into memory** for each call.
7. **The `TODO` convention depends on the model following instructions.**
8. **Workflow statuses are placeholders.** All five are `candidate` with version `TODO` until BSR reviews them.

---

## 10. Phase 2 design: shared service for Claude Enterprise

| Concern | Phase 0 | Phase 2 |
|---|---|---|
| Transport | stdio, one process per user | Streamable HTTP, one service |
| Identity | process owner | OAuth 2.1 token from CSHL SSO → CSHL username (MCP SDK `token_verifier` / `AuthSettings`) |
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

## 12. Open decisions

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
| 9 | Three pilot labs: groups, paths, sensitivity classes | BSR + PIs | Phase 3 |
| 10 | Git remote: personal GitHub vs. CSHL/BSR organization | Aarushi | now |
