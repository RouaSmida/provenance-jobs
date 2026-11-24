# provenance-jobs

A learning prototype of an **agentic job platform with data provenance**: long-running
"protein design" jobs run on background workers, and every decision they make is written
to a **tamper-evident, hash-chained audit trail** that stays consistent when jobs are
retried, cancelled or partially fail.

> Status: **Stage 0 – setup and architecture.** See [LEARNING_NOTES.md](LEARNING_NOTES.md)
> for what each stage teaches.

## Quick start

```bash
make up      # create .env, start Postgres + Redis, wait until healthy
make check   # smoke test both services
make help    # list every command
```

## Architecture

```mermaid
flowchart LR
    client([Client])

    subgraph API["FastAPI service"]
        submit["POST /jobs<br/>Idempotency-Key"]
        cancel["POST /jobs/{id}/cancel"]
        lineage["GET /jobs/{id}/lineage<br/>GET /candidates/{id}/path"]
    end

    redis[("Redis<br/>Celery broker")]

    subgraph Workers["Celery workers (N replicas)"]
        task["run_design_job<br/>retries, acks_late, time limit"]
        subgraph Agent["LangGraph agent pipeline"]
            propose[propose_mutation] --> score[score_candidate] --> decide{decide}
            decide -- "loop, max N" --> propose
        end
        task --> Agent
    end

    subgraph PG["PostgreSQL – source of truth"]
        jobs[("jobs<br/>status, idempotency_key")]
        events[("events – insert-only<br/>prev_hash → hash chain per job")]
    end

    nx["NetworkX DiGraph<br/>candidate → parent → decisions"]

    client --> submit & cancel & lineage
    submit -- "1. insert job + 'queued' event" --> jobs
    submit -- "2. enqueue job_id" --> redis
    cancel -- "set cancel flag + event" --> jobs
    redis -- "deliver task" --> task
    task -- "status changes" --> jobs
    Agent -- "one event per node run" --> events
    task -- "started / failed / retried / succeeded" --> events
    lineage -- "ordered read by job_id" --> events
    lineage --> nx
```

### How a job flows

1. **Submit.** `POST /jobs` with an `Idempotency-Key` header. In one database
   transaction the API inserts the job and its first event (`queued`). Only then does
   it push the job id onto Redis. If the same key is sent again, the existing job is
   returned and nothing is queued twice.
2. **Execute.** A Celery worker takes the task from Redis and runs the agent pipeline
   (propose → score → decide). Each step is recorded as an event.
3. **Fail, retry, cancel.** Failures are retried with exponential backoff (max 3
   attempts). If a worker dies, `acks_late` makes Redis redeliver the task to another
   worker. A cancel request sets a flag that the task checks between steps. Every one
   of these transitions is an event, tagged with its `attempt` number.
4. **Audit.** Events are never updated or deleted. Each event stores
   `hash = SHA-256(prev_hash + canonical JSON)`, so editing any past row breaks every
   hash after it. `verify_chain(job_id)` recomputes the chain to detect this.
5. **Lineage.** The lineage endpoints read a job's events in order (indexed by
   `job_id`) and build a directed graph of candidates and decisions. Target: under 1 s
   over 10,000+ events.

### Responsibilities

| Component | Role | If it dies… |
|---|---|---|
| **PostgreSQL** | Source of truth: job state and the audit trail | Nothing runs; nothing is lost (durable volume) |
| **Redis** | Transport only: queue of job ids waiting for a worker | AOF persistence keeps queued messages across restarts |
| **FastAPI** | Accepts and validates requests, serves lineage | Stateless: restart it or run more replicas |
| **Celery worker** | Runs jobs and writes events | `acks_late`: the unfinished task is delivered again |

The key rule: **Redis can lose or duplicate a message and the audit trail must still be
correct.** Everything that matters is written to Postgres.

## Repository layout

```
provenance-jobs/
├── app/                 # Python package: API, models, worker, provenance, agent (Stages 1–4)
├── tests/               # pytest suite (Stages 1–8)
├── scripts/             # benchmark and resilience demo (Stages 5–6)
├── docker-compose.yml   # postgres + redis now; api, worker, prometheus, grafana later
├── .env.example         # configuration template → copy to .env
├── Makefile             # common commands (`make help`)
└── LEARNING_NOTES.md    # concepts, decisions and interview Q&A per stage
```

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2.0 · Celery · Redis · PostgreSQL 16 · LangGraph ·
NetworkX · Docker Compose · Prometheus · Grafana · GitHub Actions
