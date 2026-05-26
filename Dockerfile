FROM python:3.11-slim

WORKDIR /app

ARG BUILD_ID=dev
ENV BUILD_ID=$BUILD_ID
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt ./requirements.txt

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]