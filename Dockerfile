FROM python:3.13-slim

WORKDIR /app

# Install system deps for psycopg binary
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Don't copy local .env — secrets come from Fly environment
ENV PYTHONUNBUFFERED=1

EXPOSE 8080
