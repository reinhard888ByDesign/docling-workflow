FROM python:3.12-slim

RUN apt-get update && apt-get install -y ripgrep && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir mcp qdrant-client requests

COPY server.py /app/vault-mcp/server.py

# enzyme binary from host (will be mounted or copied at runtime)
# Falls enzyme nicht verfügbar, wird search_vault Tool fehlschlagen

CMD ["mcpo", "--port", "8080", "--", "python3", "/app/vault-mcp/server.py"]
