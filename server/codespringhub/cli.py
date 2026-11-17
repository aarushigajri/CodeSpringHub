"""`hub`: the admin command line.

    hub add-lab <id> --group G --name N [--pi U ...]   set up a pilot lab (BSR)
    hub set-pis <id> [U ...]                           change a lab's PIs (BSR)
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

    roles = knowledge.load_roles()
    roles[args.lab_id] = {"pis": list(args.pi)}
    _save_roles(roles)
    print(f"Added {args.name} ({args.lab_id}), group '{args.group}', PIs: {', '.join(args.pi) or 'none yet'}.")
    print("Next: open the portal, fill in the lab page with the lab, then have members connect Claude.")


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
