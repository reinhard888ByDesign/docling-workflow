FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir watchdog requests
COPY watcher.py .
CMD ["python", "-u", "watcher.py"]
