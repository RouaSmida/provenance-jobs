"""FastAPI service: submit, inspect, cancel jobs and read their audit trail."""
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import SessionLocal, init_db
from app.models import CANCELLED, QUEUED, RUNNING, TERMINAL, Job
from app.provenance import append_event, get_events, lock_job, verify_events
from app.worker import run_design_job

log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="provenance-jobs", lifespan=lifespan)


# --- schemas -------------------------------------------------------------------------
class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    status: str
    idempotency_key: str | None
    attempts: int
    cancel_requested: bool
    result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    attempt: int
    event_type: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    created_at: datetime


class ChainReport(BaseModel):
    valid: bool
    events_checked: int
    first_broken_event_id: int | None
    reason: str | None


class LineageOut(BaseModel):
    job: JobOut
    chain: ChainReport
    events: list[EventOut]


# --- helpers -------------------------------------------------------------------------
def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def _get_job_or_404(session: Session, job_id: uuid.UUID) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


# --- endpoints -----------------------------------------------------------------------
@app.get("/health")
def health(session: Session = Depends(get_session)) -> dict:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.post("/jobs", status_code=201, response_model=JobOut)
def submit_job(
    response: Response,
    idempotency_key: str | None = Header(default=None, max_length=255),
    session: Session = Depends(get_session),
) -> Job:
    """Create a job. Same Idempotency-Key again -> 200 with the existing job, nothing re-queued."""
    if idempotency_key:
        existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing:
            response.status_code = 200
            return existing

    job = Job(id=uuid.uuid4(), status=QUEUED, idempotency_key=idempotency_key)
    session.add(job)
    try:
        session.flush()  # the UNIQUE constraint decides races between identical requests
    except IntegrityError:
        session.rollback()
        response.status_code = 200
        return session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))

    append_event(session, job.id, "queued", {"idempotency_key": idempotency_key})
    session.commit()  # 1. the job + its first event are durable in Postgres...

    try:
        run_design_job.delay(str(job.id))  # 2. ...and only then is it queued
    except Exception:
        # The job row survives with status 'queued'; a re-queue sweeper (later stage)
        # can pick it up. The opposite order could run a job that has no record.
        log.exception("failed to enqueue job %s", job.id)
    return job


@app.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, session: Session = Depends(get_session)) -> Job:
    return _get_job_or_404(session, job_id)


@app.get("/jobs/{job_id}/lineage", response_model=LineageOut)
def get_lineage(job_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    job = _get_job_or_404(session, job_id)
    events = get_events(session, job_id)
    return {"job": job, "chain": verify_events(events), "events": events}


@app.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: uuid.UUID, session: Session = Depends(get_session)) -> Job:
    job = lock_job(session, job_id)  # same lock the worker takes, so no race on status
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status in TERMINAL:
        raise HTTPException(status_code=409, detail=f"job already {job.status}")

    job.cancel_requested = True
    if job.status == RUNNING:
        # A worker is executing it: it will see the flag between steps and write 'cancelled'.
        append_event(session, job.id, "cancel_requested", {"by": "api"}, job.attempts)
    else:
        # queued / retrying: no worker holds it. Cancel now; a later delivery is skipped
        # because the status is terminal.
        previous = job.status
        job.status = CANCELLED
        append_event(session, job.id, "cancelled", {"by": "api", "while": previous}, job.attempts)
    session.commit()
    return job
