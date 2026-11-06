"""Recipe actions that read and write Hive projects: ``hive-read`` and
``hive-write``.

They are ``fontra-workflow`` actions like any other (registered through the
``fontra.workflow.actions`` entry point), but they need to know who runs the
recipe and where the projects are. A runner provides that with
:func:`hiveRecipeContext`, after checking the recipe's access::

    context = HiveRecipeContext(
        resolver=projectResolverFor(projectManager, token), recipe="build-latin"
    )
    await requireRecipeAccess(config, context)  # every cited project, first
    with hiveRecipeContext(context):
        async with Workflow(config=config).endPoints() as endPoints:
            for output in endPoints.outputs:
                await output.process(workDir)
    context.reads, context.writes  # repository uid and commit of each

* ``project`` names a Hive project with a :class:`HiveProjectRef`:
  ``owner/name``, ``owner/name@ref``, or the uid of its repository
  (``<uid>`` or ``<uid>@ref``, what survives a rename); ``name`` alone on a
  development server. Its JSON schema has ``"format": "hive-project"``, so
  that tools tell it from any string.
* ``hive-read`` (input): ``project`` and ``ref`` (a branch, tag or full
  commit id; the default branch when not given). It resolves the ref to a
  commit, exposes it (``commit``, ``repository``) and yields a read-only
  :class:`GitFontraBackend` on it. The person must be able to read the
  project. Several ``hive-read`` steps on several projects are the normal
  case; each project is checked on its own.
* ``hive-write`` (output): ``project``, ``branch`` (``workflow/<recipe>`` by
  default), ``message``. It commits the whole font to that branch,
  attributed to the person, with a ``Hive-Recipe: <recipe>`` trailer. It
  never writes to the project's default branch: the result is merged from
  the branches panel. It knows nothing of remote repositories: the branch is
  a local branch like any other, which the existing synchronisation takes
  up once merged. The person needs ``edit``, and ``branch`` when the branch
  does not exist yet.

Who may do what is decided where the Hive routes decide it: the project
manager's directory in development, hive-api in production
(``/api/internal/access``; a repository uid is looked up among the person's
projects, ``/api/internal/projects``). :func:`checkRecipeAccess` asks it for
every project a recipe cites, and checks every ref, before anything is read;
the actions refuse to run in a context that was not checked, and ask again
when they run.

:func:`hivePolicy` is what a recipe run on a Hive server may not do: no
``fontra-read`` nor ``fontra-write`` (no files of the server), no absolute
path nor ``..``, every placeholder declared, and no literal project in a
recipe marked shareable. :func:`validateHiveRecipe` applies it (it needs the
``fontra-workflow-ui`` package).
"""

from __future__ import annotations

import contextvars
import logging
import pathlib
import re
import tempfile
import time
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

HIVE_RECIPE_TRAILER = "Hive-Recipe"
RECIPE_BRANCH_PREFIX = "workflow/"
# Commits touching more glyphs than this carry no Hive-Glyphs trailer (the
# history then finds the glyphs by comparing trees).
MAX_GLYPHS_IN_TRAILER = 200

HIVE_DENIED_ACTIONS = frozenset({"fontra-read", "fontra-write"})
HIVE_PROJECT_FORMAT = "hive-project"


# --- naming a project -----------------------------------------------------------

_UID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_NAME_PART = re.compile(r"^[^/@\s]+$")


