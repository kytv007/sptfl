FROM ghcr.io/bartolomeorusso9/spotiflac:latest

WORKDIR /app/bot

RUN pip install --no-cache-dir telethon

COPY bot.py /app/bot/bot.py

RUN mkdir -p /app/downloads

ENTRYPOINT ["python3", "/app/bot/bot.py"]
