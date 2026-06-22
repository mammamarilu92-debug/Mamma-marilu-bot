FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV MALLOC_TRIM_THRESHOLD_=100000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cache bust: aggiorna questo per forzare ricopia dei file
ARG CACHEBUST=20260622_4
COPY telegram_bot/ ./telegram_bot/

CMD ["python3", "telegram_bot/bot.py"]
