FROM python:3.11-slim

# Install ffmpeg/ffprobe + CJK fonts + Pillow JPEG support.
# Combining ffmpeg here keeps the container self-contained — no host
# bind-mounts required. CJK fonts are needed so Chinese/Japanese/Korean
# characters render correctly in the header / timestamp labels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
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
ENV BROWSE_ROOT=/media
ENV MEDIA_ROOT=/media
ENV DEFAULT_COUNT=16
ENV DEFAULT_COLS=4
ENV DEFAULT_WIDTH=1920
ENV DEFAULT_THUMB_W=640
ENV JPEG_QUALITY=95
ENV DEFAULT_LABEL_LANG=en

EXPOSE 8800
CMD ["python", "-u", "server.py"]
