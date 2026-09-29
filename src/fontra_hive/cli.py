"""``fontra-hive`` command line: create, import, export and inspect project repositories.

Examples::

    fontra-hive init  repos/MyFont.git  path/to/MyFont.fontra
    fontra-hive export repos/MyFont.git main  out/MyFont.fontra
    fontra-hive log   repos/MyFont.git --path glyphs/A.json
    fontra-hive branch repos/MyFont.git bold-extension
    fontra-hive snapshot repos/MyFont.git "Proofs sent to client"
    fontra-hive log   repos/MyFont.git --snapshots
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import sys

from .gitstore import DEFAULT_BRANCH, GitRepoStore, Signature


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fontra-hive")
    parser.add_argument("--author-name", default="Fontra Hive")
    parser.add_argument("--author-email", default="hive@fontrahive.com")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a bare repository from a .fontra folder")
    p.add_argument("repo", type=pathlib.Path)
    p.add_argument("source", type=pathlib.Path, help=".fontra folder to import")
    p.add_argument("--message", default="Import")

    p = sub.add_parser("import", help="commit a .fontra folder on top of a branch")
    p.add_argument("repo", type=pathlib.Path)
    p.add_argument("source", type=pathlib.Path)
    p.add_argument("--branch", default=DEFAULT_BRANCH)
    p.add_argument("--message", default="Import")

    p = sub.add_parser(
        "export", help="write a commit, branch or tag as a .fontra folder"
    )
    p.add_argument("repo", type=pathlib.Path)
    p.add_argument("ref")
    p.add_argument("destination", type=pathlib.Path)

    p = sub.add_parser("log", help="list commits, newest first")
    p.add_argument("repo", type=pathlib.Path)
    p.add_argument("--ref", default=DEFAULT_BRANCH)
    p.add_argument("--path", help="only commits touching this path, e.g. glyphs/A.json")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument(
        "--snapshots",
        action="store_true",
        help="group commits under the snapshot they belong to (first-parent line)",
    )

    p = sub.add_parser(
        "snapshot",
        help="name the current state of a branch, grouping the commits since "
        "the previous snapshot (an empty commit + a snapshot/<name> tag)",
    )
    p.add_argument("repo", type=pathlib.Path)
    p.add_argument("name")
    p.add_argument("--branch", default=DEFAULT_BRANCH)

    p = sub.add_parser("branches", help="list branches and tags")
    p.add_argument("repo", type=pathlib.Path)

    p = sub.add_parser("branch", help="create a branch")
    p.add_argument("repo", type=pathlib.Path)
    p.add_argument("name")
    p.add_argument("--from", dest="from_ref", default=DEFAULT_BRANCH)

    p = sub.add_parser("tag", help="create an annotated tag (a named version)")
    p.add_argument("repo", type=pathlib.Path)
    p.add_argument("name")
    p.add_argument("--ref", default=DEFAULT_BRANCH)
    p.add_argument("--message", default="")

    p = sub.add_parser("diff", help="list files that differ between two refs")
    p.add_argument("repo", type=pathlib.Path)
    p.add_argument("old")
    p.add_argument("new")

    args = parser.parse_args(argv)
    author = Signature(args.author_name, args.author_email)

    if args.command == "init":
        store = GitRepoStore.create(args.repo)
        sha = store.import_directory(args.source, message=args.message, author=author)
        print(f"{args.repo}: main at {sha}")
        return 0

    store = GitRepoStore.open(args.repo)
    try:
        if args.command == "import":
            sha = store.import_directory(
                args.source, branch=args.branch, message=args.message, author=author
            )
            print(f"{args.branch} at {sha}")
        elif args.command == "export":
            store.export(args.ref, args.destination)
            print(f"exported {args.ref} to {args.destination}")
        elif args.command == "log":
            snapshots = [] if args.snapshots else None
            commits = store.log(
                args.ref, path=args.path, limit=args.limit, snapshots=snapshots
            )
            titles = {s.name: s for s in snapshots or []}
            group: object = ...
            for c in commits:
                if snapshots is not None and c.snapshot != group:
                    group = c.snapshot
                    if group is None:
                        print("── since the last snapshot")
                    else:
                        s = titles[group]
                        print(
                            f"── snapshot {s.title!r} ({s.name}, {s.sha[:10]}, "
                            f"{_when(s.time)}, {_changes(s.changes)})"
                        )
                indent = "   " if snapshots is not None else ""
                print(
                    f"{indent}{c.sha[:10]}  {_when(c.time)}  {c.author:<20}  "
                    f"{c.message.splitlines()[0]}"
                )
        elif args.command == "snapshot":
            try:
                s = store.create_snapshot(args.name, branch=args.branch, author=author)
            except ValueError as error:
                print(f"fontra-hive: {error}", file=sys.stderr)
                return 1
            print(f"snapshot/{s.name} -> {s.sha}  ({_changes(s.changes)} grouped)")
        elif args.command == "branches":
            for b in store.branches():
                print(f"branch {b:<24} {store.head(b)}")
            for t in store.tags():
                print(f"tag    {t:<24} {store.resolve(t)}")
        elif args.command == "branch":
            print(f"{args.name} at {store.create_branch(args.name, args.from_ref)}")
        elif args.command == "tag":
            sha = store.create_tag(args.name, args.ref, args.message, tagger=author)
            print(f"{args.name} -> {sha}")
        elif args.command == "diff":
            for change in store.diff(args.old, args.new):
                print(f"{change.kind:<7} {change.path}")
    finally:
        store.close()
    return 0


def _changes(count: int) -> str:
    return f"{count} change{'' if count == 1 else 's'}"


def _when(unix_time: int) -> str:
    when = datetime.datetime.fromtimestamp(unix_time).astimezone()
    return when.strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    sys.exit(main())
