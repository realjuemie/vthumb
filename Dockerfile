FROM python:3.11-slim

# No apt ffmpeg (heavy + slow in this build env).
# We bind-mount the host's ffmpeg/ffprobe into /usr/local/bin at compose time.
# This image only needs: Python deps + Pillow.

RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        fontconfig \
        libjpeg-dev \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY templates/ templates/

# CLI entrypoint
RUN printf '#!/bin/sh\nexec python -u /app/server.py "$@"\n' > /usr/local/bin/thumb \
    && chmod +x /usr/local/bin/thumb

ENV PORT=8800
ENV BROWSE_ROOT=/mnt/media
ENV MEDIA_ROOT=/mnt/media
ENV DEFAULT_COUNT=16
ENV DEFAULT_COLS=4
ENV DEFAULT_WIDTH=1920
ENV DEFAULT_THUMB_W=640
ENV JPEG_QUALITY=95
ENV DEFAULT_LABEL_LANG=en
ENV FFMPEG_BIN=/usr/local/bin/ffmpeg
ENV FFPROBE_BIN=/usr/local/bin/ffprobe

EXPOSE 8800
CMD ["python", "-u", "server.py"]