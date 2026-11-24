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
