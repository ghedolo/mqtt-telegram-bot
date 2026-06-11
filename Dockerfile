FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ bot/
COPY sensors.yaml .

RUN groupadd -r -g 999 bot && useradd -r -u 999 -g bot -d /app -s /usr/sbin/nologin bot \
    && mkdir -p data tmp \
    && chown -R bot:bot /app

USER bot

CMD ["python", "-m", "bot.main"]
