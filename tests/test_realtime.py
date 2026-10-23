"""Inbox changes pushed to the browser as they commit."""

import asyncio
import json
import select
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.database import engine
from app.main import app
from app.models.conversation import Conversation
from app.services import realtime
from app.services.realtime import start as start_listener, stop as stop_listener
from tests import ai


@pytest.fixture
def listen():
    """A LISTEN connection of the test's own; returns a reader of payloads."""
    connection = engine.raw_connection()
    dbapi = connection.dbapi_connection
    dbapi.autocommit = True
    with dbapi.cursor() as cursor:
        cursor.execute(f"LISTEN {realtime.CHANNEL}")

    def received(wait=0.5):
        deadline = time.monotonic() + wait
        out = []
        while time.monotonic() < deadline:
            if select.select([dbapi], [], [], 0.05) != ([], [], []):
                dbapi.poll()
                while dbapi.notifies:
                    out.append(json.loads(dbapi.notifies.pop(0).payload))
        return out

    received()  # discard anything from setup
    yield received
    connection.invalidate()


def seller_id(client, seller):
    return client.get("/api/v1/auth/me", headers=seller.headers).json()["id"]


def test_a_new_message_is_announced_to_its_shop(client, seller, listen):
    conversation = seller.conversation()
    listen()
    with ai.replies("Hi!"):
        client.post(f"/api/v1/chat/{conversation['id']}",
                    json={"message": "hello", "message_type": "text"}, headers=seller.headers)

    events = [e for e in listen() if e["type"] == "message"]
    assert len(events) == 2, "the customer's message and the reply"
    assert {e["user_id"] for e in events} == {seller_id(client, seller)}
    assert {e["conversation_id"] for e in events} == {conversation["id"]}


def test_a_conversation_change_is_announced_but_a_touch_is_not(client, seller, db, listen):
    conversation = seller.conversation()
    listen()

    row = db.query(Conversation).one()
    row.updated_at = row.updated_at  # nothing anyone watches
    row.last_read_at = None
    db.commit()
    assert listen() == []

    client.patch(f"/api/v1/conversations/{conversation['id']}", json={"status": "closed"},
                 headers=seller.headers)
    [event] = listen()
    assert event["type"] == "conversation" and event["id"] == conversation["id"]


def test_nothing_is_announced_for_a_rolled_back_write(client, seller, db, listen):
    seller.conversation()
    listen()
    row = db.query(Conversation).one()
    row.status = "closed"
    db.flush()
    db.rollback()
    assert listen() == []


def test_an_order_payment_is_announced(client, seller, listen):
    product = seller.product(stock=5)
    customer = seller.customer()
    listen()
    seller.order(customer["id"], [{"product_id": product["id"], "quantity": 1}])
    assert [e["type"] for e in listen()] == ["order"]


# ── The hub ──────────────────────────────────────────────────────────────────

def test_the_hub_delivers_across_threads_to_the_right_shop_only():
    hub = realtime.Hub()

    async def scenario():
        mine, theirs = hub.subscribe("shop-a"), hub.subscribe("shop-b")
        threading.Thread(target=hub.publish,
                         args=({"user_id": "shop-a", "type": "message", "id": "1"},)).start()
        got = await asyncio.wait_for(mine.get(), 2)
        assert got["id"] == "1"
        assert theirs.empty()
        hub.unsubscribe("shop-a", mine)
        hub.unsubscribe("shop-b", theirs)
        assert hub.count() == 0

    asyncio.run(scenario())


def test_a_stream_that_falls_behind_is_told_to_resync():
    hub = realtime.Hub()

    async def scenario():
        queue = hub.subscribe("shop-a")
        for i in range(realtime.QUEUE_LIMIT + 5):
            hub.publish({"user_id": "shop-a", "type": "message", "id": str(i)})
        await asyncio.sleep(0.05)
        assert queue.qsize() == 1 + 4
        assert queue.get_nowait() == {"type": "resync"}

    asyncio.run(scenario())


def test_events_carry_ids_not_the_shop():
    text = realtime.sse({"user_id": "u", "type": "message", "id": 1, "conversation_id": None})
    assert text == 'event: message\ndata: {"type": "message", "id": "1", "conversation_id": null}\n\n'


# ── End to end ───────────────────────────────────────────────────────────────

@pytest.fixture
def live_server():
    """The app under real uvicorn, with the real listener thread."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    start_listener()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    stop_listener()
    thread.join(5)


def test_the_browser_stream_receives_a_message_committed_elsewhere(client, seller, live_server):
    conversation = seller.conversation()
    seen: list[str] = []

    def read_stream():
        with httpx.stream("GET", f"{live_server}/api/v1/events", headers=seller.headers,
                          timeout=10) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            for line in response.iter_lines():
                seen.append(line)
                if line.startswith("data: ") and "event: message" in seen:
                    return

    reader = threading.Thread(target=read_stream, daemon=True)
    reader.start()
    deadline = time.monotonic() + 5
    while "event: ready" not in seen and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "event: ready" in seen

    # Written through the test client — another connection, as the worker
    # process would be.
    with ai.replies("Hi!"):
        client.post(f"/api/v1/chat/{conversation['id']}",
                    json={"message": "hello", "message_type": "text"}, headers=seller.headers)

    reader.join(5)
    assert "event: message" in seen
    data = json.loads(seen[seen.index("event: message") + 1].removeprefix("data: "))
    assert data["conversation_id"] == conversation["id"]


def test_the_stream_needs_a_signed_in_seller(client):
    assert client.get("/api/v1/events").status_code == 401
