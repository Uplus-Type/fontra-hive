"""Workflow actions that read and write Hive projects: ``hive-read`` and
``hive-write``.

They are ``fontra-workflow`` actions like any other (registered through the
``fontra.workflow.actions`` entry point), but they need to know who runs the
recipe and where the projects are. A runner provides that with
:func:`hiveWorkflowContext`::

    context = HiveWorkflowContext(
        resolver=projectResolverFor(projectManager, token), recipe="build-latin"
    )
    with hiveWorkflowContext(context):
        async with Workflow(config=engineConfig(recipe)).endPoints() as endPoints:
            for output in endPoints.outputs:
                await output.process(workDir)

* ``hive-read`` (input): ``project`` (``owner/name``; ``name`` on a
  development server), ``ref`` (a branch, tag or full commit id; the
  project's default branch when not given; ``project: owner/name@ref`` is
  short for both). It yields a read-only :class:`GitFontraBackend` on that
  commit. The person must be able to read the project.
* ``hive-write`` (output): ``project``, ``branch`` (``workflow/<recipe>`` by
  default), ``message``. It commits the whole font to that branch,
  attributed to the person, with a ``Hive-Workflow: <recipe>`` trailer. It
  never writes to the project's default branch: the result is merged from
  the branches panel. The person needs ``edit``, and ``branch`` when the
  branch does not exist yet.

Who may do what is decided where the Hive routes decide it: the project
manager's directory in development (``Name@branch`` identifiers), hive-api's
``/api/internal/access`` in production. :func:`checkRecipeAccess` asks it for
every project a recipe cites before anything runs; the actions ask again
when they run.

:data:`hivePolicy` is what a recipe run on a Hive server may not do: no
``fontra-read`` nor ``fontra-write`` (no files of the server), no absolute
path nor ``..``, every placeholder declared, and no literal ``project`` in a
recipe marked shareable. :func:`validateHiveRecipe` applies it (it needs the
``fontra-workflow-ui`` package).
"""

from __future__ import annotations

import contextvars
import logging
import pathlib
import tempfile
from collections.abc import Mapping
from contextlib import aclosing, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Iterator, Protocol

from fontra.backends.copy import copyFont
from fontra.backends.fontra import FontraBackend
from fontra.backends.null import NullBackend
from fontra.core.protocols import ReadableFontBackend
from fontra.workflow.actions import (
    ActionError,
    OutputProcessorProtocol,
    registerInputAction,
    registerOutputAction,
)

from .access import Access
from .backend_git import GitFontraBackend
from .gitstore import DEFAULT_BRANCH, SERVER_SIGNATURE, GitRepoStore, Signature

logger = logging.getLogger(__name__)

HIVE_WORKFLOW_TRAILER = "Hive-Workflow"
WORKFLOW_BRANCH_PREFIX = "workflow/"
# Commits touching more glyphs than this carry no Hive-Glyphs trailer (the
# history then finds the glyphs by comparing trees).
MAX_GLYPHS_IN_TRAILER = 200

HIVE_DENIED_ACTIONS = frozenset({"fontra-read", "fontra-write"})


# --- who runs the recipe, and where the projects are --------------------------


@dataclass(frozen=True)
class HiveProject:
    projectId: str  # "owner/name" (Hive) or "name" (development)
    repoPath: pathlib.Path
    defaultBranch: str
    access: Access | None  # None: a development server without accounts

    def can(self, capability: str) -> bool:
        return self.access is None or self.access.can(capability)

    @property
    def role(self) -> str:
        return self.access.role if self.access is not None else "admin"

    def author(self, fallback: Signature) -> Signature:
        return self.access.user.signature if self.access is not None else fallback


class ProjectResolver(Protocol):
    async def resolve(self, project: str) -> HiveProject | None:
        """The project and the person's access to it; None when it does not
        exist or the person cannot see it (both look the same)."""


class DevProjectResolver:
    """``fontra hive-dev``: the manager's repositories and its directory of
    users (``Name`` identifiers), as for its routes."""

    def __init__(self, manager, token: str | None):
        self.manager = manager
        self.token = token

    async def resolve(self, project: str) -> HiveProject | None:
        repoPath = self.manager._repoPath(project)
        if repoPath is None:
            return None
        access = None
        if self.manager.directory is not None:
            access = self.manager.accessFor(self.token, project)
            if access is None or not access.can("read"):
                return None
        return HiveProject(project, repoPath, DEFAULT_BRANCH, access)


