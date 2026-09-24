FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libass9 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY web ./web

RUN python -m pip install --upgrade pip \
    && python -m pip install .

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "src.handler.app:app", "--host", "0.0.0.0", "--port", "8000"]