class HiveProjectRef(str):
    """A Hive project, as a recipe names it: ``owner/name``, the uid of its
    repository, or ``name`` alone (development server), each optionally
    followed by ``@ref``. The string is kept as written; the parts are
    attributes."""

    __json_schema__ = {
        "type": "string",
        "format": HIVE_PROJECT_FORMAT,
        "pattern": r"^[^/@\s]+(/[^/@\s]+)?(@\S+)?$",
    }

    kind: str  # "path" (owner/name), "uid" or "name"
    project: str  # the part before "@"
    ref: str | None

    def __new__(cls, value):
        if isinstance(value, HiveProjectRef):
            return value
        if not isinstance(value, str):
            raise ActionError(f"a project is a string, got {value!r}")
        self = super().__new__(cls, value)
        base, at, ref = value.strip().partition("@")
        if at and not ref:
            raise ActionError(f"no ref after '@' in {value!r}")
        parts = base.split("/")
        if not all(_NAME_PART.match(part) for part in parts) or len(parts) > 2:
            raise ActionError(
                f"not a project: {value!r} (owner/name, a repository uid, "
                "or name on a development server)"
            )
        if len(parts) == 2:
            self.kind = "path"
        elif _UID.match(base):
            self.kind = "uid"
            base = base.lower()
        else:
            self.kind = "name"
        self.project = base
        self.ref = ref or None
        return self

    def withRef(self, ref: str | None) -> str | None:
        """The ref to read: ``ref`` (the step's argument) or the one after
        ``@``; both, only if they agree."""
        if ref is not None and self.ref is not None and ref != self.ref:
            raise ActionError(f"two refs for {self.project}: {self.ref!r} and {ref!r}")
        return ref if ref is not None else self.ref


# --- who runs the recipe, and where the projects are ---------------------------


@dataclass(frozen=True)
class HiveProject:
    repository: str  # the repository's uid ("<uid>" of "<uid>.git"; the name in dev)
    path: str  # "owner/name" (Hive) or "name" (development), for messages
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
    async def resolve(self, ref: HiveProjectRef) -> HiveProject | None:
        """The project and the person's access to it; None when it does not
        exist or the person cannot read it (both look the same)."""


def _repositoryUid(repoPath: pathlib.Path) -> str:
    name = repoPath.name
    return name[: -len(".git")] if name.endswith(".git") else name


class DevProjectResolver:
    """``fontra hive-dev``: the manager's repositories (``Name.git``, whose
    uid is the name) and its directory of users, as for its routes."""

    def __init__(self, manager, token: str | None):
        self.manager = manager
        self.token = token

    async def resolve(self, ref: HiveProjectRef) -> HiveProject | None:
        if ref.kind == "path":
            return None  # no owners in development
        name = ref.project
        repoPath = self.manager._repoPath(name)
        if repoPath is None:
            return None
        access = None
        if self.manager.directory is not None:
            access = self.manager.accessFor(self.token, name)
            if access is None or not access.can("read"):
                return None
        return HiveProject(
            _repositoryUid(repoPath), name, repoPath, DEFAULT_BRANCH, access
        )


class HiveApiProjectResolver:
    """``fontra hive``: roles and repositories from hive-api, fresh
    (``/api/internal/access``), as for the Hive routes."""

    def __init__(self, manager, token: str | None):
        self.manager = manager
        self.token = token

    async def resolve(self, ref: HiveProjectRef) -> HiveProject | None:
        api = self.manager.api
        try:
            path = ref.project if ref.kind == "path" else None
            if ref.kind == "uid":
                # Only the person's own projects can be found by uid.
                for entry in await api.projects(self.token):
                    if str(entry.get("uid", "")).lower() == ref.project:
                        path = entry.get("id")
                        break
            if not path:
                return None
            projectAccess = await api.access(self.token, path)
        except ActionError:
            raise
        except Exception as error:  # HiveApiUnavailable, or the network
            raise ActionError(
                f"cannot check access to {ref.project}: {error}"
            ) from error
        if projectAccess is None or not projectAccess.access.can("read"):
            return None
        repoPath = self.manager._ensureRepo(projectAccess)
        if ref.kind == "uid" and _repositoryUid(repoPath).lower() != ref.project:
            return None
        return HiveProject(
            _repositoryUid(repoPath),
            projectAccess.projectId,
            repoPath,
            projectAccess.defaultBranch,
            projectAccess.access,
        )


def projectResolverFor(manager, token: str | None) -> ProjectResolver:
    if getattr(manager, "api", None) is not None:
        return HiveApiProjectResolver(manager, token)
    return DevProjectResolver(manager, token)


@dataclass(frozen=True)
class ReadRecord:
    repository: str  # the repository's uid: what identifies the input
    commit: str  # the commit read
    ref: str  # as the recipe gave it (or the default branch)
    project: str  # owner/name at the time, for people


@dataclass(frozen=True)
class WriteRecord:
    repository: str
    branch: str
    commit: str | None  # None: nothing changed, no commit
    created: bool  # the branch was created
    project: str


