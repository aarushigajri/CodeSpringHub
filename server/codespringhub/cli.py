"""`hub`: the admin command line.

    hub add-lab <id> --group G --name N [--pi U ...]   set up a pilot lab (BSR)
    hub set-pis <id> [U ...]                           change a lab's PIs (or a core's lead) (BSR)
    hub add-core <id> --group G [--name N] [--lead U ...] [--website URL] [--contact "Name <email>" ...]
                                                       put a core facility on the Hub, or onboard its template (BSR)
    hub seed-cores                                     template pages for CSHL cores not on the Hub yet (BSR)
    hub doctor                                         check YOUR connection works (anyone)
    hub check                                          check every permission (BSR)
    hub status                                         what the Hub currently exposes to you
    hub suggestions | reviews | audit [-n N]           inbox and usage logs
"""

import argparse
import getpass
import grp
import os
import pwd
import stat
import sys

import yaml

from . import acl, config, editing, hub, knowledge, store
from .permissions import can_access_lab, is_bsr, user_groups

ME = getpass.getuser()


def _need_bsr():
    if not is_bsr(ME):
        sys.exit(f"Only BSR staff ({config.BSR_GROUP}) can do this.")


def _open_to_others(path) -> bool:
    return path.exists() and bool(path.stat().st_mode & 0o007)


# ---- labs and roles --------------------------------------------------------------

def _save_roles(roles: dict):
    path = config.KNOWLEDGE_DIR / "roles.yaml"
    header = "# PIs per lab. BSR-managed: change with `hub set-pis`, not by hand.\n"
    editing._write_atomic(path, header + yaml.safe_dump({"labs": roles}, sort_keys=True))
    os.chmod(path, 0o664)


def cmd_add_lab(args):
    _need_bsr()
    if not editing.SLUG.match(args.lab_id):
        sys.exit("Lab IDs use lowercase letters, numbers and dashes, e.g. smith-lab.")
    try:
        grp.getgrnam(args.group)
    except KeyError:
        sys.exit(f"Unix group '{args.group}' doesn't exist. `getent group | grep -i <name>` lists candidates.")
    for pi in args.pi:
        try:
            pwd.getpwnam(pi)
        except KeyError:
            sys.exit(f"No cluster user '{pi}'.")
        if args.group not in user_groups(pi):
            print(f"warning: PI {pi} is not in group '{args.group}', so won't see members' personal items yet.")
    lab_dir = config.KNOWLEDGE_DIR / args.lab_id
    if lab_dir.exists():
        sys.exit(f"{lab_dir} already exists.")

    lab_dir.mkdir()
    acl.grant_lab(lab_dir, args.group)
    (lab_dir / "lab.yaml").write_text(editing.YAML_HEADER + yaml.safe_dump(
        {"name": args.name, "unix_group": args.group, "description": "", "projects": []}, sort_keys=False))
    (lab_dir / "context.md").write_text(
        f"# {args.name}: lab context\n\n## Research areas\nTODO\n\n## Organisms and models\nTODO\n\n"
        "## Terms and abbreviations we use\nTODO\n\n## Reference genomes and annotation\nTODO\n\n"
        "## How we like analyses done\nTODO\n")
    (lab_dir / "datasets.yaml").write_text(editing.YAML_HEADER + "datasets: []\n")
    (lab_dir / "members").mkdir(exist_ok=True)
    (lab_dir / "private").mkdir(exist_ok=True)
    for f in lab_dir.rglob("*"):
        os.chmod(f, 0o2770 if f.is_dir() else 0o660)
    # The lab's own history: lab members can read it, other labs can't, nothing is pushed.
    editing.init_history(lab_dir, ["members/", "private/", "_suggestions/"])
    editing._commit(ME, [lab_dir / "lab.yaml", lab_dir / "context.md", lab_dir / "datasets.yaml",
                         lab_dir / ".gitignore"], f"add lab {args.lab_id}")
    acl.grant_lab(lab_dir, args.group)  # again, so .git inherits the lab's access too
    acl.grant_lab_paths(args.group)
    for core in knowledge.load_labs().values():  # every core's shared section is for this lab too
        if core.is_core:
            try:
                acl.grant_core_reader(core.dir, args.group)
            except acl.ACLError as e:
                print(f"warning: couldn't open {core.id}'s shared section to the lab ({e}); its owner can re-run `hub add-core`.")

    roles = knowledge.load_roles()
    roles[args.lab_id] = {"pis": list(args.pi)}
    _save_roles(roles)
    print(f"Added {args.name} ({args.lab_id}), group '{args.group}', PIs: {', '.join(args.pi) or 'none yet'}.")
    print("Next: open the portal, fill in the lab page with the lab, then have members connect Claude.")


