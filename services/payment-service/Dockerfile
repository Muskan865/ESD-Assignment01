FROM python:3.11-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY app ./app

EXPOSE 8001

# Single worker: the in-process Prometheus registry is per-process, so multiple
# uvicorn workers would each expose their own partial counters.
# --no-access-log: see the storefront Dockerfile for why.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1", "--no-access-log"]
