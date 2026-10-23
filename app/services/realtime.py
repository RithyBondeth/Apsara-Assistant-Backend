"""Telling an open inbox that something changed, the moment it does.

The web app used to poll every few seconds, so a customer's message took up
to a poll to appear and every open tab cost a stream of requests. Now:

1. Triggers on `messages`, `conversations` and `orders` call pg_notify on
   the `apsara_events` channel. Postgres delivers a NOTIFY only when its
   transaction commits — so a rolled-back write never announces itself — and
   to every listening connection, so a reply sent by the worker process
   reaches a browser connected to the API process.
2. Each API process holds one LISTEN connection on a thread (`start`), and
   hands each event to the open streams of the shop it belongs to.
3. GET /api/v1/events is a Server-Sent Events stream; the browser refetches
   whatever an event names.

Events carry ids only, never content: NOTIFY payloads are capped at 8000
bytes, and the client fetches through the ordinary, authorised endpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
import select
import threading
from collections import defaultdict

from sqlalchemy import DDL, event

from app.database import engine

logger = logging.getLogger(__name__)

CHANNEL = "apsara_events"
HEARTBEAT_SECONDS = 15
# Events a slow stream may fall behind by before it is told to resync.
QUEUE_LIMIT = 200

NOTIFY_FUNCTION = f"""
CREATE OR REPLACE FUNCTION notify_apsara_event() RETURNS trigger AS $$
DECLARE
    owner uuid;
    conversation uuid;
BEGIN
    IF TG_TABLE_NAME = 'messages' THEN
        SELECT user_id INTO owner FROM conversations WHERE id = NEW.conversation_id;
        conversation := NEW.conversation_id;
    ELSIF TG_TABLE_NAME = 'conversations' THEN
        owner := NEW.user_id;
        conversation := NEW.id;
    ELSE
        owner := NEW.user_id;
        conversation := NEW.conversation_id;
    END IF;
    IF owner IS NOT NULL THEN
        PERFORM pg_notify('{CHANNEL}', json_build_object(
            'user_id', owner,
            'type', TG_ARGV[0],
            'id', NEW.id,
            'conversation_id', conversation
        )::text);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

# (table, event type, trigger timing). Conversations and orders change often
# in ways nobody is watching (updated_at alone); the WHEN clause keeps those
# quiet. Kept in one place for the models and the migration.
TRIGGERS = {
    "messages": ("message", "AFTER INSERT", None),
    "conversations": ("conversation", "AFTER INSERT OR UPDATE", (
        "OLD IS NULL OR OLD.status IS DISTINCT FROM NEW.status "
        "OR OLD.handling_mode IS DISTINCT FROM NEW.handling_mode "
        "OR OLD.needs_attention_at IS DISTINCT FROM NEW.needs_attention_at "
        "OR OLD.unread_count IS DISTINCT FROM NEW.unread_count "
        "OR OLD.assigned_user_id IS DISTINCT FROM NEW.assigned_user_id")),
    "orders": ("order", "AFTER INSERT OR UPDATE", (
        "OLD IS NULL OR OLD.status IS DISTINCT FROM NEW.status "
        "OR OLD.payment_status IS DISTINCT FROM NEW.payment_status "
        "OR OLD.khqr_status IS DISTINCT FROM NEW.khqr_status")),
}


def trigger_sql(table: str) -> list[str]:
    kind, timing, when = TRIGGERS[table]
    # A WHEN clause may not mention OLD on INSERT, so the two are split.
    if when is None:
        return [f"CREATE TRIGGER {table}_notify {timing} ON {table} FOR EACH ROW "
                f"EXECUTE FUNCTION notify_apsara_event('{kind}')"]
    condition = when.replace("OLD IS NULL OR ", "")
    return [
        f"CREATE TRIGGER {table}_notify_insert AFTER INSERT ON {table} FOR EACH ROW "
        f"EXECUTE FUNCTION notify_apsara_event('{kind}')",
        f"CREATE TRIGGER {table}_notify_update AFTER UPDATE ON {table} FOR EACH ROW "
        f"WHEN ({condition}) EXECUTE FUNCTION notify_apsara_event('{kind}')",
    ]


