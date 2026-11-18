import hashlib
import hmac
import json
import logging
import sqlite3
import time
from contextlib import closing
from threading import Event
from urllib.request import Request, urlopen

from maf.events.store import EventStore


logger = logging.getLogger(__name__)


def signature(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def deliver_events(database, url: str, secret: str, stop: Event):
    """Retry independently of run execution and persist delivery receipts."""
    with closing(sqlite3.connect(database)) as connection:
        store = EventStore(connection)
        while not stop.is_set():
            try:
                rows = connection.execute(
                    "SELECT event_id, attempts FROM outbox WHERE delivered = 0 "
                    "AND next_retry_at <= ? ORDER BY event_id LIMIT 10",
                    (time.time(),),
                ).fetchall()
                for event_id, attempts in rows:
                    if stop.is_set():
                        break
                    payload = json.dumps(store.get(event_id), ensure_ascii=False, separators=(",", ":"))
                    body = json.dumps({"payload": payload}).encode()
                    request = Request(url, data=body, headers={
                        "Content-Type": "application/json", "X-Event-Id": str(event_id),
                        "X-Signature": signature(secret, payload),
                    })
                    try:
                        with urlopen(request, timeout=15):
                            pass
                        if not store.delivered(event_id):
                            raise RuntimeError("n8n returned without a delivery receipt")
                    except (OSError, RuntimeError):
                        with connection:
                            connection.execute(
                                "UPDATE outbox SET attempts = ?, next_retry_at = ? WHERE event_id = ?",
                                (attempts + 1, time.time() + min(300, 5 * 2 ** min(attempts, 6)), event_id),
                            )
                        logger.warning("n8n delivery pending for event %s", event_id)
            except Exception:
                logger.exception("n8n outbox iteration failed")
            stop.wait(2)
