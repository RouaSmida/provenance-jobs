"""Celery app and the run_design_job task.

Every state change is written by append_event() inside the same transaction that
changes jobs.status. The job's status and its audit trail therefore can't disagree.

Safety rules the task follows:
- Idempotent start: a job that is already succeeded/failed/cancelled is skipped. That
  covers duplicate or redelivered messages.
- Fencing: each attempt bumps jobs.attempts and remembers its number. Before every
  write it checks the number is still current, so a stale attempt (e.g. one that was
  presumed dead and redelivered) can't write over a newer one.
- Cancellation is cooperative: the flag is checked before the first step and between steps.
"""
import logging
import random
import string
import time
import uuid

from celery import Celery

from app.config import settings
from app.db import SessionLocal
from app.models import CANCELLED, FAILED, RETRYING, RUNNING, SUCCEEDED, TERMINAL, Job
from app.provenance import append_event, lock_job

log = logging.getLogger(__name__)

celery_app = Celery("provenance_jobs", broker=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,  # ack only after the task finishes, so a crash means redelivery
    task_reject_on_worker_lost=True,  # if the worker process is killed, requeue the task
    worker_prefetch_multiplier=1,  # don't hoard messages that another worker could run
    task_ignore_result=True,  # results live in Postgres, not in a Celery result backend
    # Unacked messages return to the queue after this long. It must be longer than the
    # longest task, or a slow-but-alive task gets delivered twice.
    broker_transport_options={"visibility_timeout": 300},
    broker_connection_retry_on_startup=True,
)

STEPS = ("propose_mutation", "score_candidate", "decide")
AMINO_ACIDS = "ACDEFGHIKLMNPQRSTVWY"


class SimulatedFailure(Exception):
    """Stands in for a transient error (GPU OOM, network blip, model timeout...)."""


class _Stale(Exception):
    """This attempt is no longer the current one; stop without writing anything."""


def _run_step(name: str, state: dict) -> dict:
    """Fake 'protein design' work. Returns the step output and updates state."""
    time.sleep(random.uniform(settings.step_min_s, settings.step_max_s))
    if name == "propose_mutation":
        pos = random.randint(1, 120)
        frm, to = random.sample(AMINO_ACIDS, 2)
        out = {"mutation": f"{frm}{pos}{to}", "candidate_id": "cand-" + "".join(random.choices(string.hexdigits.lower(), k=8))}
    elif name == "score_candidate":
        out = {"score": round(random.uniform(0, 1), 4)}
    else:  # decide
        out = {"accepted": state["score"] >= 0.5, "threshold": 0.5}
    state.update(out)
    return out


def _current(session, job_id: uuid.UUID, attempt: int) -> Job:
    """Lock the job and make sure this attempt still owns it."""
    job = lock_job(session, job_id)
    if job is None or job.status != RUNNING or job.attempts != attempt:
        raise _Stale()
    return job


def _cancel(session, job: Job, attempt: int, before_step: int) -> None:
    job.status = CANCELLED
    append_event(session, job.id, "cancelled", {"by": "worker", "before_step": before_step}, attempt)


@celery_app.task(
    bind=True,
    name="run_design_job",
    acks_late=True,
    max_retries=None,  # we count attempts in Postgres (survives redelivery), not in Celery
    soft_time_limit=settings.task_soft_time_limit_s,
    time_limit=settings.task_time_limit_s,
)
def run_design_job(self, job_id: str) -> str:
    job_uuid = uuid.UUID(job_id)

    # --- 1. Start: idempotency guard + new attempt -------------------------------------
    with SessionLocal.begin() as s:
        job = lock_job(s, job_uuid)
        if job is None:
            log.warning("job %s not found, dropping message", job_id)
            return "missing"
        if job.status in TERMINAL:
            return f"skipped: already {job.status}"  # duplicate / redelivered message
        if job.cancel_requested:
            _cancel(s, job, job.attempts, before_step=1)
            return "cancelled"
        if job.attempts >= settings.max_attempts:
            # e.g. the hard time limit killed the last attempt and acks_late redelivered it
            job.status = FAILED
            append_event(s, job.id, "failed", {"error": "attempts exhausted", "will_retry": False}, job.attempts)
            return "failed"
        job.attempts += 1
        attempt = job.attempts
        job.status = RUNNING
        append_event(
            s, job.id, "started",
            {"worker": self.request.hostname, "celery_task_id": self.request.id},
            attempt,
        )

    # --- 2. Steps: work outside the transaction, then record + cancel check --------------
    state: dict = {}
    fail_at = random.randint(1, len(STEPS)) if random.random() < settings.failure_rate else None
    try:
        for step_no, name in enumerate(STEPS, start=1):
            if step_no == fail_at:
                time.sleep(random.uniform(settings.step_min_s, settings.step_max_s))
                raise SimulatedFailure(f"simulated transient failure in {name}")
            output = _run_step(name, state)
            with SessionLocal.begin() as s:
                job = _current(s, job_uuid, attempt)
                if job.cancel_requested:  # checked between steps; this step's output is discarded
                    _cancel(s, job, attempt, before_step=step_no)
                    return "cancelled"
                append_event(s, job.id, "step_completed", {"step": step_no, "name": name, "output": output}, attempt)

        # --- 3. Success -----------------------------------------------------------------
        with SessionLocal.begin() as s:
            job = _current(s, job_uuid, attempt)
            job.status = SUCCEEDED
            job.result = state
            append_event(s, job.id, "succeeded", {"result": state}, attempt)
        return "succeeded"

    except _Stale:
        log.info("job %s attempt %s is stale, stopping", job_id, attempt)
        return "stale"

    except Exception as exc:  # SimulatedFailure, SoftTimeLimitExceeded, real bugs...
        with SessionLocal.begin() as s:
            try:
                job = _current(s, job_uuid, attempt)
            except _Stale:
                return "stale"
            will_retry = attempt < settings.max_attempts
            append_event(
                s, job.id, "failed",
                {"error": str(exc), "error_type": type(exc).__name__, "will_retry": will_retry},
                attempt,
            )
            if will_retry:
                countdown = settings.retry_backoff_base_s * 2 ** (attempt - 1)
                job.status = RETRYING
                append_event(s, job.id, "retried", {"next_attempt": attempt + 1, "countdown_s": countdown}, attempt)
            else:
                job.status = FAILED
        if will_retry:
            # Publishes a new delayed message, then acks this one.
            raise self.retry(exc=exc, countdown=countdown)
        return "failed"
