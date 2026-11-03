"""`hub`: the Phase 0 admin command line (the web portal will replace most of this).

    hub check          validate the knowledge folder (YAML, groups, dataset paths)
    hub status         what the Hub currently exposes
    hub suggestions    context edits Claude proposed, waiting for a person
    hub reviews        BSR review tickets
    hub audit [-n N]   the last N tool calls
"""

import argparse
import grp
import os

from . import config, knowledge, store


def _open_to_others(path) -> bool:
    return path.exists() and bool(path.stat().st_mode & 0o007)


def cmd_check(_):
    problems = 0
    for path in (config.VAR_DIR, config.DB_PATH):
        if _open_to_others(path):
            print(f"[permissions] {path} is accessible to all cluster users (fix: chmod o-rwx)")
            problems += 1
    labs = knowledge.load_labs()
    for lab in labs.values():
        try:
            grp.getgrnam(lab.unix_group)
        except KeyError:
            print(f"[{lab.id}] unix_group '{lab.unix_group}' does not exist")
            problems += 1
        # On disk, a lab's knowledge folder must be as private as it is through the Hub.
        if _open_to_others(lab.dir):
            print(f"[{lab.id}] {lab.dir} is accessible to all cluster users (fix: chmod 2750)")
            problems += 1
        dir_group = grp.getgrgid(lab.dir.stat().st_gid).gr_name
        if dir_group != lab.unix_group:
            print(f"[{lab.id}] folder group is '{dir_group}', expected '{lab.unix_group}' (fix: chgrp -R)")
            problems += 1
        if not (lab.dir / "context.md").exists():
            print(f"[{lab.id}] has no context.md")
        project_ids = {p.id for p in lab.projects}
        for ds in knowledge.load_datasets(lab).values():
            if not os.path.exists(ds.path):
                print(f"[{ds.id}] path does not exist")
                problems += 1
            if ds.project and ds.project not in project_ids:
                print(f"[{ds.id}] project '{ds.project}' is not in lab.yaml")
                problems += 1
            if ds.sensitivity not in ("standard", "sensitive"):
                print(f"[{ds.id}] sensitivity must be 'standard' or 'sensitive'")
                problems += 1
    knowledge.load_workflows()
    print(f"{len(labs)} lab(s) checked, {problems} problem(s).")
    return 1 if problems else 0


def cmd_status(_):
    conn = store.connect()
    rebuilt = store.ensure_index(conn)
    for lab in knowledge.load_labs().values():
        docs = knowledge.load_documents(lab)
        print(f"{lab.id}: {lab.name}  (group: {lab.unix_group})")
        print(f"  projects:  {', '.join(p.id for p in lab.projects) or '-'}")
        print(f"  documents: {len(docs)}  datasets: {len(knowledge.load_datasets(lab))}")
    print(f"workflows: {len(knowledge.load_workflows())}")
    print("search index:", "rebuilt now" if rebuilt else "up to date")


def _print_rows(rows, cols):
    for r in rows:
        print("  ".join(f"{c}={r[c]}" for c in cols))
    if not rows:
        print("(none)")


def cmd_suggestions(_):
    conn = store.connect()
    rows = conn.execute("SELECT * FROM suggestions WHERE status='pending' ORDER BY id").fetchall()
    for r in rows:
        print(f"#{r['id']} {r['ts']} {r['user']} -> {r['lab_id']} {r['doc_id'] or '(context.md)'}\n  {r['suggestion']}\n")
    if not rows:
        print("(no pending suggestions)")


def cmd_reviews(_):
    conn = store.connect()
    rows = conn.execute("SELECT * FROM reviews ORDER BY id DESC").fetchall()
    for r in rows:
        print(f"#{r['id']} [{r['status']}] {r['ts']} {r['user']} ({r['lab_id']}) datasets={r['resource_ids']}\n  {r['summary']}\n")
    if not rows:
        print("(no review tickets)")


def cmd_audit(args):
    conn = store.connect()
    rows = conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (args.n,)).fetchall()
    _print_rows(rows, ["ts", "user", "tool", "ok", "chars_out", "args"])


def main():
    parser = argparse.ArgumentParser(prog="hub", description="CodeSpringHub admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
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
