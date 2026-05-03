FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir watchdog requests pyyaml json-repair langdetect lxml img2pdf paramiko html2text pdfminer.six
RUN useradd -u 1000 -m user
COPY dispatcher.py analyze_classifications.py \
     enex_parser.py enml_to_markdown.py enex_tag_mapper.py \
     enex_processor.py enex_ocr_worker.py ./
USER 1000:1000
CMD ["python", "-u", "dispatcher.py"]
