FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/littlebites.db

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data

EXPOSE 8000
CMD ["sh", "-c", "waitress-serve --listen=0.0.0.0:${PORT:-8000} --call app:create_app"]
