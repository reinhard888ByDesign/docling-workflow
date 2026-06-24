FROM quay.io/docling-project/docling-serve:latest
USER root
RUN curl -sL -o /usr/share/tesseract/tessdata/deu.traineddata \
    https://github.com/tesseract-ocr/tessdata/raw/main/deu.traineddata && \
    curl -sL -o /usr/share/tesseract/tessdata/ita.traineddata \
    https://github.com/tesseract-ocr/tessdata/raw/main/ita.traineddata && \
    chmod 644 /usr/share/tesseract/tessdata/*.traineddata && \
    chown root:root /usr/share/tesseract/tessdata/*.traineddata
USER default
