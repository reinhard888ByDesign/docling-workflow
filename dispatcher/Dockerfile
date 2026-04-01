FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir watchdog requests pyyaml json-repair
COPY dispatcher.py .
CMD ["python", "-u", "dispatcher.py"]
