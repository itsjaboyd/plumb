FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MIGRATIONS_DIR=/app/migrations \
    MAPPINGS_DIR=/app/mappings

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY plumb ./plumb
COPY tests ./tests
RUN pip install --no-cache-dir -e .

# Default command prints the help, so `docker compose run --rm etl` is safe.
ENTRYPOINT []
CMD ["plumb", "--help"]
