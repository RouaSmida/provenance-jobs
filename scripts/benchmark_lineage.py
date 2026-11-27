"""Benchmark the lineage rebuild with and without the index on events(job_id, id).

Run inside the api container (no local Python needed):
    docker compose exec api python scripts/benchmark_lineage.py
    docker compose exec api python scripts/benchmark_lineage.py --jobs 500 --events-per-job 20 --keep

Steps:
  1. bulk-insert synthetic jobs and events with VALID hash chains (COPY, not one INSERT per row)
  2. time the lineage rebuild for one job, 10 runs -> min/avg/max in ms
  3. EXPLAIN ANALYZE the lineage query (index used)
  4. DROP the index inside a transaction, repeat steps 2 and 3 (sequential scan)
  5. ROLLBACK: Postgres DDL is transactional, so the index comes back exactly as it was,
     even if the script crashes halfway
  6. delete the synthetic data again (unless --keep)

"Lineage rebuild" = exactly what GET /jobs/{id}/lineage does: load the job, load its events
in order, recompute the hash chain.

While the index is dropped, the events table is locked (DROP INDEX takes an exclusive lock),
so the worker and API wait a few seconds to write events. Fine for a dev stack.
"""
import argparse
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import SUCCEEDED, Event, Job
from app.provenance import GENESIS, canonical_json, compute_hash, get_events, verify_chain, verify_events

INDEX = "ix_events_job_id_id"
BENCH_PREFIX = "bench-"
LINEAGE_SQL = "SELECT * FROM events WHERE job_id = :job_id ORDER BY id"


def event_plan(n: int) -> list[tuple[int, str, dict]]:
    """A realistic job history of n events: queued, started, steps..., succeeded."""
    steps = max(n - 3, 0)
    plan = [(0, "queued", {"by": "api"}), (1, "started", {"worker": "bench"})]
    plan += [(1, "step_completed", {"step": i, "output": {"score": round(i / steps, 4)}}) for i in range(steps)]
    plan.append((1, "succeeded", {"result": {"steps": steps}}))
    return plan[:n]


def seed(session: Session, run: str, n_jobs: int, per_job: int) -> list[uuid.UUID]:
    """Insert n_jobs jobs with per_job events each. Events are interleaved round-robin across
    jobs, like many jobs running at the same time, so one job's rows are spread over the table."""
    start = datetime.now(timezone.utc) - timedelta(days=1)
    job_ids = [uuid.uuid4() for _ in range(n_jobs)]
    session.execute(
        insert(Job),
        [
            {"id": j, "status": SUCCEEDED, "idempotency_key": f"{BENCH_PREFIX}{run}-{i}",
             "attempts": 1, "result": {}, "created_at": start, "updated_at": start}
            for i, j in enumerate(job_ids)
        ],
    )

    plan = event_plan(per_job)
    prev = {j: GENESIS for j in job_ids}
    tick = 0
    raw = session.connection().connection.driver_connection  # the psycopg connection
    with raw.cursor() as cur, cur.copy(
        "COPY events (job_id, attempt, event_type, payload, prev_hash, hash, created_at) FROM STDIN"
    ) as copy:
        for attempt, event_type, payload in plan:
            for j in job_ids:
                tick += 1
                created_at = start + timedelta(microseconds=tick)
                h = compute_hash(prev[j], job_id=j, attempt=attempt, event_type=event_type,
                                 payload=payload, created_at=created_at)
                copy.write_row((j, attempt, event_type, canonical_json(payload), prev[j], h, created_at))
                prev[j] = h
    return job_ids


def rebuild_lineage(session: Session, job_id: uuid.UUID) -> dict:
    """Same work as GET /jobs/{id}/lineage."""
    session.get(Job, job_id)
    return verify_events(get_events(session, job_id))


def time_lineage(session: Session, job_id: uuid.UUID, runs: int) -> tuple[list[float], dict]:
    rebuild_lineage(session, job_id)  # warm-up: connection, plan and page cache
    times = []
    for _ in range(runs):
        session.expunge_all()  # no ORM cache: every run really reads from Postgres
        t0 = time.perf_counter()
        report = rebuild_lineage(session, job_id)
        times.append((time.perf_counter() - t0) * 1000)
    return times, report


