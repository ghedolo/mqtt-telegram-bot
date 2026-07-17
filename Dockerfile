FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ bot/
# sensors.d/ and credentials.yaml are NOT baked into the image — they are
# bind-mounted at runtime (see docker-compose.yml / docs/backup-config.md), so
# the config can live in a separate encrypted repo outside the build context.

RUN groupadd -r -g 999 bot && useradd -r -u 999 -g bot -d /app -s /usr/sbin/nologin bot \
    && mkdir -p data tmp \
    && chown -R bot:bot /app

USER bot

CMD ["python", "-m", "bot.main"]
