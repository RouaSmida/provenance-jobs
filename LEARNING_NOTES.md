# Learning notes – provenance-jobs

One section per stage: concepts, design decisions, and the interview questions I answered.

---

## Stage 0 – Setup and architecture

### Concepts
- **Source of truth vs transport.** PostgreSQL holds everything that matters (jobs, events).
  Redis only carries "please run job X" messages between the API and the workers. If Redis
  loses or duplicates a message, the system must still end up correct.
- **Message broker.** A queue between producers (API) and consumers (workers). The API
  returns immediately, and slow jobs run in the background at the pace the workers allow.
- **Health checks.** `pg_isready` and `redis-cli ping` check that a service *accepts
  requests*, not just that its process is running. `docker compose up --wait` blocks until
  every service is healthy, and later services can use `depends_on: condition: service_healthy`.
- **Redis persistence (AOF).** `appendonly yes` logs every write to disk. `everysec` fsyncs
  once per second: at most ~1 s of queue writes can be lost on a crash. This is a trade-off
  between speed and durability.

### Decisions
| Decision | Why |
|---|---|
| Postgres 16 + named volume | Durable data that survives `docker compose down` (only `make clean` deletes it) |
| Redis with AOF on | Queued jobs survive a Redis restart |
| Ports bound to `127.0.0.1` | Databases are never exposed to the network, even in development |
| `.env.example` committed, `.env` ignored | Config is documented; secrets are never in git |
| Write to Postgres *first*, then enqueue | If enqueueing fails, the job row still exists and can be re-queued. The opposite order could run a job that has no record |

### Interview questions
_(filled in after the Q&A)_

---

## Stages 1–3 – API, worker, hash-chained audit trail

### Concepts
- **Idempotency key.** The client picks a key; a `UNIQUE` constraint on `jobs.idempotency_key`
  makes a retried POST return the same job. Even two requests that arrive at the same time
  create only one job: the loser gets an `IntegrityError` and reads the winner's row.
- **At-least-once delivery.** `acks_late` + `task_reject_on_worker_lost` mean a task that
  was running when its worker died gets delivered again. The price is that tasks *will*
  sometimes run twice, so they must be idempotent.
- **Fencing token.** `jobs.attempts` is incremented at the start of each attempt. Every
  later write checks "is my attempt still the current one?" under the row lock, so an
  attempt that was presumed dead (and replaced) cannot write anything.
- **Row lock serialises the chain.** `append_event` does `SELECT ... FOR UPDATE` on the job
  before reading the last hash, so two writers can't both build on the same previous hash.
- **Hash chain.** `hash = SHA-256(prev_hash + canonical JSON)`. Canonical = sorted keys, no
  whitespace, UTC timestamps, so the same content always gives the same bytes.
- **Defence in depth.** A trigger blocks UPDATE/DELETE on `events`; the hash chain *detects*
  what a superuser could still do by bypassing it (`scripts/tamper.py` shows both).

### Decisions
| Decision | Why |
|---|---|
| Status change + its event in one transaction | `jobs.status` and the audit trail can never disagree |
| Attempts counted in Postgres, not `self.request.retries` | Celery's counter resets on redelivery; the DB count survives crashes |
| Retry = `failed` + `retried` events, backoff 2 s, 4 s (max 3 attempts) | Every retry decision is in the audit trail |
| Cancel is cooperative (checked between steps) | You can't safely kill a step halfway; a queued job is cancelled directly |
| Index `(job_id, id)` | One index both filters by job and returns events in chain order |
| No Celery result backend | Postgres is the only place results live |

### Known gaps (next stages)
- If enqueueing to Redis fails right after the commit, the job stays `queued`. Fix: an outbox
  table or a sweeper that re-queues old `queued`/`retrying` jobs.
- Deleting the *last* event of a chain isn't detected (nothing points at it). Fix: anchor the
  head hash somewhere outside the DB (signed, or published periodically).
- `create_all` instead of Alembic migrations.

## Stage 5 – Performance: the lineage query and its index

Run: `docker compose exec api python scripts/benchmark_lineage.py` (or `make benchmark`).

### Result (100,593-row `events` table, one job with 50 events, 10 runs)
| | min | avg | max | SQL only (`EXPLAIN ANALYZE`) | pages read |
|---|---|---|---|---|---|
| with index `ix_events_job_id_id` | 3.60 ms | 3.90 ms | 4.36 ms | 0.22 ms (Bitmap Index Scan) | 53 |
| without index | 8.56 ms | 11.24 ms | 14.00 ms | 7.31 ms (Seq Scan, 100,543 rows thrown away) | 3,555 |

The SQL itself is **~33x faster** with the index. End to end it is ~3x, because the other
~3.7 ms is Python: building ORM objects and recomputing 50 SHA-256 hashes. That part does not
grow with the table; the seq scan does, linearly.

### Why the index makes it fast (simple words)
- **Without the index**, Postgres has no idea where job X's events are, so it reads the whole
  table (3,555 pages, 100k rows), checks every row's `job_id`, and throws away 99.95% of them.
  Double the table, double the time, even though the job still has 50 events.
- **With the index**, Postgres keeps a sorted list of `(job_id, id) -> row location`, like the
  index at the back of a book. It jumps straight to job X (3 index pages), then fetches only
  those 50 rows (50 heap pages). The cost depends on *the size of the job*, not of the table.
- Here the plan is a *bitmap* scan + a tiny sort, because each event of the job sits on a
  different page (jobs run at the same time, so their events interleave). The `id` part of
  the index still lets Postgres return rows already in order when it prefers a plain index scan.

### Concepts
- `EXPLAIN (ANALYZE, BUFFERS)` runs the query and shows the real plan, time and pages touched.
  "Rows Removed by Filter" is the tell-tale sign of a missing index.
- `ANALYZE events` after a bulk load, so the planner knows the table's real size.
- **Transactional DDL.** `DROP INDEX` inside a transaction + `ROLLBACK` restores the index
  exactly, even if the script crashes halfway. (It holds an exclusive lock on `events` while
  dropped, so writers wait: fine on a dev stack, not in production.)
- **Bulk load with `COPY`** instead of one INSERT per row: 100k rows in ~3.5 s, hashes computed
  in Python with the same `compute_hash` the app uses, so every chain is valid.

### Interview line
"On a 100,000-event table, the composite index on `(job_id, id)` cut the lineage query from
7.3 ms to 0.22 ms (~33x, 53 pages read instead of 3,555), and the full lineage rebuild
including hash verification from 11.2 ms to 3.9 ms on average."

