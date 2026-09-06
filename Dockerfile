FROM ghcr.io/bartolomeorusso9/spotiflac:latest

WORKDIR /app/bot

RUN pip install --no-cache-dir telethon

COPY bot.py /app/bot/bot.py
COPY bot-entrypoint.sh /app/bot/bot-entrypoint.sh

RUN chmod +x /app/bot/bot-entrypoint.sh

RUN mkdir -p /app/downloads

ENTRYPOINT ["/app/bot/bot-entrypoint.sh"]
