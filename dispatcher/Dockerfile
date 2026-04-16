FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir watchdog requests pyyaml json-repair langdetect
COPY dispatcher.py analyze_classifications.py ./
USER 1000:1000
CMD ["python", "-u", "dispatcher.py"]
