FROM python:3.12-slim

# ostree CLI is needed server-side for summary/delta maintenance jobs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ostree \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/ ./api/
COPY migrations/ ./migrations/

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
