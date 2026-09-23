FROM python:3.11-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY app ./app

EXPOSE 8000

# Single worker on purpose: prometheus_client keeps its registry in-process, so
# N workers would expose N partial views of every counter.
# --no-access-log: our JSON middleware already emits exactly one line per
# request. Leaving uvicorn's plain-text access log on would double every
# event in Elasticsearch and add lines that decode_json_fields cannot parse.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
