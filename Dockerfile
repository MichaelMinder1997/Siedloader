FROM python:3.12-slim

# ffmpeg: Zusammenfuegen von Video und Ton, MP3-Umwandlung
# deno: JavaScript-Laufzeit, die yt-dlp fuer YouTube benoetigt
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip \
    && curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y \
    && apt-get purge -y curl unzip && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV DOWNLOAD_DIR=/downloads
VOLUME /downloads
EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
