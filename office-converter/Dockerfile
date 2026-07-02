FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-impress \
    libreoffice-writer \
    libreoffice-calc \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages \
    fastapi uvicorn

RUN useradd -o -u 1000 -m converter
RUN mkdir -p /data/input /data/output /vault-anlagen && \
    chown -R converter:converter /data /vault-anlagen

COPY src/ /app/
WORKDIR /app
USER converter:converter

CMD ["python3", "api.py"]
