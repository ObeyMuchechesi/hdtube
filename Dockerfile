# syntax=docker/dockerfile:1

# ffmpeg is installed system-wide here, so downloader.py picks it up from PATH
# instead of relying on the pip wheel.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py downloader.py ./
COPY ui ./ui

ENV PORT=7788
EXPOSE 7788

CMD ["python", "app.py"]