class HiveApiProjectResolver:
    """``fontra hive``: roles and repositories from hive-api
    (``/api/internal/access``, fresh), as for the Hive routes."""

    def __init__(self, manager, token: str | None):
        self.manager = manager
        self.token = token

    async def resolve(self, project: str) -> HiveProject | None:
        if "/" not in project:
            return None
        try:
            projectAccess = await self.manager.api.access(self.token, project)
        except Exception as error:  # HiveApiUnavailable, or the network
            raise ActionError(f"cannot check access to {project}: {error}") from error
        if projectAccess is None or not projectAccess.access.can("read"):
            return None
        return HiveProject(
            projectAccess.projectId,
            self.manager._ensureRepo(projectAccess),
            projectAccess.defaultBranch,
            projectAccess.access,
        )


def projectResolverFor(manager, token: str | None) -> ProjectResolver:
    if getattr(manager, "api", None) is not None:
        return HiveApiProjectResolver(manager, token)
    return DevProjectResolver(manager, token)


@dataclass(frozen=True)
class ReadRecord:
    project: str
    ref: str
    commit: str


@dataclass(frozen=True)
class WriteRecord:
    project: str
    branch: str
    commit: str | None  # None: nothing changed, no commit
    created: bool  # the branch was created


@dataclass
class HiveWorkflowContext:
    resolver: ProjectResolver
    recipe: str  # the recipe's name: default branch, trailer, messages
    fallbackAuthor: Signature = SERVER_SIGNATURE
    # Provenance: what this run read and wrote, in order.
    reads: list[ReadRecord] = field(default_factory=list)
    writes: list[WriteRecord] = field(default_factory=list)


_context: contextvars.ContextVar[HiveWorkflowContext | None] = contextvars.ContextVar(
    "fontra_hive_workflow_context", default=None
)


@contextmanager
def hiveWorkflowContext(context: HiveWorkflowContext) -> Iterator[HiveWorkflowContext]:
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


def currentContext(actionName: str) -> HiveWorkflowContext:
    context = _context.get()
    if context is None:
        raise ActionError(
            f"{actionName}: only available in recipes run by a Fontra Hive server"
        )
    return context


def splitProjectRef(project: str, ref: str | None) -> tuple[str, str | None]:
    name, at, shortRef = str(project).partition("@")
    if at:
        if not shortRef:
            raise ActionError(f"no ref after '@' in {project!r}")
        if ref is not None and ref != shortRef:
            raise ActionError(f"two refs for {name}: {shortRef!r} and {ref!r}")
        ref = shortRef
    if not name:
        raise ActionError("no project given")
    return name, ref


async def _resolve(context, actionName, project, capability) -> HiveProject:
    hiveProject = await context.resolver.resolve(project)
    if hiveProject is None:
        raise ActionError(f"{actionName}: no project {project!r}")
    if not hiveProject.can(capability):
        raise ActionError(
            f"{actionName}: {hiveProject.role} cannot {capability} {project!r}"
        )
    return hiveProject


def _resolveCommit(store: GitRepoStore, ref: str, project: str) -> str:
    try:
        sha = store.resolve(ref)
        store.commit_info(sha)  # a full sha is not checked by resolve()
    except Exception:
        raise ActionError(
            f"hive-read: nothing called {ref!r} in {project!r} "
            "(a branch, a tag or a full commit id)"
        )
    return sha


# --- hive-read ----------------------------------------------------------------


@registerInputAction("hive-read")
@dataclass(kw_only=True)
class HiveRead:
    """Read a Hive project, as it is at a branch, a tag or a commit.

    Arguments:
        project: The project, as owner/name (name alone on a development
          server); owner/name@ref is short for project and ref.
        ref: A branch, a tag or a full commit id; the project's default
          branch when not given.
    """

    project: str
    ref: str | None = None

    @asynccontextmanager
    async def prepare(self) -> AsyncGenerator[ReadableFontBackend, None]:
        context = currentContext("hive-read")
        project, ref = splitProjectRef(self.project, self.ref)
        hiveProject = await _resolve(context, "hive-read", project, "read")
        store = GitRepoStore.open(hiveProject.repoPath)
        try:
            ref = ref or hiveProject.defaultBranch
            sha = _resolveCommit(store, ref, project)
            backend = GitFontraBackend(store, hiveProject.defaultBranch, commit=sha)
            context.reads.append(ReadRecord(hiveProject.projectId, ref, sha))
            try:
                yield backend
            finally:
                await backend.aclose()
        finally:
            store.close()


