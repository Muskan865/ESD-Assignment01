"""One-line JSON logging, shaped for Elastic Common Schema (ECS).

Pipeline this feeds:
    app -> stdout (one JSON object per line)
        -> Docker json-file log driver writes /var/lib/docker/containers/<id>/<id>-json.log
        -> Filebeat tails that file, `decode_json_fields` promotes our keys to real fields
        -> Elasticsearch index tiny-shop-logs-YYYY.MM.dd
        -> Kibana Discover

Two rules the format has to respect:
  1. One line per event, no embedded newlines — Filebeat splits on newlines.
  2. `@timestamp` in ISO-8601 UTC, so Elasticsearch maps it as a date and Kibana
     can use it as the time field.

NEVER put secrets or personal data in `fields`. No card numbers, no tokens, no
email addresses. Logs land in Elasticsearch unencrypted and are searchable by
anyone who can open Kibana.
"""

import json
import logging
import sys
from datetime import datetime, timezone

from .config import INSTANCE, SERVICE_NAME

_logger = logging.getLogger(SERVICE_NAME)
_logger.setLevel(logging.INFO)
_logger.propagate = False
if not _logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "warning": 30, "error": 40}


def log_event(message: str, *, level: str = "info", **fields) -> None:
    """Emit exactly one ECS-shaped JSON log line to stdout."""
    payload = {
        "@timestamp": datetime.now(timezone.utc).isoformat(),
        "service": {"name": SERVICE_NAME, "node": {"name": INSTANCE}},
        "log": {"level": level.lower()},
        "message": message,
    }
    payload.update(fields)
    _logger.log(_LEVELS.get(level.lower(), 20), json.dumps(payload, separators=(",", ":")))
