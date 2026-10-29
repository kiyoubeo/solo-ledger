FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SOLOLEDGER_DB_PATH=/app/data/sololedger_prod.db \
    SOLOLEDGER_ARCHIVE_DIR=/app/archive

COPY pyproject.toml README.md ./
COPY sql ./sql
COPY src ./src

RUN pip install --no-cache-dir . && \
    mkdir -p /app/data /app/archive

EXPOSE 8000

CMD ["uvicorn", "sololedger.api:app", "--host", "0.0.0.0", "--port", "8000"]
