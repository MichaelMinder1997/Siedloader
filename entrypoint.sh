#!/bin/sh
set -e

# Seiten aendern sich laufend. Eine aktuelle yt-dlp-Version ist der wichtigste
# Faktor dafuer, dass Downloads weiter funktionieren.
if [ "${AUTO_UPDATE:-1}" = "1" ]; then
    pip install --no-cache-dir --quiet --upgrade "yt-dlp[default]" || echo "yt-dlp-Update fehlgeschlagen, verwende installierte Version"
fi

exec uvicorn main:app --host 0.0.0.0 --port 8080