@dataclass
class HiveRecipeContext:
    resolver: ProjectResolver
    recipe: str  # the recipe's name: default branch, trailer, messages
    fallbackAuthor: Signature = SERVER_SIGNATURE
    # Set by checkRecipeAccess() when every cited project and ref is fine.
    checked: bool = False
    # Provenance: what this run read and wrote, in order.
    reads: list[ReadRecord] = field(default_factory=list)
    writes: list[WriteRecord] = field(default_factory=list)


_context: contextvars.ContextVar[HiveRecipeContext | None] = contextvars.ContextVar(
    "fontra_hive_recipe_context", default=None
)


@contextmanager
def hiveRecipeContext(context: HiveRecipeContext) -> Iterator[HiveRecipeContext]:
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


def currentContext(actionName: str) -> HiveRecipeContext:
    context = _context.get()
    if context is None:
        raise ActionError(
            f"{actionName}: only available in recipes run by a Fontra Hive server"
        )
    if not context.checked:
        raise ActionError(
            f"{actionName}: the recipe's access was not checked before running"
        )
    return context


async def _resolve(context, actionName, ref: HiveProjectRef, capability) -> HiveProject:
    hiveProject = await context.resolver.resolve(ref)
    if hiveProject is None:
        raise ActionError(f"{actionName}: no project {ref.project!r}")
    if not hiveProject.can(capability):
        raise ActionError(
            f"{actionName}: {hiveProject.role} cannot {capability} {ref.project!r}"
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


# --- hive-read -------------------------------------------------------------------


@registerInputAction("hive-read")
@dataclass(kw_only=True)
class HiveRead:
    """Read a Hive project, as it is at a branch, a tag or a commit.

    Arguments:
        project: The project: owner/name, or the uid of its repository (name
          alone on a development server); "@ref" after it is short for ref.
        ref: A branch, a tag or a full commit id; the project's default
          branch when not given.
    """

    project: HiveProjectRef
    ref: str | None = None
    # Once read: the repository's uid and the commit the ref resolved to.
    repository: str | None = field(init=False, default=None)
    commit: str | None = field(init=False, default=None)

    def __post_init__(self):
        self.project = HiveProjectRef(self.project)
        self.project.withRef(self.ref)  # refuse two different refs now

    async def resolveCommit(
        self, context: HiveRecipeContext
    ) -> tuple[HiveProject, str, str]:
        """(project, ref, commit), with the person's read access checked."""
        hiveProject = await _resolve(context, "hive-read", self.project, "read")
        ref = self.project.withRef(self.ref) or hiveProject.defaultBranch
        store = GitRepoStore.open(hiveProject.repoPath)
        try:
            return hiveProject, ref, _resolveCommit(store, ref, hiveProject.path)
        finally:
            store.close()

    @asynccontextmanager
    async def prepare(self) -> AsyncGenerator[ReadableFontBackend, None]:
        context = currentContext("hive-read")
        hiveProject, ref, sha = await self.resolveCommit(context)
        self.repository, self.commit = hiveProject.repository, sha
        store = GitRepoStore.open(hiveProject.repoPath)
        try:
            backend = GitFontraBackend(store, hiveProject.defaultBranch, commit=sha)
            context.reads.append(
                ReadRecord(hiveProject.repository, sha, ref, hiveProject.path)
            )
            try:
                yield backend
            finally:
                await backend.aclose()
        finally:
            store.close()


# --- hive-write ------------------------------------------------------------------


@registerOutputAction("hive-write")
@dataclass(kw_only=True)
class HiveWrite:
    """Commit the font to a branch of a Hive project, never its default
    branch: the result is merged from the branches panel.

    Arguments:
        project: The project: owner/name, or the uid of its repository (name
          alone on a development server).
        branch: The branch to write to, created from the default branch when
          it does not exist; workflow/<recipe name> when not given.
        message: The commit message; "Recipe <recipe name>" when not given.
    """

    project: HiveProjectRef
    branch: str | None = None
    message: str | None = None
    input: ReadableFontBackend = field(init=False, default=NullBackend())
    # Once written: the repository's uid and the new commit (None: no change).
    repository: str | None = field(init=False, default=None)
    commit: str | None = field(init=False, default=None)

    def __post_init__(self):
        self.project = HiveProjectRef(self.project)
        if self.project.ref is not None:
            raise ActionError("hive-write: give the branch with 'branch', not '@'")

    def branchFor(self, recipe: str) -> str:
        return self.branch or RECIPE_BRANCH_PREFIX + recipe

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
        hiveProject = await _resolve(context, "hive-write", self.project, "edit")
        branch = self.branchFor(context.recipe)
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
                        f"in {hiveProject.path!r}"
                    )
                base = store.head(hiveProject.defaultBranch)
                if base is None:
                    raise ActionError(f"hive-write: {hiveProject.path!r} is empty")
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
                        "created": int(time.time()),
                        "from": hiveProject.defaultBranch,
                        "base": base,
                        "recipe": context.recipe,
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
            self.repository, self.commit = hiveProject.repository, sha
            context.writes.append(
                WriteRecord(
                    hiveProject.repository, branch, sha, created, hiveProject.path
                )
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

        title = (self.message or f"Recipe {recipe}").strip()
        trailers = [f"{HIVE_RECIPE_TRAILER}: {recipe}"]
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


# --- before running --------------------------------------------------------------


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


def _arguments(step: Mapping, action: str) -> dict:
    return {
        k: v for k, v in step.items() if k not in ("input", "output", "steps", action)
    }


async def checkRecipeAccess(config: Mapping, context: HiveRecipeContext) -> list[str]:
    """Problems with the projects a recipe cites, for the person running it,
    found before anything is read: every ``hive-read`` project must be
    readable and its ref must exist; every ``hive-write`` project needs
    ``edit`` (and ``branch`` for a new branch) and must not be written on its
    default branch. ``config`` is what ``Workflow`` gets (substitutions
    applied). Each project is checked on its own. When there is no problem,
    the context is marked checked and the actions may run."""
    problems = []
    context.checked = False
    for path, action, step in _hiveSteps(config.get("steps")):
        try:
            if action == "hive-read":
                await HiveRead(**_arguments(step, action)).resolveCommit(context)
                continue
            write = HiveWrite(**_arguments(step, action))
            hiveProject = await _resolve(context, action, write.project, "edit")
            branch = write.branchFor(context.recipe)
            if branch == hiveProject.defaultBranch:
                raise ActionError("never writes to the default branch")
            store = GitRepoStore.open(hiveProject.repoPath)
            try:
                exists = store.head(branch) is not None
            finally:
                store.close()
            if not exists and not hiveProject.can("branch"):
                raise ActionError(f"{hiveProject.role} cannot create branches")
        except (ActionError, TypeError) as error:
            message = str(error)
            prefix = f"{action}: "
            if message.startswith(prefix):
                message = message[len(prefix) :]
            problems.append(f"{path} ({action}): {message}")
    context.checked = not problems
    return problems


class RecipeAccessDenied(ActionError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


async def requireRecipeAccess(config: Mapping, context: HiveRecipeContext) -> None:
    problems = await checkRecipeAccess(config, context)
    if problems:
        raise RecipeAccessDenied(problems)


# --- validation (fontra-workflow-ui) -----------------------------------------------


def hivePolicy():
    """The :class:`fontra_workflow_ui.validate.RecipePolicy` of Hive."""
    from fontra_workflow_ui.validate import RecipePolicy

    return RecipePolicy(
        deniedActions=HIVE_DENIED_ACTIONS,
        rejectUnsafePaths=True,
        requireDeclaredPlaceholders=True,
        shareableForbiddenArguments=frozenset({"project"}),
        shareableForbiddenFormats=frozenset({HIVE_PROJECT_FORMAT}),
        reason="recipes on Fontra Hive read and write projects with hive-read "
        "and hive-write, not files of the server",
    )


def validateHiveRecipe(config, *, catalog=None, substitutions=None):
    """Check a recipe as a Hive server will run it."""
    from fontra_workflow_ui.validate import validateRecipe

    return validateRecipe(
        config, catalog=catalog, policy=hivePolicy(), substitutions=substitutions
    )
