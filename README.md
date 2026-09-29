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

## Glyph history in the editor (plug-in)

The package also ships an editor plug-in: a "Glyph history" panel in the right
sidebar listing every commit that touched the selected glyph (author, time,
message), refreshed as you edit. It uses Fontra's editor plug-in mechanism
and is served by the Fontra server itself under `/hive/plugin/`.

- **Preview a version on the canvas.** Click a commit: that version of the
  glyph is drawn in orange over the glyph itself, in glyph coordinates, so
  the two outlines can be compared point by point. The layer shown is the
  one being edited when the old version has it, otherwise its default
  source layer (the status line says which); components are resolved
  against the same version. The overlay is a visualization layer added at
  runtime, below the editing nodes.
- **Restore this version.** With a version previewed, "Restore this version"
  (click twice: the button asks to confirm) makes the server commit that
  glyph file again on top of the branch. Nothing is rewritten or deleted:
  the restore is a new commit, marked `Hive-Restore: <sha>`, attributed to
  the user, and every editor connected to the project reloads the glyph
  through the same path as any external change. The restore is not part
  of the editor's undo stack; restoring the previous version undoes it.

To enable the panel (once per browser): open *Application settings →
Plugins*, add the address `/hive/plugin`, then reload the editor. The panel
appears as a clock icon in the right sidebar.

Why manual registration: Fontra's plug-in list is per browser (local
storage), with no way for a server to declare plug-ins yet. That is one of
the extension points to propose upstream; until then, this one step is
needed.

Routes used by the panel (also handy from the command line):

```
GET  /api/hive/projects/<name>/head?branch=main
GET  /api/hive/projects/<name>/log?branch=main&glyph=A&limit=50
GET  /api/hive/projects/<name>/glyph?glyph=A&ref=<sha|branch|tag>
GET  /api/hive/projects/<name>/branches
POST /api/hive/projects/<name>/restore?branch=main&glyph=A&ref=<sha|tag>
```

The panel polls `head` every 1.5 s (a few bytes) and reloads the list only
when the branch moved. Per-glyph history is answered from the `Hive-Glyphs:`
trailer that Hive writes in every commit message, so it costs a walk over
commit objects, not tree diffs: about 50 ms for 200 commits on a
30,000-glyph project with dulwich. Commits without the trailer (an import,
an external commit) fall back to comparing the glyph's blob with the parent's.

Note for large CJK projects: with dulwich (pure Python plus small C helpers),
a commit on a 30,000-glyph tree takes about 0.3 s and a full tree diff about
the same; fine for batched commits, and where pygit2/libgit2 would be used
in production for a 10× margin.

## Tests

```bash
pytest
```

The tests import a `.fontra` package into a bare repository, compare the git
backend with Fontra's file-system backend, apply the same edits to both and
check the exported files are identical byte for byte, then exercise scheduled
commits, attribution, external changes, concurrent commits, read-only mode,
the HTTP routes (history, glyph at a ref, restore, including restore into a
project open in a running backend), and that the repository is readable by
`git` itself.

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
