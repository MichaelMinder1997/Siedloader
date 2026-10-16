# Siedloader

Downloader für Videos und Playlists auf Basis von [yt-dlp](https://github.com/yt-dlp/yt-dlp). Er läuft als Docker-Container auf dem NAS.

## Funktionen

- **Playlists in beliebiger Grösse.** Die Einträge werden fortlaufend abgearbeitet, der Download startet also auch bei Playlists mit tausenden Videos sofort.
- **Ein Fehler stoppt nicht die ganze Playlist.** Nicht verfügbare oder gelöschte Videos werden übersprungen und unter „Fehlermeldungen“ aufgelistet.
- **Fortsetzen.** Jeder Job führt ein Archiv der fertigen Videos. Nach einem Abbruch, Netzwerkfehler oder Neustart des NAS geht es dort weiter, wo der Download aufgehört hat. Laufende Jobs werden beim Start des Containers automatisch fortgesetzt.
- **Speicherschutz.** Der Download stoppt, sobald weniger als `MIN_FREE_GB` frei sind.
- **ZIP-Download.** Alle fertigen Dateien eines Jobs lassen sich als ein ZIP herunterladen. Das ZIP wird direkt gestreamt und nicht zwischengespeichert, deshalb gibt es auch hier keine Grössengrenze (ZIP64).
- **Video oder nur Audio (MP3)**, mit wählbarer maximaler Auflösung.
- **Unterstützte Seiten:** alle Seiten, die yt-dlp unterstützt (über 1'800, z. B. YouTube, Vimeo, Dailymotion, SoundCloud, Twitch, ARD/ZDF/SRF). Bei unbekannten Seiten sucht yt-dlp nach direkt eingebetteten Videos und Streams.
- **Automatisches Update von yt-dlp** bei jedem Start des Containers.

Kopiergeschützte Inhalte (DRM, z. B. Netflix, Disney+, Spotify) werden nicht unterstützt.

## Installation auf dem NAS

1. Ordner auf das NAS kopieren.
2. In `docker-compose.yml` das `APP_PASSWORD` ändern.
3. Starten:

   ```sh
   docker compose up -d --build
   ```

4. Im Browser `http://<NAS-IP>:8080` öffnen. Für `donloader.mindernas.com` den Reverse-Proxy des NAS auf Port 8080 zeigen lassen.

Die Downloads landen im Ordner `downloads/` neben der `docker-compose.yml`. Jeder Job erhält dort einen eigenen Unterordner.

## Einstellungen

| Variable | Standard | Bedeutung |
| --- | --- | --- |
| `APP_USER` / `APP_PASSWORD` | `admin` / leer | Anmeldung für Oberfläche und API. Ohne Passwort ist die Seite offen. |
| `MAX_PARALLEL_JOBS` | `2` | Wie viele Jobs gleichzeitig laufen. Weitere warten in der Warteschlange. |
| `MIN_FREE_GB` | `2` | Mindestens freier Speicher. Darunter stoppt der Download. |
| `AUTO_UPDATE` | `1` | yt-dlp beim Start aktualisieren. |
| `COOKIES_FILE` | leer | Pfad zu einer `cookies.txt` (Netscape-Format) für Seiten, die eine Anmeldung verlangen, z. B. private Playlists. |
| `ALLOW_PRIVATE_URLS` | `0` | Auf `1` setzen, um Adressen im lokalen Netz zu erlauben. Standardmässig gesperrt, damit über die Seite keine Geräte im Heimnetz abgefragt werden können. |

## API

| Methode | Pfad | Zweck |
| --- | --- | --- |
| `POST` | `/api/jobs` | Neuer Job: `{"url": "...", "mode": "video"\|"audio", "quality": "best"\|"1080"\|"720"\|"480"}` |
| `GET` | `/api/jobs` | Alle Jobs |
| `GET` | `/api/jobs/{id}` | Status eines Jobs |
| `GET` | `/api/jobs/{id}/files?offset=0&limit=200` | Fertige Dateien, seitenweise |
| `POST` | `/api/jobs/{id}/cancel` | Abbrechen |
| `POST` | `/api/jobs/{id}/resume` | Fortsetzen oder erneut versuchen |
| `DELETE` | `/api/jobs/{id}?delete_files=true` | Job löschen, optional mit Dateien |
| `GET` | `/api/jobs/{id}/files/{name}` | Einzelne Datei herunterladen |
| `GET` | `/api/jobs/{id}/zip` | Alle Dateien als ZIP |

## Lokal starten (ohne Docker)

```sh
pip install -r requirements.txt
cd app
DOWNLOAD_DIR=../downloads uvicorn main:app --port 8080
```

`ffmpeg` muss installiert sein.

## Tests

```sh
pip install -r requirements-dev.txt
ruff format --check . && ruff check .
pytest
```

Die Tests starten einen lokalen Webserver mit einer RSS-Playlist und laden diese wirklich herunter, inklusive Abbruch, Fortsetzen und Neustart. Dafür muss `ffmpeg` installiert sein.
