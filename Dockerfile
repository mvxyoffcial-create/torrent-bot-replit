FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/usr/lib/python3/dist-packages

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-libtorrent \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
RUN mkdir -p /data/seeds

EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn bot:app --host 0.0.0.0 --port ${PORT:-8000}"]