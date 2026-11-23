"""What held at the final check of R2c (23 Nov 2026), kept as tests, and
the runner's sentence.

- Identical computations shared between people of different rights
  (HistoryWork, a key without the person): each person's answer is the one
  they get alone, and the computation is made once. Mei: manager; Fabio:
  designer without the font settings; Olga: observer; Ana: designer.
- The recipe runner, in its subprocess, refuses a branch that follows the
  remote repository (``upstream/…``), for the owner and for a designer.
- When a run stops on one of Hive's actions (an ``ActionError``), the
  person reads the action's sentence after "The run failed.", on one line,
  never a trace; another action's words stay in the log."""

import asyncio
import json
import threading
import time
import types

import pytest

pytest.importorskip("fontra")

from conftest import run  # noqa: E402
from fontra_hive import runner  # noqa: E402
from fontra_hive.access import DevDirectory, token_for  # noqa: E402
from fontra_hive.gitstore import FOLLOWS_REMOTE, GitRepoStore, Signature  # noqa: E402
from fontra_hive.projectmanager import DevHiveProjectManager  # noqa: E402
from fontra_hive.recipe import projectResolverFor  # noqa: E402
from r2c_bench import make_manager, request, store_of  # noqa: E402

MEI = Signature("Mei", "mei@example.com")
AUTHOR = Signature("Importer", "importer@example.com")
PEOPLE = ["mei", "fabio", "olga", "ana"]


# --- sharing between people of different rights ------------------------------------


def setup(manager):
    store = store_of(manager)
    try:
        head = store.head()
        store.create_branch("exp", head)
        data = json.loads(store.read_file(head, "font-data.json"))
        data.setdefault("fontInfo", {})["familyName"] = "Mei's experiment"
        store.commit(
            {"font-data.json": json.dumps(data).encode()}, branch="exp", message="exp", author=MEI
        )
        store.commit(
            {"features.txt": b"languagesystem DFLT dflt;\n"}, branch="exp", message="fea", author=MEI
        )
    finally:
        store.close()


def slow(manager, name, counter):
    original = getattr(manager, name)

    def wrapper(*args, **kwargs):
        with counter["lock"]:
            counter[name] = counter.get(name, 0) + 1
        time.sleep(0.4)
        return original(*args, **kwargs)

    setattr(manager, name, wrapper)


async def answer(manager, handler, who, **query):
    try:
        response = await getattr(manager, handler)(request("Mutator", who, **query))
        return response.status, json.loads(response.body)
    except Exception as error:  # 403, 404…
        return type(error).__name__, getattr(error, "text", str(error))


@pytest.mark.parametrize(
    "handler, query, heavy",
    [
        ("mergePreviewHandler", {"from": "exp", "into": "main"}, "_mergeState"),
        ("mergePreviewHandler", {"from": "main", "into": "exp"}, "_mergeState"),
        ("fontDiffHandler", {"branch": "exp"}, "_fontDiff"),
        ("logHandler", {"branch": "exp", "kind": "font"}, "_log"),
    ],
    ids=["preview-into-main", "preview-into-exp", "font-diff", "log"],
)
def test_shared_answers_are_each_persons_own(tmp_path, fixture_fontra, handler, query, heavy):
    manager = make_manager(tmp_path, fixture_fontra)
    setup(manager)

    async def go():
        alone = {who: await answer(manager, handler, who, **query) for who in PEOPLE}
        counter = {"lock": threading.Lock()}
        slow(manager, heavy, counter)
        together = await asyncio.gather(
            *(answer(manager, handler, who, **query) for who in PEOPLE)
        )
        await manager.aclose()
        return alone, dict(zip(PEOPLE, together)), counter.get(heavy, 0)

    alone, together, computed = asyncio.run(go())
    assert computed == 1
    for who in PEOPLE:
        assert together[who] == alone[who], who
    if query.get("into") == "main":
        assert alone["mei"][1]["closedDomains"] == []
        assert alone["mei"][1]["canMerge"] is True
        assert alone["fabio"][1]["closedDomains"] == ["fontsettings"]
        assert alone["fabio"][1]["canMerge"] is False
        assert alone["olga"][1]["canMerge"] is False


def test_a_person_without_access_cannot_join(tmp_path, fixture_fontra):
    manager = make_manager(tmp_path, fixture_fontra)
    setup(manager)

    async def go():
        counter = {"lock": threading.Lock()}
        slow(manager, "_mergeState", counter)
        q = {"from": "exp", "into": "main"}
        results = await asyncio.gather(
            answer(manager, "mergePreviewHandler", "mei", **q),
            answer(manager, "mergePreviewHandler", "nobody", **q),
            answer(manager, "mergePreviewHandler", None, **q),
        )
        await manager.aclose()
        return results

    mei, nobody, anonymous = asyncio.run(go())
    assert mei[0] == 200
    assert nobody[0] != 200 and anonymous[0] != 200


