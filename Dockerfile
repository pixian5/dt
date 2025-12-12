FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY crawler ./crawler
COPY urls.txt README.md ./ 
COPY data ./data

CMD ["python", "-m", "crawler.main", "--url-file", "urls.txt"]
