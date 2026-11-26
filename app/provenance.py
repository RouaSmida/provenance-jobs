"""The hash-chained audit trail.

append_event() is the ONLY function that writes events. verify_chain() recomputes the chain.

    hash = SHA-256(prev_hash + canonical_json(event content))

A job's first event uses prev_hash = "genesis". If any stored field of an event is edited,
its recomputed hash no longer matches the stored one, and the next event still points at
the old hash. The first mismatch shows which row was changed.
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Event, Job

GENESIS = "genesis"


def canonical_json(obj: Any) -> str:
    """Exactly one byte representation per value: sorted keys, no whitespace, no NaN."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _ts(dt: datetime) -> str:
    # Convert to UTC so the hash doesn't depend on the DB session's time zone.
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def compute_hash(
    prev_hash: str,
    *,
    job_id: uuid.UUID | str,
    attempt: int,
    event_type: str,
    payload: dict,
    created_at: datetime,
) -> str:
    body = canonical_json(
        {
            "job_id": str(job_id),
            "attempt": attempt,
            "event_type": event_type,
            "payload": payload,
            "created_at": _ts(created_at),
        }
    )
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


def lock_job(session: Session, job_id: uuid.UUID) -> Job | None:
    """SELECT ... FOR UPDATE on the job row, replacing any cached copy with fresh values.

    The lock is held until the caller's transaction commits or rolls back.
    """
    stmt = (
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.execute(stmt).scalar_one_or_none()


def append_event(
    session: Session,
    job_id: uuid.UUID,
    event_type: str,
    payload: dict | None = None,
    attempt: int = 0,
) -> Event:
    """Append one event to a job's chain. The caller commits.

    The job row lock makes writers to the same job take turns. Without it, two writers
    (two workers, or the API and a worker) could read the same previous hash and
    split the chain into two branches.
    """
    if lock_job(session, job_id) is None:
        raise LookupError(f"job {job_id} not found")

    prev_hash = session.execute(
        select(Event.hash).where(Event.job_id == job_id).order_by(Event.id.desc()).limit(1)
    ).scalar_one_or_none() or GENESIS

    # Round-trip through canonical JSON so the hashed payload is exactly what JSONB stores.
    payload = json.loads(canonical_json(payload or {}))
    created_at = datetime.now(timezone.utc)
    event = Event(
        job_id=job_id,
        attempt=attempt,
        event_type=event_type,
        payload=payload,
        prev_hash=prev_hash,
        hash=compute_hash(
            prev_hash,
            job_id=job_id,
            attempt=attempt,
            event_type=event_type,
            payload=payload,
            created_at=created_at,
        ),
        created_at=created_at,
    )
    session.add(event)
    session.flush()  # assigns event.id now, while the job row is still locked
    return event


def verify_events(events: Iterable[Event]) -> dict:
    """Recompute a chain from events in insertion order and report the first broken link."""
    prev_hash = GENESIS
    checked = 0
    for ev in events:
        checked += 1
        if ev.prev_hash != prev_hash:
            return _broken(
                ev, checked,
                "prev_hash does not match the previous event's hash "
                "(an event was removed, reordered or edited)",
            )
        expected = compute_hash(
            prev_hash,
            job_id=ev.job_id,
            attempt=ev.attempt,
            event_type=ev.event_type,
            payload=ev.payload,
            created_at=ev.created_at,
        )
        if expected != ev.hash:
            return _broken(
                ev, checked, "stored hash does not match the event's content (the event was edited)"
            )
        prev_hash = ev.hash
    return {"valid": True, "events_checked": checked, "first_broken_event_id": None, "reason": None}


def _broken(ev: Event, checked: int, reason: str) -> dict:
    return {"valid": False, "events_checked": checked, "first_broken_event_id": ev.id, "reason": reason}


def get_events(session: Session, job_id: uuid.UUID) -> list[Event]:
    stmt = select(Event).where(Event.job_id == job_id).order_by(Event.id)
    return list(session.execute(stmt).scalars())


def verify_chain(session: Session, job_id: uuid.UUID) -> dict:
    return verify_events(get_events(session, job_id))
