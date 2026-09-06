FROM ghcr.io/bartolomeorusso9/spotiflac:latest

USER root

WORKDIR /app/bot

RUN pip install --no-cache-dir telethon

COPY bot.py /app/bot/bot.py
COPY bot-entrypoint.sh /app/bot/bot-entrypoint.sh

RUN chmod 755 /app/bot/bot-entrypoint.sh
RUN chown -R spotiflac:spotiflac /app/bot

RUN mkdir -p /app/downloads
RUN chown -R spotiflac:spotiflac /app/downloads

USER spotiflac

ENTRYPOINT ["/app/bot/bot-entrypoint.sh"]