# --- the runner ---------------------------------------------------------------------

USERS = {
    "users": {"jeremie": {"name": "Jeremie"}, "fabio": {"name": "Fabio"}},
    "projects": {
        "Latin": {"owner": "jeremie", "collaborators": {"fabio": "designer"}},
        "Target": {"owner": "jeremie", "collaborators": {"fabio": "designer"}},
        "Build": {"owner": "jeremie", "collaborators": {"fabio": "designer"}},
    },
}


@pytest.fixture
def hive(tmp_path, fixture_fontra):
    root = tmp_path / "repos"
    root.mkdir()
    for name in ("Latin", "Target"):
        store = GitRepoStore.create(root / f"{name}.git")
        store.import_directory(fixture_fontra, message="Import", author=AUTHOR)
        store.close()
    store = GitRepoStore.open(root / "Target.git")
    try:
        store.create_branch("upstream/main", store.head())
    finally:
        store.close()
    latin = GitRepoStore.open(root / "Latin.git")
    try:  # a glyph more in Latin: the write changes something
        glyphs = [p for p in latin.list_tree(latin.head()) if p.startswith("glyphs/")]
        data = latin.read_file(latin.head(), glyphs[0])
        latin.commit({"glyphs/zzextra.json": data}, message="extra", author=AUTHOR)
    finally:
        latin.close()
    GitRepoStore.create(root / "Build.git").close()
    users = tmp_path / "users.json"
    users.write_text(json.dumps(USERS))
    manager = DevHiveProjectManager(root, directory=DevDirectory(users))
    return types.SimpleNamespace(root=root, manager=manager, runs=manager.runsRoot)


def recipe(branch):
    return f"""
steps:
- input: hive-read
  project: Latin
- output: hive-write
  project: Target
  branch: {branch}
"""


def start(hive, text, user="jeremie"):
    store = GitRepoStore.open(hive.root / "Build.git")
    try:
        store.commit({"recipe.yaml": text.encode()}, message="recipe", author=AUTHOR)
    finally:
        store.close()

    async def go():
        resolver = projectResolverFor(hive.manager, token_for(user))
        source = await runner.recipeSource(resolver, "Build", None)
        req = runner.RunRequest(name="build", source=source)
        return await runner.runRecipe(
            req, resolver=resolver, registry=hive.manager.recipeRuns, runsRoot=hive.runs
        )

    return run(go())


def heads(hive):
    store = GitRepoStore.open(hive.root / "Target.git")
    try:
        return {b: store.head(b) for b in store.branches()}
    finally:
        store.close()


@pytest.mark.parametrize("user", ["jeremie", "fabio"])
def test_the_runner_refuses_upstream_and_says_why(hive, user):
    before = heads(hive)
    result = start(hive, recipe("upstream/main"), user)
    assert result.state == runner.FAILED
    assert result.message == f"{runner.RUN_FAILED} hive-write: {FOLLOWS_REMOTE}"
    assert "\n" not in result.message and "Traceback" not in result.message
    assert heads(hive) == before


@pytest.mark.parametrize("name", ["Upstream/main", "UPSTREAM/main"])
def test_the_runner_refuses_a_case_variant_of_upstream(hive, name):
    before = heads(hive)
    result = start(hive, recipe(name))
    assert result.state == runner.FAILED
    assert FOLLOWS_REMOTE in result.message
    assert heads(hive) == before


def test_the_sentence_of_an_action():
    from fontra.workflow.actions import ActionError

    error = ActionError("hive-write: Font settings are kept\nto some people.")
    assert runner.actionSentence(error) == "hive-write: Font settings are kept to some people."
    assert runner.actionSentence(ActionError("add-features: word-of-the-witness")) is None
    assert runner.actionSentence(ValueError("hive-write: x")) is None
    wrapped = RuntimeError("outer")
    wrapped.__cause__ = ActionError("hive-read: no such ref 'v9'")
    assert runner.actionSentence(wrapped) == "hive-read: no such ref 'v9'"
    group = ExceptionGroup("g", [ValueError("x"), ActionError("hive-write: refused")])
    assert runner.actionSentence(group) == "hive-write: refused"


def test_what_people_read_of_a_failed_run():
    def failed(message="", log=""):
        result = runner.RunResult("x", runner.FAILED, message, log=log)
        return runner.failedMessage(result)

    assert failed("hive-write: refused") == f"{runner.RUN_FAILED} hive-write: refused"
    assert failed("add-features: word") == runner.RUN_FAILED
    assert failed("hive-write: two\nlines") == runner.RUN_FAILED
