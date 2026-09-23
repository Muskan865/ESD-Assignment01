"""One-line JSON logging, shaped for Elastic Common Schema (ECS).

Everything is written to stdout as a single JSON object per line. The Docker
daemon captures stdout into the container's log file, Filebeat reads that file
and `decode_json_fields` turns each line back into real Elasticsearch fields.
That round trip is why the JSON must be one line with no embedded newlines.
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
    # The payload is already JSON; the formatter must not wrap it in anything.
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "warning": 30, "error": 40}


def log_event(message: str, *, level: str = "info", **fields) -> None:
    """Emit one ECS-shaped JSON log line.

    Never pass card numbers, tokens, emails or any other secret/personal data
    in `fields` — these lines land in Elasticsearch unencrypted.
    """
    payload = {
        "@timestamp": datetime.now(timezone.utc).isoformat(),
        "service": {"name": SERVICE_NAME, "node": {"name": INSTANCE}},
        "log": {"level": level.lower()},
        "message": message,
    }
    payload.update(fields)
    _logger.log(_LEVELS.get(level.lower(), 20), json.dumps(payload, separators=(",", ":")))
