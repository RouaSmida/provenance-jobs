# Files in this project

A short guide to every file and folder, grouped by role. Read it top to bottom once, then
use the last section, "Where to look in the code", as a map.

```
provenance-jobs/
├── app/        Python package: API, worker, audit trail, database code
├── scripts/    Demo scripts you run by hand
├── tests/      Unit tests (pytest)
└── (root)      Docker, config and docs files
```

---

## Infrastructure

**`docker-compose.yml`**: Starts the whole system with one command: `postgres`, `redis`,
`api` and `worker`. Each service has a health check, and services wait for the ones they
depend on (`depends_on: condition: service_healthy`). The worker waits for the API because the
API creates the tables. Reads values from `.env` and passes `DATABASE_URL` / `REDIS_URL` to
the API and worker, which read them in `app/config.py`.

**`Dockerfile`**: Builds one Python image used by both the API and the worker; only the
start command differs (set in `docker-compose.yml`). Installs `requirements.txt`, copies
`app/`, `scripts/`, `tests/` and `pytest.ini`, and runs as a non-root user.

**`.dockerignore`**: Lists what must *not* be copied into the image (`.git`, `.env`, caches).
Keeps the image small and keeps secrets out of it.

**`requirements.txt`**: Python dependencies with exact versions (FastAPI, SQLAlchemy,
psycopg, Celery, Redis client, pytest). Pinned because an unpinned `kombu` once pulled in
a version that broke the worker. Used by the `Dockerfile`.

**`.env.example`**: A template of every setting (database user/password, ports,
`FAILURE_RATE`, `MAX_ATTEMPTS`). It is committed to git so the settings are documented.

**`.env`**: Your local copy of `.env.example`. Docker Compose reads it automatically.
It is in `.gitignore`, so real secrets never reach git.

**`.gitignore`**: Tells git to ignore `.env`, Python caches and virtual environments.

**`Makefile`**: Shortcuts such as `make up`, `make test`, `make submit`, `make tamper`. Each
one just runs a `docker compose ...` command, so on Windows without `make` you can type
the command yourself (they are listed in `README.md`).

**`app/config.py`**: Reads settings from environment variables into one `settings`
object: database URL, Redis URL, failure rate, max attempts, backoff, time limits.
Imported by `db.py` and `worker.py`.

**`app/__init__.py`**: Empty. It makes `app/` a Python package so `from app.db import ...`
works.

## API

**`app/main.py`**: The FastAPI web service. Endpoints: `POST /jobs` (create),
`GET /jobs/{id}`, `GET /jobs/{id}/lineage` (events + chain check) and
`POST /jobs/{id}/cancel`, plus `GET /health` for the Docker health check. On startup it calls
`init_db()` from `db.py`. It writes events through `provenance.py` and queues jobs by calling
`run_design_job.delay()` from `worker.py`. The Pydantic classes at the top (`JobOut`,
`EventOut`, ...) define the JSON responses.

## Worker

**`app/worker.py`**: The Celery app and the `run_design_job` task. It takes a job id from
Redis, runs 3 fake steps (`propose_mutation`, `score_candidate`, `decide`), fails at random to
exercise retries, checks the cancel flag between steps, and records every state change with
`append_event()` from `provenance.py`. The Celery settings at the top (`acks_late` etc.)
decide what happens when a worker crashes.

## Provenance / audit trail

**`app/provenance.py`**: The core of the project. `append_event()` is the **only** function
that writes events: it locks the job row, reads the previous hash and inserts the new event
with `hash = SHA-256(prev_hash + canonical JSON)`. `verify_chain()` recomputes every hash and
reports the first broken one. Used by `main.py`, `worker.py`, `scripts/tamper.py` and
`tests/test_provenance.py`.

## Database

**`app/db.py`**: Creates the SQLAlchemy `engine` (the connection pool) and `SessionLocal`
(opens a session/transaction). `init_db()` creates the tables and installs the **insert-only
trigger** that blocks UPDATE/DELETE on `events`. Used by every other file in `app/`.

**`app/models.py`**: The two tables as Python classes. `Job` holds the current state
(status, idempotency key, attempts, cancel flag, result) and changes over time. `Event` is
the audit trail (attempt, type, payload, prev_hash, hash) and is only ever inserted. Also
defines the status names (`queued`, `running`, `succeeded`...).

## Scripts

**`scripts/submit_jobs.py`**: An end-to-end demo. It submits 10 jobs over HTTP, resubmits
one key to show idempotency, can cancel a few (`--cancel 2`), waits for all of them to finish,
then prints each job's status, its event list and whether its chain is valid. It talks only
to the API, never to the database.

**`scripts/tamper.py`**: A tamper-detection demo. It shows that a normal UPDATE on `events`
is blocked by the trigger, then bypasses the trigger (like a database admin could) to edit
one event. `verify_chain()` reports the chain broken at that event; the script then puts the
original back. It talks to the database directly through `db.py` and `provenance.py`.

## Tests

**`tests/test_provenance.py`**: Unit tests for the hash chain, with no database needed.
They check that key order doesn't change the hash, that time zones don't change the hash,
and that an edited, deleted or re-hashed event is detected at the right place.

**`tests/__init__.py`**: Empty; marks `tests/` as a package.

**`pytest.ini`**: pytest settings: where the tests are, and the cache turned off (the
container user can't write it).

## Docs

**`README.md`**: The front page: what the project is, the architecture diagram, how to
run it and the API table.

**`LEARNING_NOTES.md`**: The concepts, design decisions and known gaps for each stage,
written for interview preparation.

**`FILES.md`**: This file.

---

## Where to look in the code

| Topic | File → function | What to notice |
|---|---|---|
| Creating a job | [app/main.py](app/main.py#L89) → `submit_job` | Inserts the `Job` row plus its `queued` event in one transaction |
| Idempotency check | [app/main.py](app/main.py#L96) → `submit_job` | Looks up the key first; if two requests race, the `UNIQUE` constraint raises `IntegrityError` (line 105) and the existing job is returned |
| Queueing after commit | [app/main.py](app/main.py#L114) → `submit_job` | `session.commit()` runs first, then `run_design_job.delay()` |
| Retry logic | [app/worker.py](app/worker.py#L151) → `run_design_job` (the `except Exception` block) | Writes `failed` + `retried` events, waits 2 s then 4 s, then `self.retry()`; after 3 attempts the status is `failed` |
| acks_late | [app/worker.py](app/worker.py#L31) → `celery_app.conf.update` | `task_acks_late` + `task_reject_on_worker_lost`: if a worker dies, the task is delivered again |
| Cancellation (API) | [app/main.py](app/main.py#L135) → `cancel_job` | Queued/retrying → cancelled at once; running → sets `cancel_requested` |
| Cancellation (worker) | [app/worker.py](app/worker.py#L128) → `run_design_job`, plus `_cancel` (line 76) | Flag is checked at start and between steps |
| append_event + row lock | [app/provenance.py](app/provenance.py#L72) → `append_event`, which calls `lock_job` (line 58) | `SELECT ... FOR UPDATE` before reading the previous hash |
| Insert-only trigger | [app/db.py](app/db.py#L18) → `INSERT_ONLY_DDL`, installed by `init_db` (line 35) | Any UPDATE/DELETE on `events` raises an error |
| verify_chain | [app/provenance.py](app/provenance.py#L153) → `verify_chain`, logic in `verify_events` (line 116) | Recomputes each hash and returns the first broken event id |
