# One image, two roles — docker-compose.yml runs it once as the bot loop
# and once as the API server via different `command:` overrides.
FROM python:3.11-slim

WORKDIR /app

# System deps for building 'cryptography' if a wheel isn't available for
# this platform; harmless no-op if wheels cover it.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

# Overridden per-service in docker-compose.yml
CMD ["python3", "bot.py"]
