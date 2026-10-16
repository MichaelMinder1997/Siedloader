"""End-to-End-Tests gegen einen lokalen HTTP-Server mit einer RSS-Playlist.

Benoetigt ffmpeg, um kleine Testvideos zu erzeugen.
"""

import base64
import functools
import http.server
import importlib
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg fehlt")

APP_DIR = Path(__file__).resolve().parent.parent / "app"
AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:geheim").decode()}


@pytest.fixture(scope="session")
def media_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("srv")
    for i in range(1, 4):
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=1:size=160x120:rate=5",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={300 + i * 100}:duration=1",
                "-shortest",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(root / f"v{i}.mp4"),
            ],
            check=True,
        )
    handler = functools.partial(QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def feed(name: str, count: int, broken: tuple[int, ...] = ()) -> str:
        items = []
        for i in range(1, count + 1):
            target = "missing" if i in broken else f"v{(i - 1) % 3 + 1}"
            items.append(
                f"<item><title>Folge {i}</title><guid>{name}-{i}</guid>"
                f'<enclosure url="{base}/{target}.mp4" type="video/mp4"/></item>'
            )
        rss = f"<rss version='2.0'><channel><title>{name}</title>{''.join(items)}</channel></rss>"
        (root / f"{name}.xml").write_text('<?xml version="1.0"?>' + rss)
        return f"{base}/{name}.xml"

    yield feed
    server.shutdown()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setenv("ALLOW_PRIVATE_URLS", "1")
    monkeypatch.setenv("APP_PASSWORD", "geheim")
    monkeypatch.setenv("MIN_FREE_GB", "0")
    monkeypatch.syspath_prepend(str(APP_DIR))
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    with TestClient(main.app, headers=AUTH) as c:
        c.main = main
        yield c
    main.executor.shutdown(cancel_futures=True)


def wait_for(client, job_id, statuses=("done", "error", "cancelled"), timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.3)
    raise AssertionError(f"Job {job_id} nicht fertig: {job}")


def test_requires_password(client):
    assert client.get("/", headers={"Authorization": ""}).status_code == 401
    assert client.get("/api/jobs", headers={"Authorization": "Basic eDp5"}).status_code == 401
    assert client.get("/").status_code == 200


def test_playlist_skips_broken_entries(client, media_server):
    url = media_server("mixed", 5, broken=(3,))
    job = client.post("/api/jobs", json={"url": url}).json()
    job = wait_for(client, job["id"])
    assert job["status"] == "done"
    assert (job["completed"], job["failed"], job["total"]) == (4, 1, 5)
    files = client.get(f"/api/jobs/{job['id']}/files").json()
    assert files["total"] == 4


def test_audio_mode(client, media_server):
    job = client.post("/api/jobs", json={"url": media_server("audio", 2), "mode": "audio"}).json()
    job = wait_for(client, job["id"])
    names = [f["name"] for f in client.get(f"/api/jobs/{job['id']}/files").json()["files"]]
    assert len(names) == 2 and all(n.endswith(".mp3") for n in names)


def test_large_playlist_cancel_and_resume(client, media_server):
    url = media_server("large", 150)
    job_id = client.post("/api/jobs", json={"url": url}).json()["id"]
    wait_for(client, job_id, statuses=("running",))
    time.sleep(1)
    client.post(f"/api/jobs/{job_id}/cancel")
    job = wait_for(client, job_id)
    assert job["status"] == "cancelled"
    assert job["completed"] < 150

    client.post(f"/api/jobs/{job_id}/resume")
    job = wait_for(client, job_id)
    assert job["status"] == "done"
    assert job["completed"] == 150

    page = client.get(f"/api/jobs/{job_id}/files?offset=100&limit=100").json()
    assert page["total"] == 150 and len(page["files"]) == 50
    # Reihenfolge der Playlist, nicht alphabetisch
    assert page["files"][0]["name"].startswith("101 - ")
    assert page["files"][-1]["name"].startswith("150 - ")


def test_zip_and_file_access(client, media_server):
    job_id = client.post("/api/jobs", json={"url": media_server("zip", 3)}).json()["id"]
    wait_for(client, job_id)
    names = [f["name"] for f in client.get(f"/api/jobs/{job_id}/files").json()["files"]]

    res = client.get(f"/api/jobs/{job_id}/zip")
    assert res.status_code == 200
    assert sorted(zipfile.ZipFile(BytesIO(res.content)).namelist()) == sorted(names)

    assert client.get(f"/api/jobs/{job_id}/files/{names[0]}").status_code == 200
    assert client.get(f"/api/jobs/{job_id}/files/.archive.txt").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/files/..%2F.jobs.json").status_code == 404


def test_rejects_private_urls(client, monkeypatch):
    monkeypatch.setattr(client.main, "ALLOW_PRIVATE_URLS", False)
    for url in ("http://127.0.0.1/x", "http://192.168.1.1/", "file:///etc/passwd", "ftp://example.com/"):
        assert client.post("/api/jobs", json={"url": url}).status_code == 400


def test_resumes_after_restart(client, media_server, tmp_path):
    job_id = client.post("/api/jobs", json={"url": media_server("restart", 3)}).json()["id"]
    wait_for(client, job_id)
    # Zustand so setzen, als waere der Server mitten im Download beendet worden.
    state = client.main.STATE_FILE
    state.write_text(state.read_text().replace('"status": "done"', '"status": "running"'))

    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    with TestClient(main.app, headers=AUTH) as restarted:
        job = wait_for(restarted, job_id)
        assert job["status"] == "done"
        assert job["completed"] == 3
    main.executor.shutdown(cancel_futures=True)
