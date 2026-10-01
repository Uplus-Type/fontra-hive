"""``fontra-hive`` command line: create, import, export and inspect project repositories.

Examples::

    fontra-hive init  repos/MyFont.git  path/to/MyFont.fontra
    fontra-hive export repos/MyFont.git main  out/MyFont.fontra
    fontra-hive log   repos/MyFont.git --path glyphs/A.json
    fontra-hive branch repos/MyFont.git bold-extension
    fontra-hive snapshot repos/MyFont.git "Proofs sent to client"
    fontra-hive log   repos/MyFont.git --snapshots
    fontra-hive pull  repos/MyFont.git https://github.com/me/MyFont.git \\
                      --path MyFont.designspace
    fontra-hive push  repos/MyFont.git https://github.com/me/MyFont.git \\
                      --path MyFont.designspace -m "Proofs for the client"

``pull``/``push``/``remote-status`` read a token from ``$GITHUB_TOKEN`` (or
the variable named by ``--token-env``), never from the command line.

With hive-api, a project ``owner/name`` lives in ``<root>/<uid>.git``;
``repo-of`` prints that name, to use with the commands above::

    fontra-hive init repos/$(fontra-hive repo-of uplustype/Mutator) Mutator.fontra
"""

from __future__ import annotations

import argparse
import datetime
import os
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

    p = sub.add_parser(
        "repo-of", help="the repository of a hive-api project (owner/name): <uid>.git"
    )
    p.add_argument("project")
    p.add_argument(
        "--api", default=os.environ.get("HIVE_API_URL", "http://127.0.0.1:8001")
    )
    p.add_argument(
        "--service-key", default=os.environ.get("HIVE_SERVICE_KEY", "dev-service-key")
    )

    def remote_args(p):
        p.add_argument("repo", type=pathlib.Path)
        p.add_argument("url", help="https://… or, for tests, a local path")
        p.add_argument("--remote-branch", default="main")
        p.add_argument(
            "--path",
            default="",
            help="the font in the remote: X.designspace, X.ufo, X.fontra "
            "(default: the repository root is a .fontra package)",
        )
        p.add_argument("--token-env", default="GITHUB_TOKEN")
        p.add_argument("--username", default="x-access-token")

    p = sub.add_parser(
        "pull", help="fetch a remote git branch into upstream/<remote branch>"
    )
    remote_args(p)
    p.add_argument("--base", default=DEFAULT_BRANCH, help="first pull: fork from")

    p = sub.add_parser("push", help="send a branch's changes to a remote git branch")
    remote_args(p)
    p.add_argument("--branch", default=DEFAULT_BRANCH)
    p.add_argument("-m", "--message", required=True)

    p = sub.add_parser("remote-status", help="a branch against a remote git branch")
    remote_args(p)
    p.add_argument("--branch", default=DEFAULT_BRANCH)

    p = sub.add_parser("diff", help="list files that differ between two refs")
    p.add_argument("repo", type=pathlib.Path)
    p.add_argument("old")
    p.add_argument("new")

    args = parser.parse_args(argv)
    author = Signature(args.author_name, args.author_email)

    if args.command == "repo-of":
        return _repoOf(args)

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
        elif args.command in ("pull", "push", "remote-status"):
            return _remote(store, args, author)
        elif args.command == "diff":
            for change in store.diff(args.old, args.new):
                print(f"{change.kind:<7} {change.path}")
    finally:
        store.close()
    return 0


def _remote(store, args, author) -> int:
    from . import remote as hive_remote

    token = os.environ.get(args.token_env)
    remote = hive_remote.Remote(
        args.url,
        branch=args.remote_branch,
        path=args.path,
        username=args.username if token else None,
        password=token,
    )
    # A command run by hand on one's own machine may use local addresses.
    local = not args.url.startswith("https://")
    try:
        if args.command == "pull":
            r = hive_remote.pull(
                store, remote, base_branch=args.base, allow_local=local
            )
            if r.commit is None:
                print(f"{r.branch}: up to date ({(r.remote_sha or 'empty')[:10]})")
            else:
                print(f"{r.branch} at {r.commit} ({len(r.glyphs)} glyphs changed)")
                print(f"merge it: the branch menu, or {r.branch} into {args.base}")
        elif args.command == "push":
            r = hive_remote.push(
                store,
                remote,
                branch=args.branch,
                message=args.message,
                author=author,
                allow_local=local,
            )
            if r.pushed:
                print(f"{remote.branch} at {r.remote_sha}: {len(r.files)} files")
                for path in r.files:
                    print(f"  {path}")
            else:
                print("nothing to push")
            for path in r.skipped:
                print(f"  not sent (not supported for UFO yet): {path}")
        else:
            s = hive_remote.status(store, remote, args.branch, allow_local=local)
            print(f"remote {remote.branch}: {s.remote_sha}")
            print(f"last sync:  {s.synced_sha}")
            if s.remote_moved:
                print("the remote has new commits: pull")
            if s.unmerged:
                print(f"the last pull is not merged into {args.branch}")
            print(f"{len(s.pending)} files to push")
    except hive_remote.RemoteError as error:
        print(f"fontra-hive: {error}", file=sys.stderr)
        return 1
    return 0


def _repoOf(args) -> int:
    import asyncio

    from .hiveapi import HiveApi, HiveApiUnavailable

    async def ask():
        api = HiveApi(args.api, args.service_key)
        try:
            return await api.project(args.project)
        finally:
            await api.aclose()

    try:
        project = asyncio.run(ask())
    except HiveApiUnavailable as error:
        print(f"fontra-hive: hive-api: {error}", file=sys.stderr)
        return 1
    if project is None:
        print(f"fontra-hive: no project {args.project}", file=sys.stderr)
        return 1
    print(project["repo"])
    return 0


def _changes(count: int) -> str:
    return f"{count} change{'' if count == 1 else 's'}"


def _when(unix_time: int) -> str:
    when = datetime.datetime.fromtimestamp(unix_time).astimezone()
    return when.strftime("%Y-%m-%d %H:%M")


if __name__ == "__main__":
    sys.exit(main())
