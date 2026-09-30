<p align="center">
  <a href="https://fontrahive.com"><img src="src/fontra_hive/client/icons/hive-icon.svg" alt="Fontra Hive" width="160"></a>
</p>

# fontra-hive

Git-backed storage and team collaboration plug-in for [Fontra](https://fontra.xyz),
the open-source, browser-based font editor. It is the open part of
[**Fontra Hive**](https://fontrahive.com), Fontra for Teams
(beta, by invitation).

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
- Comments on glyphs: numbered topics pinned to a point of a glyph's source,
  kept on `refs/hive/comments` (see below).
- `fontra-hive` — a small CLI: `init`, `import`, `export`, `log`, `branches`,
  `branch`, `tag`, `diff`, `snapshot`.

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
fontra-hive snapshot repos/MyFont.git "Proofs sent to client"
fontra-hive log repos/MyFont.git --snapshots      # commits grouped by snapshot
fontra-hive export repos/MyFont.git v0.1 out/MyFont-v0.1.fontra
git --git-dir repos/MyFont.git log --stat          # it is a normal git repo
```

Edits made in the editor are committed about two seconds after the last
change (`--commit-delay`), one commit per user. Open `MyFont@bold-extension`
to edit the branch.

## Accounts and roles (development)

Put a `hive-dev-users.json` next to the repositories (or pass `--users FILE`)
and the development server gets accounts, following GitHub's model: users
with a unique username and an email; organizations with owners and members;
projects owned by a user or an organization, with outside collaborators. A
person's role on a project is the highest of: admin (owner of the project or
of its organization), the organization's `base_role` (member), their
collaborator role. See `hive-dev-users.example.json`.

```bash
cp hive-dev-users.example.json repos/hive-dev-users.json   # then edit it
fontra --launch hive-dev repos
```

- `/` shows a sign-in page listing the users (development only: no password,
  a plain cookie). *Log out*: `/hive/logout`.
- The project list only shows the projects you have a role on; opening
  another one is refused.
- Roles: observer (read), reviewer (+ comment), designer (+ edit, restore,
  branch, snapshot), manager (+ merge, tag, export, invite), admin (+ rename,
  trash, delete). They apply **per connection** in the one shared
  `FontHandler` of a project, so live collaboration keeps working: an
  observer gets Fontra's read-only mode (lock icon) and any edit they send
  is refused before it is written or broadcast; the Hive routes (history,
  Restore, snapshots) check the same capabilities.
- Every edit is committed under the name of the person who made it, with a
  pseudonymous address (`<username>@hive`), never their real email: open the
  same project in two browsers as two users and the glyph history shows both.
- The file is re-read when it changes: roles can be edited while the server
  runs. Without the file, nothing changes (no sign-in, one author, everyone
  may edit).

In Fontra's views (editor, font overview, font info, settings, project
list), which Hive serves with its own script added (Fontra is not modified):

- **your chip** at the right of the top bar, with a menu: name, username,
  email and role, *My projects*, *Share…*, *Sign out*;
- **who else is on the project**: avatars next to the project name, and
  where each person is ("editing “A”", "in the font overview", branch)
  in the tooltip and in the menu. Each open view sends a heartbeat every
  5 s; someone disappears 20 s after their last one;
- a **Read only · observer** badge when your role cannot edit (next to
  Fontra's own lock icon);
- **the branch pill** (`⎇ main ▾`) next to the project name, tinted amber
  off the default branch: the project's branches, each with how it compares
  with the default branch ("2 ahead · 1 behind main"), its latest change and
  who is on it; a click opens the same view on that branch (only the
  `project` parameter of the URL changes, so the editor keeps its glyph and
  text). *New branch…* (designers and up) starts from the current branch,
  the default one or a snapshot, and includes edits not yet committed. The
  × of a branch deletes it (managers and admins, or whoever made it; never
  the default branch, nor a branch someone has open): a branch whose
  changes are not all in the default branch is kept as an `archive/<name>`
  tag, listed under *Deleted branches* (folded) with who deleted it and
  when; *Restore* brings it back, under its name or another one (managers
  and admins, whoever deleted it, or whoever made it). The project page of
  the home page lists the branches too (open, delete, new branch, deleted
  branches, *Merge…*) and downloads from any of them;
- **merging**: off the default branch, the pill's menu has *Merge into
  main…* (managers and admins) and *Update from main…* (anyone who can
  edit). The dialog says what the merge brings (glyphs changed on each
  side, those changed on both but in different sources, merged
  automatically) and lists the conflicts, each glyph drawn as it is on both
  sides, with a choice for each: keep one side or take the other. See
  "Merging" below;
- **File › Share…**: the members of the project and where their role comes
  from (owner, organization, collaborator). Managers and admins can add
  people, change or remove collaborators; in development this writes
  `hive-dev-users.json` (invitations by email come with hive-api). A project
  always keeps at least one admin;
- the **Glyph history** plug-in registers itself: no more *Application
  settings → Plugins* by hand;
- opening a view without being signed in goes to the sign-in page, then
  back to the page you asked for.

Routes: `GET /api/hive/me` (who is signed in), `GET
/api/hive/projects/<name>/access` (your role and capabilities), `POST
/api/hive/projects/<name>/presence` (heartbeat, answers with the others),
`GET|POST /api/hive/projects/<name>/members`.

## With hive-api (real accounts)

`fontra hive` is the project manager of the hosted service: people, roles
and invitations come from hive-api, a separate accounts service of Fontra
Hive (proprietary, not part of this repository), the fonts from git as above.

```bash
# terminal 1 — hive-api, told the address people use:
HIVE_DEBUG=1 HIVE_PUBLIC_URL=http://localhost:8000 python manage.py runserver 8001

# terminal 2 — Fontra with Hive, relaying /api/* to hive-api:
fontra --launch hive repos --api http://127.0.0.1:8001
```

- Projects are `owner/name` (a person's or an organization's), as in
  hive-api, and `owner/name@branch` for a branch. A project's repository is
  `repos/<uid>.git`, after hive-api's stable project id: renaming a project
  moves nothing. The first time a project is opened, its repository is made
  with an empty font. To start from an existing font instead:
  `fontra-hive init repos/$(fontra-hive repo-of owner/name) MyFont.fontra`.
- **Hive's home page** (`/` once signed in) replaces Fontra's project list:
  your projects (grouped by owner, open in one click, trash and restore),
  **New project** (yours or an organization's you own; empty, or from a
  font: a `.zip` of a `.designspace` with its UFOs, `.ufo`, `.fontra` or
  `.glyphspackage`, or a single `.ttf`/`.otf`/`.woff2`/`.glyphs`… file, any
  format Fontra reads, converted to `.fontra`), project settings (rename,
  description, people and invitations, import a font again as a new
  version, trash), organizations (create, members and roles, base role,
  invitations) and your profile (name, photo, password, where you are signed
  in). Pending invitations show as a banner, accepted in one click.
  Importing is `POST /api/hive/projects/<owner%2Fname>/import` (multipart
  `file`, admins; up to 300 MB). A failed import creates nothing: the
  project then has no repository (`GET …/repository` says so without
  creating one), and its page offers to import again or start with an empty
  font instead of opening on an empty font.
- Signing in: `/` is the sign-in page (username or email, password);
  `/invitation#<token>` accepts an invitation and creates the account (the
  only way to sign up); `/forgot-password` and `/reset-password#<token>` do
  what they say; `/hive/logout` signs out. hive-api sets two HttpOnly
  cookies; this server checks the short one (`hive_access`, 15 min) with
  hive-api's public key and asks hive-api for the role, kept 10 s. The views
  renew the cookie every 10 minutes while open.
- **File › Share…** invites by username or email (an email with a link),
  changes roles up to your own, cancels invitations, removes people.
- This server relays `/api/*` (except its own `/api/hive/*` and hive-api's
  `/api/internal/*`) to hive-api: one address for the browser, and cookies
  that just work. In production a reverse proxy can do it instead
  (`--no-proxy`). `--service-key` (or `$HIVE_SERVICE_KEY`) must match
  hive-api's; `--issuer` (or `$HIVE_PUBLIC_URL`) checks the tokens' issuer.
- If hive-api does not answer, the Hive routes say 503 and new connections
  are refused; open ones keep working with the roles last known.

For a first try: create a staff account in hive-api (`python manage.py
createsuperuser`), then an organization, its members and a project in the
admin (`http://localhost:8000/api/admin/`), or invite people with `python
manage.py invite someone@example.com --project owner/name --role designer`
(the link is printed; emails go to hive-api's console in development).

## Glyph history in the editor (plug-in)

The package also ships an editor plug-in: a "Glyph history" panel in the right
sidebar listing every commit that touched the selected glyph (with no glyph
selected, it becomes "Font history": the changes of the whole font), one compact
line each (author, message, date; the full message, sha and time in the
tooltip), refreshed as you edit. It uses Fontra's editor plug-in mechanism
and is served by the Fontra server itself under `/hive/plugin/`.

- **Preview a version on the canvas.** Hover a commit: that version of the
  glyph is drawn in a soft amber over the glyph itself, in glyph coordinates, so
  the two outlines can be compared point by point; moving off the row
  removes it. Click to keep a version on the canvas ("pinned", drawn
  stronger; hovering other rows still shows them, paler, and leaving them
  comes back to the pinned one). A version not yet loaded is fetched after
  120 ms of hover, so sweeping over the list costs nothing; loaded versions
  (the eight most recent ones are fetched in the background when the list
  is shown) appear at once. The layer shown is the
  one being edited when the old version has it, otherwise its default
  source layer (the preview bar under the list says which; that bar has a
  fixed place and height, so hovering never moves the rows); components are resolved
  against the same version. Other sources shown with the glyph (several
  sources edited at once, or background sources) get the old version's
  outline too, when that version has them. The overlay is a visualization
  layer added at runtime, below the editing nodes.
- **Restore this version.** With a version pinned, "Restore" in the
  preview bar (click twice: the button asks to confirm) makes the server commit that
  glyph file again on top of the branch. Nothing is rewritten or deleted:
  the restore is a new commit, marked `Hive-Restore: <sha>`, attributed to
  the user, and every editor connected to the project reloads the glyph
  through the same path as any external change. The restore is not part
  of the editor's undo stack; restoring the previous version undoes it.
- **Which sources changed.** In a glyph's history, each version, snapshot
  and landmark says which of the glyph's sources it changed ("Edit H ·
  LightWide"), found by comparing the glyph file with its previous version
  (cached per pair of versions).
- **Glyph snapshots.** In a glyph's history, "Snapshot…" names the current
  version of that glyph only ("Approved by the art director"): its versions
  made since its previous glyph snapshot are grouped under that name, and
  the font's snapshots show as landmarks (◆) between them. It is an
  annotated tag `glyph-snapshot/<name>` on the branch head, whose message
  lists the glyph: no commit is added, the font's history is untouched.
- **Snapshots.** "Snapshot…", in the font history (with no glyph selected),
  names the current state of the whole font
  ("Proofs sent to client") and groups under that name every commit made
  since the previous snapshot. The list then shows the changes since the
  last snapshot, followed by one collapsible row per snapshot (how many
  versions of the glyph it groups, how many changes in the project); a
  snapshot row can be hovered, pinned and restored like a commit. Nothing
  is rewritten or squashed: a snapshot is an empty commit (same tree as its
  parent) whose message sums up the grouped commits — count, authors,
  glyphs — with `Hive-Snapshot*` trailers, plus an annotated tag
  `snapshot/<name>` on it. Clones, `git pull`, earlier `Hive-Restore`
  references and per-commit authorship are unaffected, and the history of
  every edit stays available. (Compacting old history into one commit per
  snapshot, on a separate branch or by rewriting, is left for later.)

The panel registers itself when the editor is served by Hive (it appears as
a clock icon in the right sidebar). Otherwise, once per browser: *Application
settings → Plugins*, add the address `/hive/plugin`, then reload the editor.

Why manual registration: Fontra's plug-in list is per browser (local
storage), with no way for a server to declare plug-ins yet. That is one of
the extension points to propose upstream; until then, this one step is
needed.

Routes used by the panel (also handy from the command line):

```
GET  /api/hive/projects/<name>/head?branch=main
GET  /api/hive/projects/<name>/log?branch=main&glyph=A&limit=50
GET  /api/hive/projects/<name>/glyph?glyph=A&ref=<sha|branch|tag>
GET    /api/hive/projects/<name>/branches
POST   /api/hive/projects/<name>/branches?name=bold&from=<branch|snapshot/<name>|sha>
DELETE /api/hive/projects/<name>/branches?branch=bold
POST   /api/hive/projects/<name>/branches/restore?tag=archive/bold[&name=bold-again]
GET    /api/hive/projects/<name>/merge-preview?from=bold[&into=main]
POST   /api/hive/projects/<name>/merge?from=bold[&into=main][&fromHead=…&intoHead=…]
       {"resolutions": {"glyphs/A^1.json": "theirs", "font-data.json": "ours"}}
POST /api/hive/projects/<name>/restore?branch=main&glyph=A&ref=<sha|tag>
GET  /api/hive/projects/<name>/snapshots?branch=main
POST /api/hive/projects/<name>/snapshot?branch=main&name=Proofs%20sent
```

`log` returns, for each commit, the snapshot it belongs to (`snapshot`: the
name, or `null` for changes since the last snapshot) and the snapshots met
while walking back (`snapshots`), so the panel groups without a second
request. `snapshots` lists the branch's snapshots, newest first, with the
number of commits a new snapshot would group (`pending`). `snapshot` answers
409 when nothing changed since the last snapshot or the name is taken, 403
on a read-only server; with the project open, pending edits are committed
first.

`branches` lists the branches, the default one first (`default` names it:
hive-api's `defaultBranch`, `main` in development), each with its head, its
latest change, `ahead`/`behind` counts against the default branch (cached
per pair of commits), `merged` (nothing the default branch lacks), `open`
(someone has it in the editor), and who made it, when and from what; plus
what the requester may do (`can`: create, delete any, merge), and the
deleted branches (`archived`: tag, name, who deleted it and when, how many
of its commits the default branch lacks). An archive tag's message carries
the branch's name, who deleted it and what was known about it
(`Hive-Archived-Branch`, `Hive-Deleted-By`, `Hive-Branch-Info`); restoring
recreates the branch with that information and removes the tag. That
information is kept in git but outside the font: a commit chain on
`refs/hive/meta` (`branches.json`), which backups carry (bundles of all
refs) and a plain `git clone` does not fetch. Branch names follow git's
rules within hive-api's characters (letters, digits, `.`, `_`, `-`, `/`),
and cannot start with `snapshot/`, `glyph-snapshot/` or `archive/`.

### Merging

`fontra_hive/merge.py` merges two versions of a `.fontra` package against
the commit where they parted (three-way), file by file: a file changed on
one side only is taken from that side; a file changed on both is merged by
its content: a glyph layer by layer and source by source (keyed by layer
name), `glyph-info.csv` row by row, `kerning.csv` pair by pair and group by
group, `font-data.json` key by key through nested objects; other files
whole. So two designers working on two masters of the same glyph merge
without a question. A conflict (the same layer, source, row, pair or key
changed differently, or a file deleted on one side and changed on the
other) is settled per file by a side, which wins on the parts in conflict
while what merged cleanly stays merged.

`merge-preview` answers what a merge would do (`changes.from`,
`changes.into`, `merged`, `conflicts`, `ahead`/`behind`, `upToDate`,
`fastForward`, `canMerge`). `merge` refuses (409) a merge with conflicts
left unresolved (the list, as JSON), a merge whose branches moved since
the preview (`fromHead`, `intoHead`), and one with nothing to merge. Into
the default branch it always makes a merge commit (two parents, message
`Merge bold into main`, trailers `Hive-Merge`, `Hive-Merge-Base`,
`Hive-Merge-Head` and `Hive-Glyphs` for the glyphs it changed, so that a
glyph's history on main shows the merge as one version) and records
`mergedInto`/`mergedAt`/`mergedBy` in the branch's information; into
another branch (*Update from main*), a fast-forward when that branch has
nothing of its own. Capability `merge` (managers, admins) into the default
branch, `edit` into another. Pending edits of both branches are committed
first; an open target picks the merge up as an external change.

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

## Comments on glyphs

Reviewers leave notes where they belong: on a point of a glyph, on one of
its sources. Each comment is a numbered topic (#12) of the project with a
thread of replies, open or resolved.

- **The Comment tool** (a speech bubble in the toolbar): click in the glyph
  being edited to pin a comment there, on the source being edited (at an
  interpolated position there is no source: go to one first). Clicking over
  another glyph of the line selects it, like the pointer tool.
- **Pins on the canvas**, whatever the tool: a numbered pin at each open
  topic of the glyphs shown, solid on the source it was written on, pale on
  the glyph's other sources. A click opens the post-it (the thread, a reply
  box — Enter sends, Shift+Enter makes a new line —, Resolve or Reopen,
  edit and delete); dragging a pin moves it. The pins are a visualization
  layer, "Hive: comments", that the View menu can switch off.
- **The "Comments" panel** (right sidebar): the selected glyph's topics,
  open then resolved, or the whole project's when no glyph is selected; a
  click goes to the glyph and its source and opens the post-it. "Resolved"
  shows resolved topics on the canvas too.
- **Who may do what.** Reviewers and up (`comment`): open topics, reply,
  edit their own messages, resolve, reopen and move their own topics.
  Designers and up (`edit`): resolve, reopen and move any topic. Managers and
  up (`moderate`): delete any topic or reply. Anyone may delete their own
  reply, and their own topic while nobody else has written in it.
- **Storage.** In the project's repository, outside the font's history: a
  commit chain on `refs/hive/comments` (one JSON file per topic,
  `issues/0012.json`, and a counter so numbers are never reused), one commit
  per change authored by the person who made it, compare-and-swap on the
  reference. Backups (bundles of all references) keep it; a plain
  `git clone` does not fetch it (`git fetch origin refs/hive/comments` does).
  A topic records the branch and the commit it was written on, and the
  commit it was resolved on.

```
GET    /api/hive/projects/<name>/comments[?glyph=H]      topics, who you are, what you may do
GET    /api/hive/projects/<name>/comments/head           changes with every comment (polled every 3 s)
POST   /api/hive/projects/<name>/comments                {glyph, source: {layer, name, location}, point: {x, y}, text[, branch]}
POST   /api/hive/projects/<name>/comments/<n>/messages   {text}
PATCH  /api/hive/projects/<name>/comments/<n>            {state: "open"|"resolved"} or {point: {x, y}}
PATCH  /api/hive/projects/<name>/comments/<n>/messages/<id>   {text}
DELETE /api/hive/projects/<name>/comments/<n>[/messages/<id>]
```

## Tests

```bash
pytest
```

The tests import a `.fontra` package into a bare repository, compare the git
backend with Fontra's file-system backend, apply the same edits to both and
check the exported files are identical byte for byte, then exercise scheduled
commits, attribution, external changes, concurrent commits, read-only mode,
the HTTP routes (history, glyph at a ref, restore, snapshots, including
restore and snapshot with the project open in a running backend), and that
the repository is readable by `git` itself.

`tests/test_hive_integration.py` runs `fontra hive`'s project manager
against a real hive-api (a Django development server started by the test,
from `$HIVE_API_DIR` or a `hive-api` checkout next to this one): sign-in
through the relay, roles, repository creation, the shared handler per role,
cross-site refusal, sign-out, sign-up by invitation. Skipped without it.

`tests/test_account_js.py` runs the sign-in and invitation pages and the
hive-api Share dialog in a headless Chromium, like the next one.

`tests/test_plugin_js.py` runs the editor plug-in in a headless Chromium
against a fake editor and a mocked server (hover, pin, restore, snapshot
groups and form); `tests/test_comments_js.py` does the same for comments
(the tool, pins, post-its, the panel). It needs Playwright (`pip install playwright && playwright
install chromium`) and is skipped otherwise.

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

The icons of the history and comments panels (`history`, `message-circle`)
and the delete button of a comment (`trash`) are from [Tabler Icons](https://tabler.io/icons)
(MIT License, see `src/fontra_hive/client/plugin/TABLER-ICONS-LICENSE.txt`),
the icon set Fontra uses for its own panels.
