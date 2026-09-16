import time

import pytest

from app.store import Store


@pytest.fixture()
def store(tmp_path):
    return Store(str(tmp_path / "validator.db"))


def test_schema_and_wal(store):
    assert store.get_kv("schema_version") == "1"
    assert store.query_one("PRAGMA journal_mode")[0].lower() == "wal"


def test_kv_round_trip(store):
    store.set_json("tools", ["get_version"])
    assert store.get_json("tools") == ["get_version"]
    store.delete_kv("tools")
    assert store.get_json("tools", []) == []


def test_secret_key_is_stable(store):
    first = store.secret_key()
    assert first == store.secret_key()
    assert len(first) == 32


def test_lease_blocks_a_second_holder(store):
    now = time.time()
    assert store.acquire_lease("engine", "a", now=now)
    assert not store.acquire_lease("engine", "b", now=now + 5)
    assert store.acquire_lease("engine", "a", now=now + 5)  # heartbeat
    assert store.acquire_lease("engine", "b", now=now + 60)  # stale lease taken over


def test_outbox_dedupe_key_is_unique(store):
    store.queue_message("s1:ENTER", "42", "hello")
    store.queue_message("s1:ENTER", "42", "hello again")
    pending = store.pending_messages()
    assert len(pending) == 1
    assert pending[0]["text"] == "hello"
    store.mark_sent(pending[0]["id"])
    assert store.pending_messages() == []


def test_transaction_rolls_back(store):
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            store.enqueue(conn, "x", "1", "text", time.time())
            raise RuntimeError("boom")
    assert store.pending_messages() == []
