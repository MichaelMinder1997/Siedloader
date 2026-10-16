"""Siedloader: Video- und Playlist-Downloader auf Basis von yt-dlp.

Playlists werden ohne Groessenbeschraenkung heruntergeladen:
- Eintraege werden fortlaufend abgearbeitet (lazy_playlist), ein Start ist
  also auch bei Playlists mit zehntausenden Videos sofort moeglich.
- Ein fehlerhaftes Video bricht die Playlist nicht ab (ignoreerrors).
- Jeder Job fuehrt ein Download-Archiv. Nach einem Abbruch oder Neustart
  macht der Job dort weiter, wo er aufgehoert hat.
- Fertige Dateien koennen einzeln oder als ZIP gestreamt werden. Das ZIP
  wird nicht zwischengespeichert, daher gibt es auch hier keine Grenze.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from yt_dlp.utils import DownloadCancelled
from zipstream import ZipStream

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads")).resolve()
MAX_PARALLEL_JOBS = int(os.environ.get("MAX_PARALLEL_JOBS", "2"))
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "2"))
APP_USER = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
ALLOW_PRIVATE_URLS = os.environ.get("ALLOW_PRIVATE_URLS", "0") == "1"
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")

STATE_FILE = DOWNLOAD_DIR / ".jobs.json"
ARCHIVE_NAME = ".archive.txt"
HIDDEN_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")
MAX_LOGGED_ERRORS = 500

QUALITY_FORMATS = {
    "best": "bestvideo*+bestaudio/best",
    "1080": "bestvideo*[height<=1080]+bestaudio/best[height<=1080]/best",
    "720": "bestvideo*[height<=720]+bestaudio/best[height<=720]/best",
    "480": "bestvideo*[height<=480]+bestaudio/best[height<=480]/best",
}


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_state()
    yield


app = FastAPI(title="Siedloader", lifespan=lifespan)
executor = ThreadPoolExecutor(max_workers=MAX_PARALLEL_JOBS)
jobs: dict[str, Job] = {}
jobs_lock = threading.Lock()
state_lock = threading.Lock()


@app.middleware("http")
async def require_auth(request: Request, call_next):
    # Schuetzt Oberflaeche und API gleichermassen, sobald APP_PASSWORD gesetzt ist.
    if APP_PASSWORD:
        expected = "Basic " + base64.b64encode(f"{APP_USER}:{APP_PASSWORD}".encode()).decode()
        given = request.headers.get("authorization", "")
        if not secrets.compare_digest(given.encode(), expected.encode()):
            return Response(
                "Anmeldung erforderlich",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Siedloader"'},
            )
    return await call_next(request)


class JobRequest(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    mode: Literal["video", "audio"] = "video"
    quality: Literal["best", "1080", "720", "480"] = "best"


class Job:
    def __init__(self, url: str, mode: str, quality: str, job_id: str | None = None):
        self.id = job_id or uuid.uuid4().hex
        self.url = url
        self.mode = mode
        self.quality = quality
        self.status = "queued"
        self.title: str | None = None
        self.total: int | None = None
        self.current_index: int | None = None
        self.current_title: str | None = None
        self.current_percent: float | None = None
        self.completed = 0
        self.failed = 0
        self.errors: list[str] = []
        self.message: str | None = None
        self.created = time.time()
        self.cancel_requested = False
        self.stop_reason: str | None = None
        self.lock = threading.Lock()

    @property
    def dir(self) -> Path:
        return DOWNLOAD_DIR / self.id

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "url": self.url,
                "mode": self.mode,
                "quality": self.quality,
                "status": self.status,
                "title": self.title,
                "total": self.total,
                "current_index": self.current_index,
                "current_title": self.current_title,
                "current_percent": self.current_percent,
                "completed": self.completed,
                "failed": self.failed,
                "errors": self.errors[-20:],
                "message": self.message,
                "created": self.created,
            }

    @classmethod
    def from_dict(cls, data: dict) -> Job:
        job = cls(data["url"], data["mode"], data["quality"], job_id=data["id"])
        for key in ("status", "title", "total", "completed", "failed", "message", "created"):
            setattr(job, key, data.get(key, getattr(job, key)))
        job.errors = data.get("errors", [])
        return job


def save_state() -> None:
    with jobs_lock:
        current = list(jobs.values())
    data = [job.to_dict() for job in current]
    with state_lock:
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(STATE_FILE)


def check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(400, "Bitte eine vollständige http(s)-Adresse angeben.")
    if ALLOW_PRIVATE_URLS:
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise HTTPException(400, f"Die Adresse {parsed.hostname} konnte nicht aufgelöst werden.") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise HTTPException(400, "Adressen im lokalen Netz sind nicht erlaubt.")


class JobLogger:
    """Leitet die Meldungen von yt-dlp in den Job um."""

    def __init__(self, job: Job):
        self.job = job

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        with self.job.lock:
            self.job.failed += 1
            self.job.errors.append(msg)
            del self.job.errors[:-MAX_LOGGED_ERRORS]


def build_options(job: Job) -> dict:
    def before_entry(info: dict, *, incomplete: bool) -> None:
        # Wird vor jedem Eintrag aufgerufen und erlaubt sauberes Abbrechen
        # zwischen zwei Videos einer Playlist.
        if job.cancel_requested:
            raise DownloadCancelled("Abgebrochen")
        if shutil.disk_usage(DOWNLOAD_DIR).free < MIN_FREE_GB * 1024**3:
            job.stop_reason = f"Gestoppt: weniger als {MIN_FREE_GB:g} GB freier Speicher auf dem NAS"
            raise DownloadCancelled(job.stop_reason)
        return None

    def progress(d: dict) -> None:
        if job.cancel_requested:
            raise DownloadCancelled("Abgebrochen")
        info = d.get("info_dict") or {}
        with job.lock:
            job.current_title = info.get("title")
            job.current_index = info.get("playlist_index") or info.get("playlist_autonumber")
            total = info.get("n_entries") or info.get("playlist_count")
            if total:
                job.total = total
            if info.get("playlist_title") and not job.title:
                job.title = info["playlist_title"]
            if d["status"] == "downloading":
                done = d.get("downloaded_bytes") or 0
                size = d.get("total_bytes") or d.get("total_bytes_estimate")
                job.current_percent = round(done / size * 100, 1) if size else None

    def postprocessed(d: dict) -> None:
        # "MoveFiles" ist der letzte Schritt pro Video, danach liegt die Datei fertig da.
        if d["status"] == "finished" and d.get("postprocessor") == "MoveFiles":
            with job.lock:
                job.completed += 1
                job.current_percent = None

    outtmpl = "%(playlist_index&{} - |)s%(title).150B [%(id)s].%(ext)s"
    opts: dict = {
        "paths": {"home": str(job.dir), "temp": str(job.dir / ".tmp")},
        "outtmpl": outtmpl,
        "windowsfilenames": True,
        "ignoreerrors": True,
        "lazy_playlist": True,
        "download_archive": str(job.dir / ARCHIVE_NAME),
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "file_access_retries": 5,
        "concurrent_fragment_downloads": 4,
        "match_filter": before_entry,
        "progress_hooks": [progress],
        "postprocessor_hooks": [postprocessed],
        "logger": JobLogger(job),
        "noprogress": True,
    }
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    if job.mode == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
        ]
    else:
        opts["format"] = QUALITY_FORMATS[job.quality]
        opts["merge_output_format"] = "mp4"
    return opts


def run_job(job: Job) -> None:
    with job.lock:
        if job.cancel_requested:
            job.status = "cancelled"
            return
        job.status = "running"
        job.message = None

    stop_saving = threading.Event()

    def save_periodically() -> None:
        while not stop_saving.wait(5):
            save_state()

    threading.Thread(target=save_periodically, daemon=True).start()
    try:
        job.dir.mkdir(parents=True, exist_ok=True)
        archive = job.dir / ARCHIVE_NAME
        if archive.exists():
            # Das Archiv ist die verlaessliche Quelle dafuer, was schon fertig ist,
            # auch wenn der Server waehrend eines Downloads beendet wurde.
            with archive.open() as f, job.lock:
                job.completed = sum(1 for line in f if line.strip())
        save_state()
        with yt_dlp.YoutubeDL(build_options(job)) as ydl:
            ydl.download([job.url])
        with job.lock:
            if job.completed == 0 and job.failed > 0:
                job.status = "error"
                job.message = "Keine Datei konnte heruntergeladen werden."
            else:
                job.status = "done"
                if job.failed:
                    job.message = f"{job.failed} Eintrag/Einträge fehlgeschlagen."
    except DownloadCancelled:
        with job.lock:
            job.status = "cancelled"
            job.message = job.stop_reason or "Abgebrochen"
    except Exception as exc:  # noqa: BLE001 - jede Ausnahme soll im Job sichtbar sein
        with job.lock:
            job.status = "error"
            job.message = str(exc)
    finally:
        stop_saving.set()
        with job.lock:
            job.current_percent = None
            job.current_title = None
        save_state()


def submit(job: Job) -> None:
    with job.lock:
        job.status = "queued"
        job.cancel_requested = False
        job.stop_reason = None
        job.message = None
    executor.submit(run_job, job)


def get_job(job_id: str) -> Job:
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job nicht gefunden")
    return job


def list_files(job: Job) -> list[Path]:
    if not job.dir.is_dir():
        return []
    files = (
        p
        for p in job.dir.iterdir()
        if p.is_file() and not p.name.startswith(".") and not p.name.endswith(HIDDEN_SUFFIXES)
    )
    # Natuerliche Sortierung, damit "2 - ..." vor "10 - ..." kommt (Reihenfolge der Playlist).
    return sorted(
        files, key=lambda p: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]
    )


def load_state() -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return
    for data in json.loads(STATE_FILE.read_text()):
        job = Job.from_dict(data)
        jobs[job.id] = job
        # Jobs, die beim Herunterfahren noch liefen, werden fortgesetzt.
        # Das Download-Archiv sorgt dafuer, dass Fertiges uebersprungen wird.
        if job.status in ("queued", "running"):
            submit(job)


@app.post("/api/jobs")
def create_job(req: JobRequest) -> dict:
    url = req.url.strip()
    check_url(url)
    job = Job(url, req.mode, req.quality)
    with jobs_lock:
        jobs[job.id] = job
    submit(job)
    save_state()
    return job.to_dict()


@app.get("/api/jobs")
def all_jobs() -> list[dict]:
    with jobs_lock:
        current = list(jobs.values())
    return sorted((j.to_dict() for j in current), key=lambda j: j["created"], reverse=True)


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str) -> dict:
    return get_job(job_id).to_dict()


@app.get("/api/jobs/{job_id}/files")
def job_files(job_id: str, offset: int = 0, limit: int = 200) -> dict:
    # Seitenweise, damit auch Playlists mit zehntausenden Dateien die Oberflaeche nicht ausbremsen.
    files = list_files(get_job(job_id))
    page = files[max(offset, 0) : max(offset, 0) + min(max(limit, 1), 1000)]
    return {"total": len(files), "files": [{"name": p.name, "size": p.stat().st_size} for p in page]}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    job = get_job(job_id)
    with job.lock:
        job.cancel_requested = True
    return job.to_dict()


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str) -> dict:
    job = get_job(job_id)
    if job.status in ("queued", "running"):
        raise HTTPException(409, "Job läuft bereits")
    with job.lock:
        job.failed = 0
        job.errors = []
    submit(job)
    save_state()
    return job.to_dict()


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, delete_files: bool = False) -> dict:
    job = get_job(job_id)
    if job.status in ("queued", "running"):
        raise HTTPException(409, "Bitte den Job zuerst abbrechen")
    with jobs_lock:
        jobs.pop(job_id, None)
    if delete_files:
        shutil.rmtree(job.dir, ignore_errors=True)
    save_state()
    return {"deleted": job_id}


@app.get("/api/jobs/{job_id}/files/{name}")
def download_file(job_id: str, name: str) -> FileResponse:
    job = get_job(job_id)
    path = (job.dir / name).resolve()
    if (
        path.parent != job.dir.resolve()
        or not path.is_file()
        or path.name.startswith(".")
        or path.name.endswith(HIDDEN_SUFFIXES)
    ):
        raise HTTPException(404, "Datei nicht gefunden")
    return FileResponse(path, filename=path.name)


@app.get("/api/jobs/{job_id}/zip")
def download_zip(job_id: str) -> StreamingResponse:
    job = get_job(job_id)
    files = list_files(job)
    if not files:
        raise HTTPException(404, "Noch keine fertigen Dateien")
    # Videos sind bereits komprimiert, darum ohne Kompression (ZIP_STORED).
    # ZipStream nutzt bei Bedarf ZIP64, damit sind auch Archive > 4 GB moeglich.
    zs = ZipStream(sized=True)
    for path in files:
        zs.add_path(str(path), path.name)
    name = "".join(c for c in (job.title or job.id) if c.isalnum() or c in " -_").strip() or job.id
    ascii_name = name.encode("ascii", "ignore").decode().strip() or job.id
    return StreamingResponse(
        zs,
        media_type="application/zip",
        headers={
            "Content-Length": str(len(zs)),
            "Content-Disposition": (
                f"attachment; filename=\"{ascii_name}.zip\"; filename*=UTF-8''{quote(name + '.zip')}"
            ),
        },
    )


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
