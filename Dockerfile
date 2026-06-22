FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cache bust: aggiorna questo per forzare ricopia dei file
ARG CACHEBUST=20260622_2
COPY telegram_bot/ ./telegram_bot/

CMD ["python3", "telegram_bot/bot.py"]
