FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# bot.py here is NOT a second copy: deploy.sh copies the repo's bot.py into this
# directory right before the build (and .gitignore keeps it out of git), so the
# image always carries the one engine. A committed copy is what used to drift.
COPY bot.py .
RUN mkdir -p /app/data

# bot.py keeps its SQLite journal and the allow-list under ./data, which is a
# Fly volume, so the journal survives every redeploy.
CMD ["python3", "bot.py"]
