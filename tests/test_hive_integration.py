"""The Hive project manager against a real hive-api (a Django development
server in a subprocess), through this server's relay, as a browser would.

Skipped unless hive-api's checkout is found (``$HIVE_API_DIR``, or a
``hive-api`` folder next to this repository) and Django is importable.
"""

import asyncio
import os
import pathlib
import socket
import subprocess
import sys
import time
import zipfile
from types import SimpleNamespace

import pytest

aiohttp = pytest.importorskip("aiohttp")
pytest.importorskip("jwt")
import yarl  # noqa: E402
from aiohttp import web  # noqa: E402

from fontra_hive.hiveapi import HiveApi, token_for_uid  # noqa: E402
from fontra_hive.hivemanager import HiveProjectManager  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
API_DIR = pathlib.Path(os.environ.get("HIVE_API_DIR", HERE.parent.parent / "hive-api"))
PASSWORD = "correct horse battery staple"

if not (API_DIR / "manage.py").exists():
    pytest.skip(f"no hive-api checkout at {API_DIR}", allow_module_level=True)
try:
    import django  # noqa: F401
except ImportError:
    pytest.skip("Django is not installed", allow_module_level=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


SEED = """
from hive.models import *
users = {n: User.objects.create_user(n, n + "@example.com", %(pw)r, name=full)
         for n, full in [("jeremie", "Jérémie Hornus"), ("fabio", "Fabio Rossi"),
                         ("ana", "Ana López")]}
org = Organization.objects.create(login="uplustype", name="U+Type", base_role="designer")
Membership.objects.create(organization=org, user=users["jeremie"], role="owner")
Membership.objects.create(organization=org, user=users["fabio"], role="member")
Project.objects.create(owner_org=org, name="Mutator")
for n, u in users.items():
    print("UID", n, u.uid)
"""


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    data = tmp_path_factory.mktemp("hive-api")
    fontraPort, apiPort = free_port(), free_port()
    env = {
        **os.environ,
        "HIVE_DEBUG": "1",
        "HIVE_DATA_DIR": str(data),
        "HIVE_PUBLIC_URL": f"http://127.0.0.1:{fontraPort}",
        "HIVE_SERVICE_KEY": "test-service-key",
        "HIVE_MAIL_IN_BACKGROUND": "0",
        "PYTHONPATH": os.pathsep.join(
            [str(API_DIR), *os.environ.get("PYTHONPATH", "").split(os.pathsep)]
        ),
    }

    def manage(*args, **kwargs):
        return subprocess.run(
            [sys.executable, "manage.py", *args],
            cwd=API_DIR,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            **kwargs,
        ).stdout

    manage("migrate", "-v0")
    uids = dict(
        line.split()[1:]
        for line in manage("shell", "-c", SEED % {"pw": PASSWORD}).split("\n")
        if line.startswith("UID ")
    )
    link = (
        manage(
            "invite",
            "zoe@example.com",
            "--project",
            "uplustype/Mutator",
            "--role",
            "observer",
            "--no-email",
        )
        .strip()
        .splitlines()[-1]
    )
    server = subprocess.Popen(
        [
            sys.executable,
            "manage.py",
            "runserver",
            f"127.0.0.1:{apiPort}",
            "--noreload",
        ],
        cwd=API_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", apiPort), timeout=0.5).close()
                break
            except OSError:
                if time.time() > deadline or server.poll() is not None:
                    pytest.fail("hive-api did not start")
                time.sleep(0.2)
        yield SimpleNamespace(
            fontraPort=fontraPort,
            apiURL=f"http://127.0.0.1:{apiPort}",
            uids=uids,
            invitationToken=link.rsplit("#", 1)[1],
        )
    finally:
        server.terminate()
        server.wait(10)


def run(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


def test_sign_in_roles_projects_and_invitations_through_the_relay(
    stack, tmp_path, fixture_fontra
):
    origin = f"http://127.0.0.1:{stack.fontraPort}"

    async def go():
        manager = HiveProjectManager(
            tmp_path, api=HiveApi(stack.apiURL, "test-service-key"), commitDelay=0.05
        )
        app = web.Application()
        app.add_routes(
            [web.get("/", manager.rootDocumentHandler), *manager.webRoutes()]
        )
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", stack.fontraPort).start()
        jar = aiohttp.CookieJar(unsafe=True)
        browser = aiohttp.ClientSession(
            origin, cookie_jar=jar, headers={"Origin": origin}
        )
        try:
            # Not signed in: the root page is the sign-in page.
            page = await (await browser.get("/")).text()
            assert 'data-page="login"' in page
            assert (await browser.get("/api/hive/me")).status == 401

            # Signing in goes through the relay to hive-api, which sets the cookies.
            response = await browser.post(
                "/api/auth/login", json={"login": "fabio", "password": PASSWORD}
            )
            assert response.status == 200, await response.text()
            me = await (await browser.get("/api/hive/me")).json()
            assert me["user"]["username"] == "fabio" and me["source"] == "hive-api"

            # His role comes from hive-api; the repository is made on first use.
            access = await (
                await browser.get("/api/hive/projects/uplustype%2Fmutator/access")
            ).json()
            assert access["role"] == "designer" and "edit" in access["capabilities"]
            repos = list(tmp_path.glob("*.git"))
            assert [r.name for r in repos] == [f"{_projectUid(repos)}.git"]
            log = await (
                await browser.get("/api/hive/projects/uplustype%2FMutator/log")
            ).json()
            assert log["commits"][0]["author"] == "Fabio Rossi"
            assert log["commits"][0]["message"].startswith(
                "New project uplustype/Mutator"
            )

            # Fontra's side: the project list and the shared handler, per role.
            fabio = token_for_uid(stack.uids["fabio"])
            ana = token_for_uid(stack.uids["ana"])
            assert await manager.getProjectList(fabio) == ["uplustype/Mutator"]
            assert await manager.getProjectList(ana) == []
            handler = await manager.getRemoteSubject("uplustype/Mutator", fabio)
            assert handler is not None
            assert handler.projectIdentifier == "uplustype/Mutator@main"
            assert await manager.getRemoteSubject("uplustype/Mutator", ana) is None
            connection = SimpleNamespace(authorizationToken=fabio, clientUUID="c1")
            assert await handler.isReadOnly(connection=connection) is False

            # hive-api's own routes: yes; its internal ones: not from outside.
            members = await (
                await browser.get("/api/projects/uplustype/Mutator/members")
            ).json()
            assert [m["username"] for m in members["members"]] == ["jeremie", "fabio"]
            assert (
                await browser.get("/api/internal/project?project=uplustype/Mutator")
            ).status == 404
            response = await browser.post(
                "/api/projects/uplustype/Mutator/invitations",
                json={"email": "x@example.com", "role": "observer"},
            )
            assert response.status == 403  # a designer cannot invite

            # A cross-site page cannot use the cookies.
            evil = await browser.get(
                "/api/hive/me", headers={"Origin": "https://evil.example"}
            )
            assert evil.status == 401

            # Signing out.
            assert (await browser.post("/api/auth/logout")).status == 204
            assert (await browser.get("/api/hive/me")).status == 401

            # Zoé signs up with her invitation and can read, not edit.
            response = await browser.post(
                "/api/invitations/signup",
                json={
                    "token": stack.invitationToken,
                    "username": "zoe",
                    "name": "Zoé",
                    "password": PASSWORD,
                },
            )
            assert response.status == 201, await response.text()
            access = await (
                await browser.get("/api/hive/projects/uplustype%2FMutator/access")
            ).json()
            assert access["role"] == "observer"
            zoe = await manager.authorize(
                SimpleNamespace(
                    cookies={
                        "hive_access": jar.filter_cookies(yarl.URL(origin))[
                            "hive_access"
                        ].value
                    },
                    headers={},
                    host=f"127.0.0.1:{stack.fontraPort}",
                )
            )
            zoeConnection = SimpleNamespace(authorizationToken=zoe, clientUUID="c2")
            await manager.api.access(zoe, "uplustype/Mutator")
            assert await handler.isReadOnly(connection=zoeConnection) is True

            # Jérémie, from Hive's home page: a new project from a font.
            await browser.post("/api/auth/logout")
            response = await browser.post(
                "/api/auth/login", json={"login": "jeremie", "password": PASSWORD}
            )
            assert response.status == 200
            assert 'data-page="home"' in await (await browser.get("/")).text()
            response = await browser.post(
                "/api/projects", json={"name": "Imported", "owner": "uplustype"}
            )
            assert response.status == 201, await response.text()
            archive = tmp_path / "upload.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                for path in sorted(fixture_fontra.rglob("*")):
                    zf.write(path, f"MyFont.fontra/{path.relative_to(fixture_fontra)}")

            async def upload():
                data = aiohttp.FormData()
                data.add_field("file", archive.read_bytes(), filename="MyFont.zip")
                return await browser.post(
                    "/api/hive/projects/uplustype%2FImported/import", data=data
                )

            async def hasRepository():
                response = await browser.get(
                    "/api/hive/projects/uplustype%2FImported/repository"
                )
                assert response.status == 200, await response.text()
                return (await response.json())["exists"]

            # A failed import creates nothing: the project has no repository
            # (asking does not create one), and the home page says so.
            assert await hasRepository() is False
            bad = aiohttp.FormData()
            bad.add_field("file", b"not a font", filename="broken.zip")
            response = await browser.post(
                "/api/hive/projects/uplustype%2FImported/import", data=bad
            )
            assert response.status == 422, await response.text()
            assert await hasRepository() is False

            response = await upload()
            assert response.status == 200, await response.text()
            assert (await response.json())["created"] is True
            assert await hasRepository() is True
            log = await (
                await browser.get("/api/hive/projects/uplustype%2FImported/log")
            ).json()
            assert [c["message"].splitlines()[0] for c in log["commits"]] == [
                "Import MyFont.zip"
            ]
            assert log["commits"][0]["author"] == "Jérémie Hornus"
            response = await upload()  # again: a new version on top
            assert (await response.json())["created"] is False

            # Download: the sources, zipped (fonts only where fontmake is).
            formats = await (await browser.get("/api/hive/export-formats")).json()
            assert [f["format"] for f in formats["formats"]][:2] == [
                "fontra",
                "designspace",
            ]
            response = await browser.get(
                "/api/hive/projects/uplustype%2FImported/export?format=fontra"
            )
            assert response.status == 200, await response.text()
            assert (
                'filename="Imported.fontra.zip"'
                in response.headers["Content-Disposition"]
            )
            archive_bytes = await response.read()
            import io

            names = zipfile.ZipFile(io.BytesIO(archive_bytes)).namelist()
            assert "Imported.fontra/font-data.json" in names
            response = await browser.get(
                "/api/hive/projects/uplustype%2FImported/export?format=exe"
            )
            assert response.status == 400

            # A designer cannot replace the font.
            await browser.post("/api/auth/logout")
            await browser.post(
                "/api/auth/login", json={"login": "fabio", "password": PASSWORD}
            )
            assert (await upload()).status == 403
            # Nor download it (export is for managers and admins).
            response = await browser.get(
                "/api/hive/projects/uplustype%2FImported/export?format=fontra"
            )
            assert response.status == 403
        finally:
            await browser.close()
            await manager.aclose()
            await runner.cleanup()

    run(go())


def _projectUid(repos):
    return repos[0].name[: -len(".git")]