def drop_sql(table: str) -> list[str]:
    return [f"DROP TRIGGER IF EXISTS {table}_notify ON {table}",
            f"DROP TRIGGER IF EXISTS {table}_notify_insert ON {table}",
            f"DROP TRIGGER IF EXISTS {table}_notify_update ON {table}"]


def notify_on_change(table) -> None:
    """Attach the trigger whenever the table is created, as with create_all
    in tests and on a fresh database."""
    event.listen(table, "after_create", DDL(NOTIFY_FUNCTION))
    for statement in trigger_sql(table.name):
        event.listen(table, "after_create", DDL(statement))


# ── Fan-out inside one process ───────────────────────────────────────────────

class Hub:
    """Open streams, by the shop whose events they receive."""

    def __init__(self):
        self._lock = threading.Lock()
        self._streams: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = \
            defaultdict(set)

    def subscribe(self, user_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        with self._lock:
            self._streams[user_id].add((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, user_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            streams = self._streams.get(user_id, set())
            streams.difference_update({entry for entry in streams if entry[1] is queue})
            if not streams:
                self._streams.pop(user_id, None)

    def publish(self, payload: dict) -> None:
        """Hand an event to every stream of its shop. Safe from any thread."""
        with self._lock:
            targets = list(self._streams.get(str(payload.get("user_id")), ()))
        for loop, queue in targets:
            loop.call_soon_threadsafe(_offer, queue, payload)

    def users(self) -> list[str]:
        with self._lock:
            return list(self._streams)

    def count(self) -> int:
        with self._lock:
            return sum(len(streams) for streams in self._streams.values())


def _offer(queue: asyncio.Queue, payload: dict) -> None:
    try:
        queue.put_nowait(payload)
    except asyncio.QueueFull:
        # A stream this far behind is better off refetching everything than
        # replaying 200 ids.
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait({"type": "resync"})


hub = Hub()


# ── The LISTEN connection ────────────────────────────────────────────────────

_stop = threading.Event()
_listener: threading.Thread | None = None


def _listen(stop: threading.Event) -> None:
    backoff = 1.0
    while not stop.is_set():
        try:
            # A raw DBAPI connection of its own: LISTEN needs autocommit and a
            # connection that stays out of the pool for the process lifetime.
            connection = engine.raw_connection()
            dbapi = connection.dbapi_connection
            dbapi.autocommit = True
            with dbapi.cursor() as cursor:
                cursor.execute(f"LISTEN {CHANNEL}")
            backoff = 1.0
            logger.info("Listening for inbox events")
            # Anything missed while disconnected: tell every stream to refetch.
            for user_id in hub.users():
                hub.publish({"user_id": user_id, "type": "resync"})
            while not stop.is_set():
                if select.select([dbapi], [], [], 1.0) == ([], [], []):
                    continue
                dbapi.poll()
                while dbapi.notifies:
                    note = dbapi.notifies.pop(0)
                    try:
                        hub.publish(json.loads(note.payload))
                    except ValueError:
                        logger.warning("Unreadable inbox event: %r", note.payload[:200])
            connection.invalidate()
        except Exception:
            logger.exception("Inbox event listener lost its connection")
            stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)


def start() -> None:
    global _listener
    if _listener is not None and _listener.is_alive():
        return
    _stop.clear()
    _listener = threading.Thread(target=_listen, args=(_stop,), name="inbox-events", daemon=True)
    _listener.start()


def stop(timeout: float = 5.0) -> None:
    global _listener
    if _listener is None:
        return
    _stop.set()
    _listener.join(timeout)
    _listener = None


def sse(payload: dict) -> str:
    data = {key: (str(value) if value is not None else None)
            for key, value in payload.items() if key != "user_id"}
    return f"event: {payload.get('type', 'message')}\ndata: {json.dumps(data)}\n\n"
