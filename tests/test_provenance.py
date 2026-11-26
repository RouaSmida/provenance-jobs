"""Unit tests for the hash chain. No database needed."""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.provenance import GENESIS, canonical_json, compute_hash, verify_events

JOB = uuid.uuid4()
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_chain(n: int) -> list[SimpleNamespace]:
    events, prev = [], GENESIS
    for i in range(n):
        fields = dict(job_id=JOB, attempt=1, event_type=f"e{i}", payload={"i": i, "b": [1, 2]},
                      created_at=T0 + timedelta(seconds=i))
        h = compute_hash(prev, **fields)
        events.append(SimpleNamespace(id=i + 1, prev_hash=prev, hash=h, **fields))
        prev = h
    return events


def test_canonical_json_ignores_key_order():
    assert canonical_json({"b": 1, "a": {"y": 2, "x": 1}}) == canonical_json({"a": {"x": 1, "y": 2}, "b": 1})


def test_hash_depends_on_prev_hash_and_content():
    kw = dict(job_id=JOB, attempt=1, event_type="x", payload={"a": 1}, created_at=T0)
    h = compute_hash(GENESIS, **kw)
    assert h == compute_hash(GENESIS, **kw)
    assert h != compute_hash("other", **kw)
    assert h != compute_hash(GENESIS, **{**kw, "payload": {"a": 2}})


def test_hash_is_timezone_independent():
    kw = dict(job_id=JOB, attempt=1, event_type="x", payload={})
    paris = timezone(timedelta(hours=1))
    assert compute_hash(GENESIS, created_at=T0, **kw) == compute_hash(GENESIS, created_at=T0.astimezone(paris), **kw)


def test_valid_chain():
    assert verify_events(make_chain(5))["valid"] is True


def test_edited_payload_is_detected_at_that_event():
    chain = make_chain(5)
    chain[2].payload = {"i": 999, "b": [1, 2]}
    report = verify_events(chain)
    assert report["valid"] is False
    assert report["first_broken_event_id"] == 3


def test_deleted_event_is_detected():
    chain = make_chain(5)
    del chain[2]
    report = verify_events(chain)
    assert report["valid"] is False
    assert report["first_broken_event_id"] == 4  # its prev_hash points at the deleted one


def test_rehashing_one_event_still_breaks_the_next_link():
    chain = make_chain(5)
    e = chain[1]
    e.payload = {"forged": True}
    e.hash = compute_hash(e.prev_hash, job_id=e.job_id, attempt=e.attempt, event_type=e.event_type,
                          payload=e.payload, created_at=e.created_at)
    assert verify_events(chain)["first_broken_event_id"] == 3