# ---- core facilities ----------------------------------------------------------------

CORE_SHARED_CONTEXT = """# {name}

> What every lab using CodeSpringHub sees about {name}. {status}
> Lines marked TODO are ignored as guidance.

## What we do
{description}

Website: {website}

## Services
TODO: what labs can ask us for.

## How to request a service or submit samples
TODO: forms, sample requirements, turnaround times.

## Protocols and guidelines we share
TODO: link the documents in this shared section (e.g. protocols/sample-submission).

## Data we return and where it goes
TODO: formats, folders on /grid, how long we keep it.

## Contacts
TODO: who to ask, and how.
"""

CORE_INTERNAL_CONTEXT = """# {name}: internal

> Only {name} staff see this section (and Claude, when one of them is using it).
> Nothing here is shown to other labs. Lines marked TODO are ignored as guidance.

## How we work
TODO: internal procedures, instrument notes, QC thresholds we use.

## Conventions
TODO: naming, folders, pipelines and versions.
"""


def _hub_groups() -> list[str]:
    """Every group that should read the cores' shared sections: labs, cores, BSR."""
    return sorted({lab.unix_group for lab in knowledge.load_labs().values() if lab.unix_group} | {config.BSR_GROUP})


def _parse_contact(text: str) -> dict:
    text = text.strip()
    if "<" in text and text.endswith(">"):
        name, email = text[:-1].split("<", 1)
        return {"name": name.strip(), "email": email.strip()}
    return {"name": text}


