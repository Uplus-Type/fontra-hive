"""Running a recipe: one process per run, in a fresh folder, with limits.

:func:`runRecipe` is the server's side. It asks the registry whether the run
may take place (hive-api answers, and may lower the limits), starts
``python -m fontra_hive.runner`` without a shell and in a folder of its own,
follows what the process says, and closes the run with what was measured,
whatever happened. The folder is gone afterwards; the artifacts of a run that
succeeded are set aside in ``<runsRoot>/<run id>/``, those of a built-in recipe
in a folder of their own, gone once they are sent.

``<runsRoot>`` is out of the repositories (``HIVE_RUNS_DIR``) and out of the
backups: an artifact can be built again. The registry decides which runs lose
their files, here they are erased (:func:`sweepExpiredRuns`); nothing here says
how long files stay. When the server starts it clears what a stopped server
left (:func:`removeWorkFolders`, :func:`sweepInterruptedRuns`).

The process runs the recipe:

    read the recipe at its commit, copy its assets
    → validateHiveRecipe(), substitutions applied
    → requireRecipeAccess(): every cited project, before anything is read
    → Workflow(...) in hiveRecipeContext(), then each output

It holds no secret: its environment is a short list, and it asks the server
about projects through its standard input and output, so that hive-api's key
never reaches it and the same checks answer as for the Hive routes.

Protocol: one JSON object per line. The job is the first line on the
process's standard input. On its output, ``{"type": "progress", ...}`` (stage,
or step, action and state), ``{"type": "read"|"write"|"artifact", ...}``
(provenance, as it happens, so that a run that is killed keeps it) and
``{"type": "resolve", "id": n, "ref": ...}``, answered with one line on its
input; the last line is ``{"type": "result", ...}``. Whatever else a compiler
prints goes to the standard error, which is the run's log.

:class:`RunLimits` protect the machine and are the same for every run; the
registry may lower them for a run, and answers with numbers only. Memory is
bounded with ``RLIMIT_AS`` on Linux; elsewhere (macOS) the limit is only
indicative.

Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import errno
import json
import logging
import os
import pathlib
import re
import shutil
import signal
import socket
import stat
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .access import Access, HiveUser
from .export import check_feature_includes
from .hiveapi import HiveApiUnavailable
from .recipe import (
    HiveProject,
    HiveProjectRef,
    HiveRecipeContext,
    OutputLimitExceeded,
    ProjectResolver,
    RecipeAccessDenied,
    hiveRecipeContext,
    requireRecipeAccess,
    validateHiveRecipe,
)

logger = logging.getLogger(__name__)

RECIPE_FILE = "recipe.yaml"
ASSETS_FOLDER = "assets"
RUNS_DIR_VARIABLE = "HIVE_RUNS_DIR"
SERVER_ID_VARIABLE = "HIVE_SERVER_ID"
WORK_PREFIX = "work-"
SEND_PREFIX = "send-"  # a built-in recipe's files, on their way to a person
SWEEP_SECONDS = 15 * 60
DOWNLOAD_RECIPE = "download"

RUNS_AT_ONCE = 1  # builds running at the same time on the server
LOG_BYTES = 64 * 1024  # the end of a run's log is kept
MESSAGE_CHARACTERS = 500
PANIC = " panicked at "  # what Rust prints, in fontc's case
LINE_BYTES = 1 << 20
FILE_BYTES_FACTOR = 4  # one file of a run, as a multiple of its outputs

SUCCEEDED = "succeeded"
INVALID = "invalid"  # the recipe cannot run as written
DENIED = "denied"  # the person cannot read an input or change an output
FAILED = "failed"  # an action or a compiler stopped
TIMEOUT = "timeout"
TOO_LARGE = "too-large"
OUT_OF_MEMORY = "out-of-memory"
INTERRUPTED = "interrupted"  # the server stopped during the run
INTERRUPTED_MESSAGE = "The server stopped during the run."

# Why a run ended so, when its state does not say: the server reads only these.
REFUSED_FEATURES = "refused-features"  # feature code that includes other files
STOPPED = "stopped"  # the process ended without a result: killed, or crashed
REASONS = frozenset({REFUSED_FEATURES, STOPPED})
STATES = frozenset(
    {SUCCEEDED, INVALID, DENIED, FAILED, TIMEOUT, TOO_LARGE, OUT_OF_MEMORY}
)

# What a failed run says: one of these, never what a compiler or an action
# printed, which may quote any file it read. That stays in the log.
RUN_FAILED = "The run failed."
COMPILER_CRASHED = "The compiler crashed."
RUN_STOPPED = "The run stopped unexpectedly."
RUN_NOT_STARTED = "The run could not start."
RUN_INTERRUPTED = "The run was interrupted."
RUN_SAID_TOO_MUCH = "The run said something it should not."
FAILED_MESSAGES = frozenset(
    {
        RUN_FAILED,
        COMPILER_CRASHED,
        RUN_STOPPED,
        RUN_NOT_STARTED,
        RUN_INTERRUPTED,
        RUN_SAID_TOO_MUCH,
    }
)

# All the process sees of the server's environment (HIVE_FONTC: where fontc
# is, a path and no secret).
_ENVIRONMENT = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "HIVE_FONTC",
)

_runSlot = asyncio.Semaphore(RUNS_AT_ONCE)
_RUN_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def runsFolder(rootPath: pathlib.Path) -> pathlib.Path:
    """Where runs put their files: ``HIVE_RUNS_DIR``, or beside the
    repositories, never among them."""
    configured = os.environ.get(RUNS_DIR_VARIABLE, "").strip()
    return (
        pathlib.Path(configured)
        if configured
        else rootPath.with_name(rootPath.name + ".runs")
    )


def serverId() -> str:
    """This server, for the registry: ``HIVE_SERVER_ID``, or the host's name.
    Stable across restarts, so that a server closes its own runs only."""
    configured = os.environ.get(SERVER_ID_VARIABLE, "").strip()
    name = configured or socket.gethostname()
    return re.sub(r"[^A-Za-z0-9._:-]", "-", name)[:100]


@dataclass(frozen=True)
class RunLimits:
    timeSeconds: float = 30 * 60  # a large CJK font takes long to build
    memoryBytes: int = 16 << 30
    outputBytes: int = 4 << 30

    def lowered(self, limits: Mapping | None) -> "RunLimits":
        """These limits, or lower ones when ``limits`` (a registry's answer)
        has them. A registry cannot raise them."""
        values = {}
        for f in dataclasses.fields(self):
            value = (limits or {}).get(f.name)
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            values[f.name] = (
                min(getattr(self, f.name), value)
                if ok and value > 0
                else getattr(self, f.name)
            )
        return RunLimits(**values)


@dataclass(frozen=True)
class RecipeSource:
    """A recipe in its own repository, at a commit."""

    repository: str  # the repository's uid
    path: str  # owner/name, for the registry
    repoPath: pathlib.Path
    commit: str


@dataclass(frozen=True)
class RunRequest:
    """``source`` for a recipe of its own, or ``builtin`` and ``config`` for
    one of the server's, which is not checked against the policy: nothing in
    it comes from a person."""

    name: str  # the recipe's name: default branch, trailer, messages
    user: str = ""  # the person's stable id, for the registry
    source: RecipeSource | None = None
    builtin: str = ""
    project: str = ""  # builtin: the project it runs on, for the registry
    commit: str = ""  # builtin: the commit it reads
    config: Mapping | None = None
    substitutions: Mapping[str, Any] = field(default_factory=dict)


def downloadRequest(
    project: str, commit: str, stem: str, format: str, *, user: str = ""
) -> RunRequest:
    """The Download panel, as a recipe of one step: read the project at a
    commit, deliver one file."""
    return RunRequest(
        name=DOWNLOAD_RECIPE,
        user=user,
        builtin=DOWNLOAD_RECIPE,
        project=project,
        commit=commit,
        config={
            "steps": [
                {"input": "hive-read", "project": project, "ref": commit},
                {"output": "hive-artifact", "format": format, "name": stem},
            ]
        },
    )


async def recipeSource(
    resolver: ProjectResolver, project: str, ref: str | None = None
) -> RecipeSource | None:
    """The recipe ``project`` at ``ref`` (its default branch when not given);
    None when the person cannot read it or there is no such ref."""
    from .gitstore import GitRepoStore

    parsed = HiveProjectRef(project)
    hiveProject = await resolver.resolve(parsed)
    if hiveProject is None or not hiveProject.can("read"):
        return None
    store = GitRepoStore.open(hiveProject.repoPath)
    try:
        commit = store.resolve(parsed.withRef(ref) or hiveProject.defaultBranch)
        store.commit_info(commit)
    except Exception:
        return None
    finally:
        store.close()
    return RecipeSource(
        hiveProject.repository, hiveProject.path, hiveProject.repoPath, commit
    )


@dataclass
class RunResult:
    id: str
    state: str
    message: str = ""
    durationSeconds: float = 0.0
    reads: list[dict] = field(default_factory=list)
    writes: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)  # kept, in ``directory``
    outputBytes: int = 0
    peakMemoryBytes: int | None = None
    log: str = ""
    directory: pathlib.Path | None = None
    reason: str = ""  # one of REASONS, or nothing

    @property
    def succeeded(self) -> bool:
        return self.state == SUCCEEDED

    def discard(self) -> None:
        """Forget the files (a download, once sent)."""
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)

    def record(self) -> dict:
        """What the registry keeps."""
        return {
            "state": self.state,
            "message": self.message,
            "durationSeconds": self.durationSeconds,
            "inputs": self.reads,
            "writes": self.writes,
            "artifacts": self.artifacts,
            "outputBytes": self.outputBytes,
            "peakMemoryBytes": self.peakMemoryBytes,
            "log": self.log,
        }


# --- the registry ---------------------------------------------------------------


class RunRefused(Exception):
    """The registry says this run cannot take place."""

    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status
        self.text = text


@dataclass(frozen=True)
class RunTicket:
    id: str
    limits: Mapping | None = None
    # False: an older accounts service has no registry; nothing to close.
    registered: bool = True


class RunRegistry(Protocol):
    async def open(self, request: RunRequest) -> RunTicket:
        """May this run take place? Raises :class:`RunRefused` when not."""

    async def close(self, ticket: RunTicket, record: dict) -> None:
        """How it went."""

    async def artifact(
        self, runId: str, name: str, token: str | None, resolver: ProjectResolver
    ) -> dict | None:
        """The artifact, for a person who may read the recipe; else None."""

    async def expired(self) -> list[str]:
        """The runs whose files must go."""

    async def markExpired(self, runs: list[str]) -> None:
        """Their files are gone."""

    async def interrupted(self, before: str) -> list[str]:
        """Runs of this server open since before this moment (ISO 8601)
        stopped with it: closes them, and returns their ids."""


class LocalRunRegistry:
    """``fontra hive-dev``: runs written next to their artifacts. Nothing
    expires on a development server."""

    def __init__(self, runsRoot: pathlib.Path):
        self.runsRoot = runsRoot

    async def open(self, request: RunRequest) -> RunTicket:
        runId = str(uuid.uuid4())
        self._write(runId, {"id": runId, **_openingOf(request), "state": "running"})
        return RunTicket(runId)

    async def close(self, ticket: RunTicket, record: dict) -> None:
        self._write(ticket.id, {**self.get(ticket.id), **record})

    def get(self, runId: str) -> dict:
        return json.loads(self._path(runId).read_text(encoding="utf-8"))

    def _path(self, runId: str) -> pathlib.Path:
        return self.runsRoot / f"{runId}.json"

    def _write(self, runId: str, record: dict) -> None:
        self.runsRoot.mkdir(parents=True, exist_ok=True)
        self._path(runId).write_text(json.dumps(record, indent=1), encoding="utf-8")

    async def artifact(self, runId, name, token, resolver) -> dict | None:
        if not _RUN_ID.match(runId) or not self._path(runId).is_file():
            return None
        record = self.get(runId)
        if not record.get("recipe"):
            return None
        if await resolver.resolve(HiveProjectRef(record["repository"])) is None:
            return None
        return next((a for a in record.get("artifacts", []) if a["name"] == name), None)

    async def expired(self) -> list[str]:
        return []

    async def markExpired(self, runs: list[str]) -> None:
        pass

    async def interrupted(self, before: str) -> list[str]:
        moment = datetime.datetime.fromisoformat(before).timestamp()
        stopped = []
        for path in self.runsRoot.glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("state") == "running" and path.stat().st_mtime < moment:
                self._write(
                    record["id"],
                    {**record, "state": INTERRUPTED, "message": INTERRUPTED_MESSAGE},
                )
                stopped.append(record["id"])
        return stopped


class HiveApiRunRegistry:
    """``fontra hive``: the registry of hive-api. A built-in recipe is what
    the server did before there was a registry, so it goes on without one
    (an older hive-api, or one that is down); a recipe of its own does not."""

    def __init__(self, api, server: str | None = None):
        self.api = api
        self.server = serverId() if server is None else server

    async def open(self, request: RunRequest) -> RunTicket:
        opening = {**_openingOf(request), "server": self.server}
        try:
            status, data = await self.api.recipeRunOpen(opening)
        except HiveApiUnavailable as error:
            if request.builtin:
                logger.warning("recipe run not registered: %s", error)
                return RunTicket(str(uuid.uuid4()), registered=False)
            raise RunRefused(503, "Runs cannot start now: try again later.")
        if status == 201 and isinstance(data, dict):
            runId = str(data.get("id", ""))
            if _RUN_ID.match(runId):
                return RunTicket(runId, data.get("limits"))
        if request.builtin and status == 404 and not isinstance(data, dict):
            return RunTicket(str(uuid.uuid4()), registered=False)
        detail = data.get("detail") if isinstance(data, dict) else None
        raise RunRefused(status, str(detail or "This run cannot take place."))

    async def close(self, ticket: RunTicket, record: dict) -> None:
        if ticket.registered:
            await self.api.recipeRunClose(ticket.id, record)

    async def artifact(self, runId, name, token, resolver) -> dict | None:
        if not _RUN_ID.match(runId):
            return None
        return await self.api.recipeRunArtifact(token, runId, name)

    async def expired(self) -> list[str]:
        return await self.api.recipeRunsExpired()

    async def markExpired(self, runs: list[str]) -> None:
        await self.api.recipeRunsErased(runs)

    async def interrupted(self, before: str) -> list[str]:
        return await self.api.recipeRunsInterrupted(before, self.server)


def _openingOf(request: RunRequest) -> dict:
    source = request.source
    return {
        "user": request.user or None,
        "recipe": source.path if source is not None else None,
        "repository": source.repository if source is not None else None,
        "builtin": request.builtin or None,
        "project": request.project or None,
        "commit": source.commit if source is not None else request.commit,
        "substitutions": dict(request.substitutions),
    }


# --- the server's side ------------------------------------------------------------


async def runRecipe(
    request: RunRequest,
    *,
    resolver: ProjectResolver,
    registry: RunRegistry,
    runsRoot: pathlib.Path,
    limits: RunLimits = RunLimits(),
    onProgress: Callable[[dict], Awaitable[None] | None] | None = None,
    slot: asyncio.Semaphore | None = None,
    serverPaths: Sequence[pathlib.Path] = (),
) -> RunResult:
    """Run a recipe to its end. Whatever happens to the recipe is in the
    result, which the registry has too: this raises only :class:`RunRefused`.
    ``serverPaths``: more paths people must not read in messages and logs."""
    ticket = await registry.open(request)
    limits = limits.lowered(ticket.limits)
    result = RunResult(ticket.id, FAILED, RUN_STOPPED)
    try:
        async with slot or _runSlot:
            await _run(
                request,
                ticket,
                resolver,
                limits,
                runsRoot,
                result,
                onProgress,
                serverPaths,
            )
    except BaseException:
        result.message = RUN_INTERRUPTED
        raise
    finally:
        await asyncio.shield(_close(registry, ticket, result))
    await _sweepAfterRun(registry, runsRoot)
    return result


async def _close(registry: RunRegistry, ticket: RunTicket, result: RunResult) -> None:
    try:
        await registry.close(ticket, result.record())
    except Exception as error:  # the fonts are built: never lose them for this
        logger.error("recipe run %s not recorded: %s", ticket.id, error)


async def _sweepAfterRun(registry: RunRegistry, runsRoot: pathlib.Path) -> None:
    try:
        await sweepExpiredRuns(registry, runsRoot)
    except Exception as error:  # the run is done: the next sweep will do
        logger.warning("expired recipe runs not swept: %s", error)


async def _run(
    request, ticket, resolver, limits, runsRoot, result, onProgress, serverPaths
) -> None:
    runsRoot.mkdir(parents=True, exist_ok=True)
    workDir = pathlib.Path(tempfile.mkdtemp(dir=runsRoot, prefix=WORK_PREFIX))
    started = time.monotonic()
    process = None
    log = _Log()
    said = False  # the process gave its result
    try:
        for name in ("artifacts", "tmp", "home"):
            (workDir / name).mkdir()
        environment = {k: os.environ[k] for k in _ENVIRONMENT if k in os.environ}
        environment["TMPDIR"] = str(workDir / "tmp")
        environment["HOME"] = str(workDir / "home")
        environment["PYTHONUNBUFFERED"] = "1"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "fontra_hive.runner",
            cwd=workDir,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # to stop whatever a compiler started
            limit=LINE_BYTES,
        )
        logTask = asyncio.ensure_future(log.follow(process.stderr))
        await _send(process, _job(request, limits, workDir))
        try:
            async with asyncio.timeout(limits.timeSeconds):
                said = await _follow(process, resolver, result, onProgress)
                await process.wait()
        except TimeoutError:
            result.state = TIMEOUT
            result.message = f"The run took longer than {limits.timeSeconds:g} seconds."
        finally:
            _stop(process)
            await process.wait()
            await asyncio.wait([logTask], timeout=5)
            logTask.cancel()
    except OSError as error:
        logger.exception("recipe run %s", ticket.id)
        result.message = RUN_NOT_STARTED
    finally:
        if process is not None:
            _stop(process)
        result.durationSeconds = round(time.monotonic() - started, 3)
        if result.state == FAILED and process is not None and not said:
            result.reason = STOPPED
        paths = [str(p) for p in (workDir, runsRoot, *serverPaths)]
        result.log = _scrub(log.text(), paths)
        if result.state == FAILED:
            result.message = failedMessage(result)
        result.message = _scrub(result.message, paths)[:MESSAGE_CHARACTERS]
        try:
            if result.succeeded:
                # A built-in recipe keeps nothing: should the server die before
                # the file is sent, its folder is cleared with the working ones.
                folder = (SEND_PREFIX if request.builtin else "") + ticket.id
                _keep(result, workDir / "artifacts", runsRoot / folder)
            else:
                result.artifacts = []
        finally:
            shutil.rmtree(workDir, ignore_errors=True)


# --- files of past runs ------------------------------------------------------------


async def _erase(runsRoot: pathlib.Path, runs) -> list[str]:
    """Remove the files of these runs; returns those that have none left."""
    erased = []
    for run in runs:
        if not isinstance(run, str) or not _RUN_ID.match(run):
            logger.warning("not erasing unexpected recipe run %r", run)
            continue
        folder = runsRoot / run
        await asyncio.to_thread(shutil.rmtree, folder, ignore_errors=True)
        if folder.exists():
            logger.warning("recipe run %s: files not erased", run)
            continue
        erased.append(run)
    return erased


async def sweepExpiredRuns(registry: RunRegistry, runsRoot: pathlib.Path) -> list[str]:
    """Erase the files the registry lists, then tell it. A run that goes
    wrong half way is listed again."""
    erased = await _erase(runsRoot, await registry.expired())
    if erased:
        await registry.markExpired(erased)
    return erased


async def sweepInterruptedRuns(
    registry: RunRegistry, runsRoot: pathlib.Path, before: str
) -> list[str]:
    """Runs a stopped server left open become interrupted, without files: one
    that finished may have set some aside before the registry heard of it."""
    runs = await registry.interrupted(before)
    await _erase(runsRoot, runs)
    return runs


def removeWorkFolders(runsRoot: pathlib.Path) -> list[str]:
    """The folders of runs of a stopped server, which belong to no record.
    Only when the server starts: no run is going on then."""
    if not runsRoot.is_dir():
        return []
    folders = [
        path
        for path in runsRoot.iterdir()
        if path.is_dir() and path.name.startswith((WORK_PREFIX, SEND_PREFIX))
    ]
    for folder in folders:
        shutil.rmtree(folder, ignore_errors=True)
    return sorted(path.name for path in folders)


def _job(request: RunRequest, limits: RunLimits, workDir: pathlib.Path) -> dict:
    source = request.source
    return {
        "name": request.name,
        "workDir": str(workDir),
        "source": (
            {"repoPath": str(source.repoPath), "commit": source.commit}
            if source is not None
            else None
        ),
        "config": dict(request.config) if request.config is not None else None,
        "substitutions": dict(request.substitutions),
        "limits": dataclasses.asdict(limits),
    }


async def _send(process, message: dict) -> None:
    with contextlib.suppress(ConnectionError):  # it stopped: _follow will see
        process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        await process.stdin.drain()


async def _follow(process, resolver, result: RunResult, onProgress) -> bool:
    """Until the process stops talking; True once it gave its result."""
    said = False
    lists = {"read": result.reads, "write": result.writes}
    while True:
        try:
            line = await process.stdout.readline()
        except ValueError:  # a line longer than the stream's limit
            result.message = RUN_SAID_TOO_MUCH
            return said
        if not line:
            return said
        try:
            message = json.loads(line)
            kind = message["type"]
        except (ValueError, KeyError, TypeError):
            continue
        if kind == "resolve":
            await _send(process, await _answer(message, resolver))
        elif kind in lists:
            lists[kind].append(message["record"])
        elif kind == "artifact":
            result.artifacts.append(message["record"])
        elif kind == "result":
            state = message.get("state")
            result.state = state if state in STATES else FAILED
            result.message = str(message.get("message", ""))
            result.outputBytes = int(message.get("outputBytes") or 0)
            result.peakMemoryBytes = message.get("peakMemoryBytes")
            reason = message.get("reason")
            result.reason = reason if reason in REASONS else ""
            said = True
        elif kind == "progress" and onProgress is not None:
            outcome = onProgress(message)
            if asyncio.iscoroutine(outcome):
                await outcome


async def _answer(message: dict, resolver: ProjectResolver) -> dict:
    answer: dict[str, Any] = {"id": message.get("id")}
    try:
        project = await resolver.resolve(HiveProjectRef(message.get("ref")))
    except Exception as error:
        answer["error"] = str(error)
    else:
        answer["project"] = _projectToJson(project)
    return answer


def _stop(process) -> None:
    """The process and whatever it started."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def _keep(result: RunResult, artifacts: pathlib.Path, destination: pathlib.Path):
    destination.mkdir(parents=True)
    kept = []
    for artifact in result.artifacts:
        path = artifacts / pathlib.PurePath(artifact["name"]).name
        if path.is_file() and not path.is_symlink():
            shutil.move(path, destination / path.name)
            kept.append(artifact)
    result.artifacts = kept
    result.directory = destination


class _Log:
    """The end of what a run printed."""

    def __init__(self):
        self.data = b""
        self.truncated = False

    async def follow(self, stream) -> None:
        while chunk := await stream.read(1 << 16):
            self.data += chunk
            if len(self.data) > LOG_BYTES:
                self.data = self.data[-LOG_BYTES:]
                self.truncated = True

    def text(self) -> str:
        text = self.data.decode("utf-8", "replace")
        return "[…]\n" + text if self.truncated else text


def _scrub(text: str, paths) -> str:
    """No path of the server in what people read."""
    for path in paths:
        text = text.replace(path + os.sep, "").replace(path, "")
    return text


def failedMessage(result: RunResult) -> str:
    """What people read of a failed run: one of :data:`FAILED_MESSAGES`."""
    if result.message in FAILED_MESSAGES:
        return result.message  # the server's own
    if PANIC in result.log:
        return COMPILER_CRASHED
    if result.reason == STOPPED:
        return RUN_STOPPED
    return RUN_FAILED


# --- projects, over the pipe ------------------------------------------------------


def _projectToJson(project: HiveProject | None) -> dict | None:
    if project is None:
        return None
    access = project.access
    return {
        "repository": project.repository,
        "path": project.path,
        "repoPath": str(project.repoPath),
        "defaultBranch": project.defaultBranch,
        "access": (
            {
                "username": access.user.username,
                "name": access.user.name,
                "uid": access.user.uid,
                "role": access.role,
                "capabilities": sorted(access.capabilities),
            }
            if access is not None
            else None
        ),
    }


def _projectFromJson(data: dict | None) -> HiveProject | None:
    if data is None:
        return None
    access = data["access"]
    return HiveProject(
        data["repository"],
        data["path"],
        pathlib.Path(data["repoPath"]),
        data["defaultBranch"],
        (
            Access(
                HiveUser(access["username"], access["name"], "", uid=access["uid"]),
                access["role"],
                frozenset(access["capabilities"]),
            )
            if access is not None
            else None
        ),
    )


class _Channel:
    """The process's side of the protocol."""

    def __init__(self, inFd: int, outFd: int):
        self.input = os.fdopen(inFd, "r", encoding="utf-8")
        self.output = os.fdopen(outFd, "w", encoding="utf-8")
        self.lock = asyncio.Lock()
        self.asked = 0

    def send(self, **message) -> None:
        self.output.write(json.dumps(message) + "\n")
        self.output.flush()

    def receive(self) -> dict:
        return json.loads(self.input.readline())

    async def ask(self, **message) -> dict:
        async with self.lock:
            self.asked += 1
            self.send(id=self.asked, **message)
            return await asyncio.to_thread(self.receive)


class _PipeResolver:
    """The server answers, with the checks of the Hive routes."""

    def __init__(self, channel: _Channel):
        self.channel = channel

    async def resolve(self, ref: HiveProjectRef) -> HiveProject | None:
        from fontra.workflow.actions import ActionError

        answer = await self.channel.ask(type="resolve", ref=str(ref))
        if "error" in answer:
            raise ActionError(answer["error"])
        return _projectFromJson(answer.get("project"))


# --- the process's side -----------------------------------------------------------


class _Invalid(Exception):
    """The recipe cannot run as written."""


def _applyLimits(limits: dict) -> None:
    try:
        import resource
    except ImportError:
        return
    settings = [
        (resource.RLIMIT_CORE, 0),
        # Sources on the way to a font are larger than the font: this only
        # keeps one file from filling the disk; the outputs are counted.
        (resource.RLIMIT_FSIZE, int(limits["outputBytes"]) * FILE_BYTES_FACTOR),
    ]
    if sys.platform == "linux":  # macOS does not enforce it
        settings.append((resource.RLIMIT_AS, int(limits["memoryBytes"])))
    for which, value in settings:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(which, (value, value))
    # Writing past RLIMIT_FSIZE is then an error (EFBIG), not a signal.
    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)


