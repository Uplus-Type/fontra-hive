"""A bench for the tests of the R2c audit fixes (test_audit_r2c.py): a
development project manager with a directory of people, and connections as
Fontra's websocket makes them."""
import json
from types import SimpleNamespace

from fontra_hive.access import DevDirectory, token_for
from fontra_hive.gitstore import GitRepoStore, Signature
from fontra_hive.projectmanager import DEV_USER_COOKIE, DevHiveProjectManager

ME = Signature("Import", "import@example.com")

USERS = {
    "users": {
        "jeremie": {"name": "Jeremie"},
        "fabio": {"name": "Fabio"},
        "ana": {"name": "Ana"},
        "mei": {"name": "Mei"},
        "olga": {"name": "Olga"},
    },
    "organizations": {
        "org": {
            "name": "Org",
            "members": {"jeremie": "owner", "fabio": "member", "ana": "member"},
            "base_role": "designer",
        }
    },
    "projects": {
        "Mutator": {
            "owner": "org",
            "collaborators": {"mei": "manager", "olga": "observer"},
            # Fabio: no font settings, no kerning, no plug-in data.
            "writers": {"fontsettings": ["ana"], "kerning": ["ana"], "plugindata": ["ana"]},
        },
    },
}


def make_manager(tmp_path, fontra_dir, users=USERS, **kw):
    path = tmp_path / "hive-dev-users.json"
    path.write_text(json.dumps(users), encoding="utf-8")
    directory = DevDirectory(path)
    root = tmp_path / "repos"
    root.mkdir()
    store = GitRepoStore.create(root / "Mutator.git")
    store.import_directory(fontra_dir, message="Import", author=ME)
    store.close()
    return DevHiveProjectManager(root, directory=directory, commitDelay=0.05, **kw)


def connection(user, uuid=None):
    async def externalChange(change, isLiveChange):
        pass

    async def messageFromServer(*a):
        pass

    return SimpleNamespace(
        authorizationToken=token_for(user),
        clientUUID=uuid or f"c-{user}",
        proxy=SimpleNamespace(externalChange=externalChange, messageFromServer=messageFromServer),
    )


def request(project, user=None, method="GET", body=None, **query):
    async def json_():
        if body is None:
            raise ValueError("no body")
        return body

    return SimpleNamespace(
        match_info={"name": project},
        query=query,
        cookies={DEV_USER_COOKIE: user} if user else {},
        headers={"Origin": "http://localhost:8000"},
        host="localhost:8000",
        method=method,
        json=json_,
        path=f"/api/hive/projects/{project}",
        rel_url=None,
    )


async def body(response):
    return json.loads(response.body)


def store_of(manager):
    return GitRepoStore.open(manager.rootPath / "Mutator.git")