# --- hive-write ---------------------------------------------------------------


@registerOutputAction("hive-write")
@dataclass(kw_only=True)
class HiveWrite:
    """Commit the font to a branch of a Hive project, never its default
    branch: the result is merged from the branches panel.

    Arguments:
        project: The project, as owner/name (name alone on a development
          server).
        branch: The branch to write to, created from the default branch when
          it does not exist; workflow/<recipe name> when not given.
        message: The commit message; "Workflow <recipe name>" when not given.
    """

    project: str
    branch: str | None = None
    message: str | None = None
    input: ReadableFontBackend = field(init=False, default=NullBackend())

    @asynccontextmanager
    async def connect(
        self, input: ReadableFontBackend
    ) -> AsyncGenerator[OutputProcessorProtocol, None]:
        self.input = input
        try:
            yield self
        finally:
            self.input = NullBackend()
            await input.aclose()

    async def process(
        self, outputDir: Any = None, *, continueOnError: bool = False
    ) -> None:
        context = currentContext("hive-write")
        if "@" in str(self.project):
            raise ActionError("hive-write: give the branch with 'branch', not '@'")
        hiveProject = await _resolve(context, "hive-write", self.project, "edit")
        branch = self.branch or WORKFLOW_BRANCH_PREFIX + context.recipe
        if branch == hiveProject.defaultBranch:
            raise ActionError(
                f"hive-write: never writes to the default branch ({branch}); "
                "write to another branch and merge it from the branches panel"
            )

        files = await self._fontFiles(continueOnError)

        store = GitRepoStore.open(hiveProject.repoPath)
        try:
            head = store.head(branch)
            created = head is None
            if created:
                error = store.branch_name_error(branch)
                if error:
                    raise ActionError(f"hive-write: branch {branch!r}: {error}")
                if not hiveProject.can("branch"):
                    raise ActionError(
                        f"hive-write: {hiveProject.role} cannot create branches "
                        f"in {self.project!r}"
                    )
                base = store.head(hiveProject.defaultBranch)
                if base is None:
                    raise ActionError(f"hive-write: {self.project!r} is empty")
            else:
                base = head

            changes = _changes(store.list_tree(base), files)
            author = hiveProject.author(context.fallbackAuthor)
            if created:
                store.create_branch(branch, base)
                user = hiveProject.access.user if hiveProject.access else None
                store.set_branch_info(
                    branch,
                    {
                        "createdBy": user.username if user else None,
                        "createdByName": user.name if user else None,
                        "created": _now(),
                        "from": hiveProject.defaultBranch,
                        "base": base,
                        "workflow": context.recipe,
                    },
                    author=author,
                )
            sha = None
            if changes:
                sha = store.commit(
                    changes,
                    branch=branch,
                    message=self._commitMessage(context.recipe, changes),
                    author=author,
                    expected_head=base,
                )
            context.writes.append(
                WriteRecord(hiveProject.projectId, branch, sha, created)
            )
        finally:
            store.close()

    async def _fontFiles(self, continueOnError: bool) -> dict[str, bytes]:
        """The font in Fontra's format, as files: exactly what the Fontra
        backend writes, the format Hive stores."""
        with tempfile.TemporaryDirectory(prefix="hive-write-") as tmp:
            path = pathlib.Path(tmp) / "font.fontra"
            output = FontraBackend.createFromPath(path)
            async with aclosing(output):
                await copyFont(self.input, output, continueOnError=continueOnError)
            return {
                p.relative_to(path).as_posix(): p.read_bytes()
                for p in sorted(path.rglob("*"))
                if p.is_file()
            }

    def _commitMessage(self, recipe: str, changes: Mapping[str, Any]) -> str:
        from fontra.backends.filenames import fileNameToString

        title = (self.message or f"Workflow {recipe}").strip()
        trailers = [f"{HIVE_WORKFLOW_TRAILER}: {recipe}"]
        glyphs = sorted(
            fileNameToString(p[len("glyphs/") : -len(".json")])
            for p in changes
            if p.startswith("glyphs/") and p.endswith(".json")
        )
        if glyphs and len(glyphs) <= MAX_GLYPHS_IN_TRAILER:
            trailers.insert(0, f"Hive-Glyphs: {' '.join(glyphs)}")
        return title + "\n\n" + "\n".join(trailers) + "\n"


