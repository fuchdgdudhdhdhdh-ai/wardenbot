FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BOT_MODE=webhook \
    DATABASE_PATH=/var/data/warden.db

WORKDIR /app

RUN addgroup --system bot && adduser --system --ingroup bot bot

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /var/data && chown -R bot:bot /app /var/data

USER bot

EXPOSE 10000

CMD ["python", "bot.py"]