def _peakMemory() -> int | None:
    try:
        import resource
    except ImportError:
        return None
    peak = max(
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
    )
    return peak if sys.platform == "darwin" else peak * 1024  # bytes, kilobytes


def _safeTreePath(path: str) -> bool:
    return all(part not in ("", ".", "..", ".git") for part in path.split("/"))


def _readRecipe(job: dict, workDir: pathlib.Path) -> Mapping:
    """The recipe at its commit, its assets copied in the folder."""
    import yaml

    from .gitstore import GitRepoStore

    source = job["source"]
    store = GitRepoStore.open(source["repoPath"])
    try:
        entries = store.list_tree_modes(source["commit"])
        if RECIPE_FILE not in entries:
            raise _Invalid(f"no {RECIPE_FILE} in the recipe's repository")
        if not stat.S_ISREG(entries[RECIPE_FILE][0]):
            raise _Invalid(f"{RECIPE_FILE} is not a file")
        text = store.read_blob(entries[RECIPE_FILE][1])
        prefix = ASSETS_FOLDER + "/"
        for path, (mode, sha) in sorted(entries.items()):
            if not path.startswith(prefix):
                continue
            if not stat.S_ISREG(mode):
                raise _Invalid(f"{path} is not a file: links are refused")
            if not _safeTreePath(path):
                raise _Invalid(f"{path!r} is not a path in the recipe")
            data = store.read_blob(sha)
            # Any asset may be feature code (add-features, or an include):
            # checked before anything runs, as a font's features are.
            check_feature_includes(data.decode("utf-8", "replace"))
            target = workDir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    finally:
        store.close()
    try:
        config = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise _Invalid(f"{RECIPE_FILE} is not YAML: {error}")
    return config