def _isFontFile(path: str) -> bool:
    """A file of the font package (what hive-write replaces); anything else
    in the repository (recipes, notes) is left as it is."""
    top = path.split("/", 1)[0]
    return top in (
        FontraBackend.glyphsDirName,
        FontraBackend.backgroundImagesDirName,
    ) or path in (
        FontraBackend.fontDataFileName,
        FontraBackend.glyphInfoFileName,
        FontraBackend.kerningFileName,
        FontraBackend.featureTextFileName,
    )


def _changes(
    current: Mapping[str, str], files: Mapping[str, bytes]
) -> dict[str, bytes | None]:
    """What to commit for the tree to hold ``files`` as the font: changed
    and new files, and font files that are no longer there."""
    from dulwich.objects import Blob

    changes: dict[str, bytes | None] = {}
    for path, data in files.items():
        if current.get(path) != Blob.from_string(data).id.decode():
            changes[path] = data
    for path in current:
        if path not in files and _isFontFile(path):
            changes[path] = None
    return changes


def _now() -> int:
    import time

    return int(time.time())


# --- before running -----------------------------------------------------------


def _hiveSteps(steps, path="steps"):
    """(path, action, step) of every hive-read / hive-write step."""
    if not isinstance(steps, list):
        return
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        stepPath = f"{path}[{index}]"
        if step.get("input") == "hive-read":
            yield stepPath, "hive-read", step
        elif step.get("output") == "hive-write":
            yield stepPath, "hive-write", step
        yield from _hiveSteps(step.get("steps"), f"{stepPath}.steps")


async def checkRecipeAccess(config: Mapping, context: HiveWorkflowContext) -> list[str]:
    """Problems with the projects a recipe cites, for the person running it:
    read access to every ``hive-read`` project, ``edit`` (and ``branch`` for a
    new branch) on every ``hive-write`` project, and no write to a default
    branch. ``config`` has its substitutions applied. Empty: it may run."""
    problems = []
    for path, action, step in _hiveSteps(config.get("steps")):
        try:
            project, ref = splitProjectRef(step.get("project", ""), step.get("ref"))
            capability = "read" if action == "hive-read" else "edit"
            if action == "hive-write" and ref is not None:
                raise ActionError("give the branch with 'branch', not '@'")
            hiveProject = await _resolve(context, action, project, capability)
            if action == "hive-write":
                branch = step.get("branch") or WORKFLOW_BRANCH_PREFIX + context.recipe
                if branch == hiveProject.defaultBranch:
                    raise ActionError("never writes to the default branch")
                store = GitRepoStore.open(hiveProject.repoPath)
                try:
                    exists = store.head(branch) is not None
                finally:
                    store.close()
                if not exists and not hiveProject.can("branch"):
                    raise ActionError(f"{hiveProject.role} cannot create branches")
        except ActionError as error:
            message = str(error)
            prefix = f"{action}: "
            if message.startswith(prefix):
                message = message[len(prefix) :]
            problems.append(f"{path} ({action}): {message}")
    return problems


# --- validation (fontra-workflow-ui) ------------------------------------------


def hivePolicy():
    """The :class:`fontra_workflow_ui.validate.RecipePolicy` of Hive."""
    from fontra_workflow_ui.validate import RecipePolicy

    return RecipePolicy(
        deniedActions=HIVE_DENIED_ACTIONS,
        rejectUnsafePaths=True,
        requireDeclaredPlaceholders=True,
        shareableForbiddenArguments=frozenset({"project"}),
        reason="recipes on Fontra Hive read and write projects with hive-read "
        "and hive-write, not files of the server",
    )


def validateHiveRecipe(config, *, catalog=None, substitutions=None):
    """Check a recipe as a Hive server will run it."""
    from fontra_workflow_ui.validate import validateRecipe

    return validateRecipe(
        config, catalog=catalog, policy=hivePolicy(), substitutions=substitutions
    )
