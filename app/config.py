"""Settings read from environment variables (docker-compose sets them)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql+psycopg://provenance:provenance@localhost:5432/provenance"
    )
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # Fake workload
    failure_rate: float = float(os.getenv("FAILURE_RATE", "0.3"))  # chance that an attempt fails
    step_min_s: float = float(os.getenv("STEP_MIN_S", "1"))
    step_max_s: float = float(os.getenv("STEP_MAX_S", "2"))

    # Retries: 1 attempt + 2 retries = 3 attempts. Backoff = base * 2**(attempt-1) -> 2 s, 4 s.
    max_attempts: int = int(os.getenv("MAX_ATTEMPTS", "3"))
    retry_backoff_base_s: int = int(os.getenv("RETRY_BACKOFF_BASE_S", "2"))

    # Per-attempt time limits: the soft one raises an exception we record,
    # the hard one kills the worker process (acks_late then redelivers the task).
    task_soft_time_limit_s: int = int(os.getenv("TASK_SOFT_TIME_LIMIT_S", "30"))
    task_time_limit_s: int = int(os.getenv("TASK_TIME_LIMIT_S", "40"))

    # Redis broker: a task taken by a worker but never acked (the worker died) goes back to
    # the queue after this many seconds. It must be larger than the longest task
    # (task_time_limit_s) and the longest retry countdown, or a slow but healthy task
    # would be delivered twice. Lower = faster recovery after a crash.
    visibility_timeout_s: int = int(os.getenv("VISIBILITY_TIMEOUT_S", "60"))


settings = Settings()
