FROM python:3.11-slim

# Dipendenze ffmpeg + Chromium (per playwright)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
    libcairo2 libx11-6 libx11-xcb1 libxcb1 libxext6 libxcursor1 \
    libxi6 libxtst6 libglib2.0-0 libexpat1 wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Installa solo il browser Chromium (le dipendenze di sistema sono già sopra)
RUN playwright install chromium

COPY telegram_bot/ ./telegram_bot/

CMD ["python3", "telegram_bot/bot.py"]