def _prepareConfig(job: dict, workDir: pathlib.Path) -> dict:
    """What ``Workflow`` gets: validated, substitutions applied."""
    if job["source"] is None:
        return job["config"]  # built in
    from fontra.workflow.workflow import substituteStrings

    config = _readRecipe(job, workDir)
    substitutions = job["substitutions"]
    try:
        check = validateHiveRecipe(config, substitutions=substitutions)
    except ImportError as error:
        raise _Invalid(f"this server cannot check recipes: {error}")
    if check.errors:
        raise _Invalid("\n".join(str(error) for error in check.errors))
    from fontra_workflow_ui.validate import engineConfig

    engine = engineConfig(config)
    return substituteStrings(engine, substitutions) if substitutions else engine


def _outputSteps(steps, path="steps") -> list[tuple[str, str]]:
    """(path, action) of the outputs, in the engine's order."""
    outputs = []
    for index, step in enumerate(steps if isinstance(steps, list) else []):
        stepPath = f"{path}[{index}]"
        if isinstance(step, Mapping):
            outputs += _outputSteps(step.get("steps"), f"{stepPath}.steps")
            if "output" in step:
                outputs.append((stepPath, str(step["output"])))
    return outputs


class _Provenance:
    """Tells the server what the context gained, as soon as it is known."""

    def __init__(self, channel: _Channel, context: HiveRecipeContext):
        self.channel = channel
        self.context = context
        self.sent = {"read": 0, "write": 0, "artifact": 0}

    def flush(self) -> None:
        context = self.context
        for kind, records in (
            ("read", context.reads),
            ("write", context.writes),
            ("artifact", context.artifacts),
        ):
            for record in records[self.sent[kind] :]:
                self.channel.send(type=kind, record=dataclasses.asdict(record))
            self.sent[kind] = len(records)


