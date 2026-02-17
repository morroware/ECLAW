"""Queue manager behavior tests."""

import os

import pytest

import app.database as db_module
from app.config import settings
from app.database import close_db
from app.game.queue_manager import QueueManager


@pytest.mark.anyio
async def test_positions_are_resequenced_after_leave_and_complete(tmp_path):
    db_path = str(tmp_path / "queue.db")
    os.environ["DATABASE_PATH"] = db_path
    settings.database_path = db_path
    db_module._db = None
    db_module._db_lock = None
    db_module._write_lock = None

    qm = QueueManager()

    a = await qm.join("A", "a@test.com", "127.0.0.1")
    b = await qm.join("B", "b@test.com", "127.0.0.1")
    c = await qm.join("C", "c@test.com", "127.0.0.1")

    queue = await qm.list_queue()
    assert [e["position"] for e in queue] == [1, 2, 3]

    left = await qm.leave(db_module.hash_token(b["token"]))
    assert left is True

    queue = await qm.list_queue()
    assert [e["name"] for e in queue] == ["A", "C"]
    assert [e["position"] for e in queue] == [1, 2]

    await qm.complete_entry(a["id"], "loss", 1)
    queue = await qm.list_queue()
    assert [e["name"] for e in queue] == ["C"]
    assert [e["position"] for e in queue] == [1]

    d = await qm.join("D", "d@test.com", "127.0.0.1")
    assert d["position"] == 2

    await close_db()
    db_module._db = None
