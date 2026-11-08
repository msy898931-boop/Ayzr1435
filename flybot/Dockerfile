FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY bot.py .
RUN mkdir -p /app/data

# bot.py keeps its SQLite journal and the allow-list under ./data, which is a
# Fly volume, so the journal survives every redeploy.
CMD ["python3", "bot.py"]