def _write_core(core_dir, data: dict, status: str):
    core_dir.mkdir()
    for sub in ("shared", "internal", "members", "private"):
        (core_dir / sub).mkdir()
    (core_dir / "lab.yaml").write_text(editing.YAML_HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    fill = {"name": data["name"], "description": data.get("description") or "TODO", "status": status,
            "website": data.get("website") or "TODO"}
    (core_dir / "shared" / "context.md").write_text(CORE_SHARED_CONTEXT.format(**fill))
    (core_dir / "internal" / "context.md").write_text(CORE_INTERNAL_CONTEXT.format(**fill))
    for sub in ("shared", "internal"):
        (core_dir / sub / "datasets.yaml").write_text(editing.YAML_HEADER + "datasets: []\n")
        # Each section keeps its own history, so reading the shared history shows nothing internal.
        editing.init_history(core_dir / sub, ["_suggestions/"])
        editing._commit(ME, [core_dir / sub / "context.md", core_dir / sub / "datasets.yaml",
                             core_dir / sub / ".gitignore"], f"add {core_dir.name} {sub} section")


def cmd_add_core(args):
    _need_bsr()
    if not editing.SLUG.match(args.core_id):
        sys.exit("Core IDs use lowercase letters, numbers and dashes, e.g. sequencing.")
    try:
        grp.getgrnam(args.group)
    except KeyError:
        sys.exit(f"Unix group '{args.group}' doesn't exist. `getent group | grep -i <name>` lists candidates.")
    for u in args.lead:
        try:
            pwd.getpwnam(u)
        except KeyError:
            sys.exit(f"No cluster user '{u}'.")
    core_dir = config.KNOWLEDGE_DIR / args.core_id
    contacts = [_parse_contact(c) for c in args.contact]
    if core_dir.exists():
        path = core_dir / "lab.yaml"
        data = yaml.safe_load(path.read_text()) or {}
        if data.get("kind") != "core":
            sys.exit(f"{core_dir} is a lab, not a core.")
        if data.get("unix_group") and data["unix_group"] != args.group:
            sys.exit(f"{args.core_id} is already on the Hub with group '{data['unix_group']}'.")
        data["unix_group"] = args.group
        for key, val in (("name", args.name), ("website", args.website)):
            if val:
                data[key] = val
        if contacts:
            data["contacts"] = contacts
        editing._write_atomic(path, editing.YAML_HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        verb = "Onboarded"
    else:
        if not args.name:
            sys.exit("Give the core's name with --name (or run `hub seed-cores` first for CSHL's cores).")
        _write_core(core_dir, {"kind": "core", "name": args.name, "unix_group": args.group,
                               "description": "", "website": args.website or "", "contacts": contacts,
                               "projects": []}, "Maintained by the core's staff.")
        verb = "Added"
    acl.grant_core(core_dir, args.group, _hub_groups())
    acl.grant_lab_paths(args.group)
    roles = knowledge.load_roles()
    roles[args.core_id] = {"pis": list(args.lead)}
    _save_roles(roles)
    print(f"{verb} core {args.core_id}: staff group '{args.group}', lead(s): {', '.join(args.lead) or 'none yet'}.")
    print("Staff see its shared and internal sections in the portal; everyone else sees the shared section.")


def cmd_seed_cores(_):
    _need_bsr()
    catalog = yaml.safe_load((config.KNOWLEDGE_DIR / "cshl_cores.yaml").read_text()) or {}
    made, groups = [], _hub_groups()
    for entry in catalog.get("cores", []):
        core_dir = config.KNOWLEDGE_DIR / entry["id"]
        if core_dir.exists():
            continue
        _write_core(core_dir, {"kind": "core", "name": entry["name"], "unix_group": "",
                               "description": entry.get("description", ""), "website": entry.get("website", ""),
                               "contacts": [], "projects": [],
                               "source": f"Template from {catalog.get('source', 'the CSHL core list')} ({catalog.get('retrieved', '')})"},
                    f"This is a starter page made by BSR from CSHL's public core list; {entry['name']} isn't on "
                    "CodeSpringHub yet, so details may be incomplete. Check the website or ask the core.")
        acl.grant_core(core_dir, None, groups)
        made.append(entry["id"])
    print(f"Made {len(made)} core template(s): {', '.join(made) or 'none (all exist already)'}.")
    if made:
        print("BSR keeps them until each core joins: `hub add-core <id> --group <staff group> --lead <user>`.")


def cmd_set_pis(args):
    _need_bsr()
    labs = knowledge.load_labs()
    if args.lab_id not in labs:
        sys.exit(f"No lab '{args.lab_id}'.")
    roles = knowledge.load_roles()
    roles[args.lab_id] = {"pis": list(args.users)}
    _save_roles(roles)
    print(f"PIs for {args.lab_id}: {', '.join(args.users) or 'none'}.")
    print("Members' personal folders pick this up the next time each member saves something in the portal.")


# ---- checks ------------------------------------------------------------------------

def cmd_doctor(_):
    """Anyone can run this: does the Hub work for me?"""
    ok = True
    print(f"You are {ME}.")
    labs = hub.accessible_labs(ME)
    print(f"Labs you can use: {', '.join(labs) or 'none'}")
    if not labs:
        ok = False
        print("  → You're not in any pilot lab's group. Ask BSR to add your lab.")
    try:
        store.note_once("doctor")
        print("Usage log: OK (shared with BSR)")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"Usage log: NOT WORKING ({e}). Ask BSR to run `hub check`.")
    try:
        store.index_connect().close()
        print(f"Personal search index: OK ({config.USER_DIR})")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"Personal search index: NOT WORKING ({e})")
    print("Everything looks fine." if ok else "Some things need fixing (see above).")
    return 0 if ok else 1