def explain(session: Session, job_id: uuid.UUID) -> str:
    rows = session.execute(text("EXPLAIN (ANALYZE, BUFFERS) " + LINEAGE_SQL), {"job_id": job_id})
    return "\n".join("    " + r[0] for r in rows)


def summary(times: list[float]) -> str:
    return f"min {min(times):7.3f} ms | avg {statistics.mean(times):7.3f} ms | max {max(times):7.3f} ms"


def index_exists(session: Session) -> bool:
    return session.scalar(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": INDEX})


def cleanup(session: Session) -> None:
    """Remove all benchmark data. events is insert-only, so bypass the trigger (dev superuser)."""
    bench = select(Job.id).where(Job.idempotency_key.like(f"{BENCH_PREFIX}%"))
    session.execute(text("SET LOCAL session_replication_role = replica"))
    n = session.execute(delete(Event).where(Event.job_id.in_(bench))).rowcount
    session.execute(delete(Job).where(Job.id.in_(bench)))
    print(f"cleanup: deleted {n:,} benchmark events and their jobs")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=2000, help="number of synthetic jobs")
    parser.add_argument("--events-per-job", type=int, default=50, help="events per job")
    parser.add_argument("--runs", type=int, default=10, help="timed runs per case")
    parser.add_argument("--keep", action="store_true", help="keep the synthetic data afterwards")
    args = parser.parse_args()

    run = uuid.uuid4().hex[:8]
    total = args.jobs * args.events_per_job
    print(f"seeding {args.jobs:,} jobs x {args.events_per_job} events = {total:,} events ...")
    t0 = time.perf_counter()
    with SessionLocal.begin() as s:
        job_ids = seed(s, run, args.jobs, args.events_per_job)
    print(f"  done in {time.perf_counter() - t0:.1f} s")

    with SessionLocal.begin() as s:
        s.execute(text("ANALYZE events"))  # fresh statistics so the planner sees the real size
    target = job_ids[len(job_ids) // 2]

    with SessionLocal() as s:
        table_rows = s.scalar(select(func.count()).select_from(Event))
        bad = [j for j in job_ids[:: max(len(job_ids) // 50, 1)] if not verify_chain(s, j)["valid"]]
    print(f"events table now holds {table_rows:,} rows; target job {target}")
    print(f"hash chains checked on a sample of jobs: {'all valid' if not bad else f'{len(bad)} BROKEN'}\n")

    with SessionLocal() as s:
        with_idx, report = time_lineage(s, target, args.runs)
        plan_with = explain(s, target)
        s.rollback()

        # DDL is transactional: drop the index, measure, then ROLLBACK brings it back.
        s.execute(text(f"DROP INDEX {INDEX}"))
        try:
            no_idx, report_no = time_lineage(s, target, args.runs)
            plan_without = explain(s, target)
        finally:
            s.rollback()
        restored = index_exists(s)
        if not restored:  # should never happen, but never leave the table without it
            s.execute(text(f"CREATE INDEX IF NOT EXISTS {INDEX} ON events (job_id, id)"))
            s.commit()
            restored = index_exists(s)

    n_ev, ok = report["events_checked"], report["valid"]
    print(f"=== WITH index {INDEX} ({args.runs} runs, {n_ev} events, chain valid={ok}) ===")
    print("  " + summary(with_idx))
    print("  EXPLAIN ANALYZE:\n" + plan_with + "\n")
    print(f"=== WITHOUT index ({args.runs} runs, {report_no['events_checked']} events, "
          f"chain valid={report_no['valid']}) ===")
    print("  " + summary(no_idx))
    print("  EXPLAIN ANALYZE:\n" + plan_without + "\n")

    speedup = statistics.mean(no_idx) / statistics.mean(with_idx)
    print(f"index restored: {restored}")
    print(f"speed-up from the index: {speedup:.1f}x on avg "
          f"({statistics.mean(with_idx):.2f} ms vs {statistics.mean(no_idx):.2f} ms, "
          f"{table_rows:,}-row events table)\n")

    if not args.keep:
        with SessionLocal.begin() as s:
            cleanup(s)


if __name__ == "__main__":
    main()