def _refusedFeatures(error: BaseException, depth: int = 0) -> bool:
    """Whether ``error`` is, or comes from, refused feature code."""
    from .export import FeaturesRefused

    if isinstance(error, FeaturesRefused):
        return True
    if depth > 10:
        return False
    if isinstance(error, BaseExceptionGroup):
        return any(_refusedFeatures(e, depth + 1) for e in error.exceptions)
    cause = error.__cause__ or error.__context__
    return cause is not None and _refusedFeatures(cause, depth + 1)


async def _execute(
    job: dict, channel: _Channel, context: HiveRecipeContext
) -> tuple[str, str, str]:
    """(state, message, reason)."""
    from fontra.workflow.workflow import Workflow

    workDir = pathlib.Path(job["workDir"])
    artifactsDir = workDir / "artifacts"
    provenance = _Provenance(channel, context)
    current = None
    try:
        channel.send(type="progress", stage="validate", state="running")
        config = _prepareConfig(job, workDir)
        channel.send(type="progress", stage="access", state="running")
        await requireRecipeAccess(config, context)
        channel.send(type="progress", stage="run", state="running")
        outputSteps = _outputSteps(config.get("steps"))
        with hiveRecipeContext(context):
            workflow = Workflow(config=config, parentDir=workDir)
            async with workflow.endPoints() as endPoints:
                provenance.flush()
                for index, output in enumerate(endPoints.outputs):
                    step, action = (
                        outputSteps[index]
                        if len(outputSteps) == len(endPoints.outputs)
                        else (f"outputs[{index}]", type(output).__name__)
                    )
                    current = dict(type="progress", step=step, action=action)
                    channel.send(**current, state="running")
                    await output.process(artifactsDir, continueOnError=False)
                    provenance.flush()
                    channel.send(**current, state="done")
                    current = None
        return SUCCEEDED, "", ""
    except _Invalid as error:
        return INVALID, str(error)[:MESSAGE_CHARACTERS], ""
    except RecipeAccessDenied as error:
        return DENIED, str(error)[:MESSAGE_CHARACTERS], ""
    except OutputLimitExceeded as error:
        return TOO_LARGE, str(error), ""
    except MemoryError:
        return OUT_OF_MEMORY, "The run needed more memory than it may use.", ""
    except SystemExit as error:  # how fontmake ends
        if not isinstance(error.code, (int, type(None))):
            logger.warning("the compiler stopped: %s", error.code)
        return FAILED, "", ""
    except Exception as error:
        if isinstance(error, OSError) and error.errno == errno.EFBIG:
            return TOO_LARGE, "A file was larger than this run may produce.", ""
        if _refusedFeatures(error):
            from .export import FEATURES_REFUSED

            return INVALID, FEATURES_REFUSED, REFUSED_FEATURES
        logger.exception("the run failed")
        return FAILED, "", ""
    finally:
        provenance.flush()
        if current is not None:
            channel.send(**current, state="failed")


def main() -> int:
    # The protocol keeps its own descriptors: a compiler's output is log.
    channel = _Channel(os.dup(0), os.dup(1))
    os.dup2(2, 1)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    # One line per font built: the engine, how long (fontra_hive.export).
    logging.getLogger("fontra_hive.export").setLevel(logging.INFO)

    job = channel.receive()
    _applyLimits(job["limits"])
    context = HiveRecipeContext(
        resolver=_PipeResolver(channel),
        recipe=job["name"],
        outputLimit=int(job["limits"]["outputBytes"]),
        # A recipe of the server reads features only to compile, which checks.
        checkFeatures=job["source"] is not None,
        # What a build says goes to the server as progress (the Download panel).
        notify=lambda event: channel.send(type="progress", **event),
    )
    state, message, reason = asyncio.run(_execute(job, channel, context))
    channel.send(
        type="result",
        state=state,
        message=message,
        reason=reason,
        outputBytes=context.outputBytes,
        peakMemoryBytes=_peakMemory(),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