def cmd_check(_):
    _need_bsr()
    problems = 0

    def bad(msg):
        nonlocal problems
        problems += 1
        print(msg)

    for path in (config.KNOWLEDGE_DIR, config.VAR_DIR, config.DB_PATH):
        if _open_to_others(path):
            bad(f"[permissions] {path} is open to all cluster users (fix: chmod o-rwx)")
    events = config.VAR_DIR / "events"
    if events.exists():
        mode = events.stat().st_mode
        if not (mode & stat.S_ISVTX):
            bad(f"[events] {events} is missing the sticky bit (fix: chmod +t)")
        if any(e.startswith("default:") for e in acl.entries(events)):
            bad(f"[events] {events} has default ACLs; people could read each other's logs")
        for f in events.glob("*.db"):
            owner = pwd.getpwuid(f.stat().st_uid).pw_name
            if f.stem != owner:
                bad(f"[events] {f.name} is owned by {owner}: possible impersonation, investigate")
            if f.stat().st_mode & 0o037 or acl.entries(f):
                bad(f"[events] {f.name} is readable beyond its owner and BSR (should be 640, no ACL)")

    ignore = (config.HUB_HOME / ".gitignore")
    ignored = ignore.read_text() if ignore.exists() else ""
    for needed in ("knowledge/*/members/", "knowledge/*/private/"):
        if needed not in ignored:
            bad(f"[git] .gitignore doesn't keep {needed} out of the main (GitHub) repository")
    for lab in knowledge.load_labs().values():
        if lab.is_core:
            problems += _check_core(lab, bad)
            continue
        try:
            gid = grp.getgrnam(lab.unix_group).gr_gid
        except KeyError:
            bad(f"[{lab.id}] unix_group '{lab.unix_group}' does not exist")
            continue
        if _open_to_others(lab.dir):
            bad(f"[{lab.id}] {lab.dir} is open to all cluster users")
        if lab.id != "bsr":
            if f"group:{gid}:rwx" not in acl.entries(lab.dir):
                bad(f"[{lab.id}] lab group has no access to its folder (re-run `hub add-lab` steps)")
            if not (lab.dir / ".git").exists():
                bad(f"[{lab.id}] has no history of its own")
            if "knowledge/*/" not in ignored:
                bad("[git] .gitignore doesn't keep lab knowledge out of the main (GitHub) repository")
        elif not lab.dir.stat().st_mode & 0o020:
            print(f"[{lab.id}] note: members can view but not edit this lab (to allow: chmod -R g+w)")
        for owner, d in knowledge.member_dirs(lab):
            for p in acl.personal_is_private(d, lab.pis):
                bad(f"[{lab.id}] personal: {p}")
        for owner, d in knowledge.private_dirs(lab):
            for p in acl.personal_is_private(d, []):
                bad(f"[{lab.id}] private: {p}")
        project_ids = {p.id for p in lab.projects}
        for ds in knowledge._datasets_in(lab.dir / "datasets.yaml", lab, None).values():
            if not os.path.exists(ds.path):
                bad(f"[{ds.id}] path does not exist")
            if ds.project and ds.project not in project_ids:
                bad(f"[{ds.id}] project '{ds.project}' is not in lab.yaml")
    print(f"{len(knowledge.load_labs())} lab(s) checked, {problems} problem(s).")
    return 1 if problems else 0


