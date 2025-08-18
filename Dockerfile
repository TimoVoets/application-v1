FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y tesseract-ocr poppler-utils libzbar0 \
    libglib2.0-0 libsm6 libxext6 libxrender1 libgl1 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["gunicorn", "-w", "1", "-k", "uvicorn.workers.UvicornWorker", "app:app", "-b", "0.0.0.0:8000", "--timeout", "180"]

