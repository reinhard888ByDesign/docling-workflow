FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir watchdog requests pyyaml json-repair langdetect lxml img2pdf paramiko
RUN useradd -u 1000 -m user
COPY dispatcher.py analyze_classifications.py ./
USER 1000:1000
CMD ["python", "-u", "dispatcher.py"]