def _check_core(core, bad) -> int:
    before = 0
    if core.onboarded:
        try:
            grp.getgrnam(core.unix_group)
        except KeyError:
            bad(f"[{core.id}] staff group '{core.unix_group}' does not exist")
            return 0
    if _open_to_others(core.dir):
        bad(f"[{core.id}] {core.dir} is open to all cluster users")
    for sub in ("shared", "internal"):
        if not (core.dir / sub).is_dir():
            bad(f"[{core.id}] has no {sub}/ section (re-run `hub add-core`)")
    for p in acl.core_problems(core.dir, core.unix_group or None):
        bad(f"[{core.id}] internal: {p}")
    for owner, d in knowledge.member_dirs(core):
        for p in acl.personal_is_private(d, core.pis):
            bad(f"[{core.id}] personal: {p}")
    for owner, d in knowledge.private_dirs(core):
        for p in acl.personal_is_private(d, []):
            bad(f"[{core.id}] private: {p}")
    for ds in knowledge.load_datasets(core).values():
        if not ds.owner and not os.path.exists(ds.path):
            bad(f"[{ds.id}] path does not exist")
    return before


def cmd_status(_):
    for lab in hub.accessible_labs(ME).values():
        print(f"{lab.id}: {lab.name}  (group: {lab.unix_group}; PIs: {', '.join(lab.pis) or '-'})")
        print(f"  projects:  {', '.join(p.id for p in lab.projects) or '-'}")
        docs, data = hub.visible_documents(ME, lab), hub.visible_datasets(ME, lab)
        print(f"  documents: {len(docs)} ({sum(1 for d in docs if d.owner)} personal)  "
              f"resources: {len(data)} ({sum(1 for d in data if d.kind == 'code')} code)")
    print(f"workflows: {len(knowledge.load_workflows())}")


def cmd_suggestions(_):
    rows = [s for s in editing.inbox(ME)["suggestions"] if s.get("status") == "pending"]
    for r in rows:
        print(f"{r['id']} {r['user']} -> {r['lab_id']} {r.get('doc_id') or '(lab context)'}\n  {r['suggestion']}\n")
    if not rows:
        print("(no pending suggestions)")


def cmd_reviews(_):
    rows = editing.inbox(ME)["reviews"]
    for r in rows:
        print(f"{r['key']} [{r['status']}] {r['ts']} {r['user']} ({r['lab_id']}) datasets={r['resource_ids']}\n  {r['summary']}\n")
    if not rows:
        print("(no review tickets)")


def cmd_audit(args):
    for r in editing.activity(ME, args.n):
        print("  ".join(f"{c}={r[c]}" for c in ["ts", "user", "lab_id", "tool", "ok", "chars_out", "args"]))


def main():
    parser = argparse.ArgumentParser(prog="hub", description="CodeSpringHub admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add-lab", help="set up a pilot lab (BSR)")
    add.add_argument("lab_id")
    add.add_argument("--group", required=True, help="the lab's Unix group")
    add.add_argument("--name", required=True, help="display name, e.g. 'Smith Lab'")
    add.add_argument("--pi", action="append", default=[], help="PI username (repeatable)")
    add.set_defaults(fn=cmd_add_lab)
    pis = sub.add_parser("set-pis", help="set a lab's PIs (BSR)")
    pis.add_argument("lab_id")
    pis.add_argument("users", nargs="*")
    pis.set_defaults(fn=cmd_set_pis)
    core = sub.add_parser("add-core", help="put a core facility on the Hub, or onboard its template (BSR)")
    core.add_argument("core_id")
    core.add_argument("--group", required=True, help="the core staff's Unix group")
    core.add_argument("--name", help="display name (needed unless a template exists)")
    core.add_argument("--lead", action="append", default=[], help="the core's lead (repeatable)")
    core.add_argument("--website", help="the core's web page")
    core.add_argument("--contact", action="append", default=[], help='"Name <email>" (repeatable)')
    core.set_defaults(fn=cmd_add_core)
    sub.add_parser("seed-cores", help="template pages for CSHL cores not on the Hub yet (BSR)").set_defaults(fn=cmd_seed_cores)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("suggestions").set_defaults(fn=cmd_suggestions)
    sub.add_parser("reviews").set_defaults(fn=cmd_reviews)
    audit = sub.add_parser("audit")
    audit.add_argument("-n", type=int, default=20)
    audit.set_defaults(fn=cmd_audit)
    args = parser.parse_args()
    raise SystemExit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
