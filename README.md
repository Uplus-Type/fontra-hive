# fontra-hive

Git-backed storage and team collaboration plug-in for [Fontra](https://fontra.xyz),
the open-source, browser-based font editor.

**Status: phase 0, work in progress.** What exists today:

- `fontra_hive.gitstore.GitRepoStore` — one *bare* git repository per project.
  Every commit's tree is a complete `.fontra` package, so `git clone` of a
  project gives a folder Fontra opens as is. Atomic commits with
  compare-and-swap on the branch reference, branches, annotated tags,
  per-path history, tree diffs, import/export. Pure Python (dulwich), no
  shell, no working tree.
- `fontra_hive.backend_git.GitFontraBackend` — a Fontra backend that reads and
  writes a branch. It reuses Fontra's own `.fontra` serialisation untouched
  (the files committed are byte-for-byte what `FontraBackend` writes on disk),
  groups edits into commits authored by the user who made them, and detects
  changes made to the branch by someone else.
- `fontra hive-dev <folder>` — a development project manager (no
  authentication) serving every `<name>.git` repository of a folder, one
  project per branch (`Name`, `Name@branch`).
- `fontra-hive` — a small CLI: `init`, `import`, `export`, `log`, `branches`,
  `branch`, `tag`, `diff`.

Fontra itself is not modified: the plug-in registers through the
`fontra.projectmanagers` entry point and uses public backend APIs only.

## How it fits together

```
browser  ──websocket──▶  Fontra server (unmodified)
                            └── hive-dev ProjectManager
                                  └── FontHandler per project@branch
                                        └── GitFontraBackend
                                              └── GitRepoStore  ──▶  <name>.git (bare)
```

The commercial side of Fontra Hive (accounts, roles, hosting) lives in
separate services that talk to the Fontra server over HTTP; nothing of it is
in this repository.

## Try it

```bash
# 1. Fontra from source, in a virtualenv (Python >= 3.11, Node >= 24):
#    see https://github.com/fontra/fontra#install-from-the-source-code
git clone https://github.com/fontra/fontra.git && cd fontra
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt && pip install -e .

# 2. this plug-in, in the same virtualenv (do not `pip install fontra`:
#    the PyPI package of that name is unrelated to the editor)
cd ../fontra-hive
pip install -e ".[dev]"

# create a project repository from an existing .fontra package
fontra-hive init repos/MyFont.git path/to/MyFont.fontra

# serve it
fontra hive-dev repos --launch
# then open  http://localhost:8000/  → project "MyFont"

# meanwhile, in another terminal
fontra-hive log repos/MyFont.git                 # commits, newest first
fontra-hive log repos/MyFont.git --path glyphs/A.json
fontra-hive branch repos/MyFont.git bold-extension
fontra-hive tag repos/MyFont.git v0.1 --message "sent to client"
fontra-hive export repos/MyFont.git v0.1 out/MyFont-v0.1.fontra
git --git-dir repos/MyFont.git log --stat          # it is a normal git repo
```

Edits made in the editor are committed about two seconds after the last
change (`--commit-delay`), one commit per user. Open `MyFont@bold-extension`
to edit the branch.

## Tests

```bash
pytest
```

The tests import a `.fontra` package into a bare repository, compare the git
backend with Fontra's file-system backend, apply the same edits to both and
check the exported files are identical byte for byte, then exercise scheduled
commits, attribution, external changes, concurrent commits, read-only mode,
and that the repository is readable by `git` itself.

## Design notes

- **Format first.** The repository tree *is* the `.fontra` package; no
  database schema mirrors the format, so a format change in Fontra needs no
  migration here.
- **One writer per branch in the Fontra server, compare-and-swap in git.**
  The `FontHandler` serialises edits in memory; the backend commits behind it.
  If the branch moved meanwhile (a merge, another server), the pending files
  are re-applied on the new head.
- **Attribution.** The commit author is taken from the
  `fontra_hive.backend_git.current_author` context variable, set per
  connection by the project manager. Until Fontra exposes the writing
  connection to backends (a small upstream change to discuss), the
  development manager uses a single author.
- **External changes.** The branch head is polled (`poll_interval`); a tree
  diff between the known and the new head yields exactly the glyphs to reload,
  through the same code path Fontra uses for changed files on disk.

## Licence

GPLv3, like Fontra. See `LICENSE`.
