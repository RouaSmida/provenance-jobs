"""Resilience demo: kill the worker in the middle of 20 jobs and show nothing is lost.

Runs on the HOST (it calls `docker compose`), standard library only. From the repo root:

    py scripts\\resilience_demo.py                 # Windows PowerShell
    python3 scripts/resilience_demo.py            # macOS / Linux

What it does:
  1. submit 20 jobs through the API
  2. after a few seconds: `docker compose kill worker` (SIGKILL, no clean shutdown)
  3. leave the worker dead for a few seconds, then `docker compose up -d worker`
  4. wait until every job is finished (or the timeout)
  5. verify every job's hash chain and print a summary

Recovery is not instant. The killed worker never acked its tasks (acks_late), so Redis
holds them as "unacked" and puts them back on the queue only after the broker's
visibility_timeout (VISIBILITY_TIMEOUT_S, default 60 s). Expect about a minute of waiting.
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # docker compose must run next to docker-compose.yml
TERMINAL = {"succeeded", "failed", "cancelled"}


def call(api: str, method: str, path: str, headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(api + path, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def compose(*args: str) -> None:
    cmd = ["docker", "compose", *args]
    print(f"\n$ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=ROOT, check=True)


def statuses(api: str, ids: list[str]) -> dict[str, str]:
    return {i: call(api, "GET", f"/jobs/{i}")[1]["status"] for i in ids}


def count(values) -> str:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return ", ".join(f"{k}={n}" for k, n in sorted(out.items()))


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--n", type=int, default=20, help="jobs to submit (default 20)")
    p.add_argument("--kill-after", type=float, default=4, help="seconds before killing the worker")
    p.add_argument("--down-for", type=float, default=5, help="seconds the worker stays dead")
    p.add_argument("--timeout", type=int, default=300, help="max seconds to wait for all jobs")
    # 127.0.0.1, not "localhost": on Windows "localhost" tries IPv6 ::1 first, and the API
    # is bound to IPv4 only, so every request would wait ~2 s for the fallback.
    p.add_argument("--api", default="http://127.0.0.1:8000")
    args = p.parse_args()
    # Print our lines in order with docker's output (stdout is block-buffered when piped).
    sys.stdout.reconfigure(line_buffering=True)

    # --- 0. preflight ---------------------------------------------------------------
    try:
        call(args.api, "GET", "/health")
    except OSError as err:
        print(f"API not reachable at {args.api} ({err}). Start the stack first:\n"
              "    docker compose up -d --build --wait")
        return 2

    # --- 1. submit ------------------------------------------------------------------
    run = uuid.uuid4().hex[:8]
    print(f"[{stamp()}] submitting {args.n} jobs (run {run})")
    ids = [call(args.api, "POST", "/jobs", {"Idempotency-Key": f"resilience-{run}-{i}"})[1]["id"]
           for i in range(args.n)]

    # --- 2. kill the worker mid-flight ------------------------------------------------
    time.sleep(args.kill_after)
    before_kill = statuses(args.api, ids)
    running_at_kill = [i for i, s in before_kill.items() if s == "running"]
    print(f"[{stamp()}] just before kill: {count(before_kill.values())}")
    compose("kill", "worker")
    t_kill = time.time()

    # --- 3. worker is dead: nothing moves -----------------------------------------------
    time.sleep(args.down_for)
    print(f"[{stamp()}] worker dead for {args.down_for:.0f}s: {count(statuses(args.api, ids).values())}"
          "   <- 'running' jobs are stuck: nobody is executing them")
    compose("up", "-d", "worker")

    # --- 4. wait for everything to finish ----------------------------------------------
    print(f"\n[{stamp()}] waiting up to {args.timeout}s. Tasks the dead worker held come back "
          "after the visibility timeout (~60 s)")
    deadline = time.time() + args.timeout
    last_print = 0.0
    while True:
        now = statuses(args.api, ids)
        done = all(s in TERMINAL for s in now.values())
        if done or time.time() >= deadline:
            break
        if time.time() - last_print >= 10:
            print(f"[{stamp()}] +{time.time() - t_kill:4.0f}s after kill: {count(now.values())}")
            last_print = time.time()
        time.sleep(2)
    print(f"[{stamp()}] {'all jobs finished' if done else 'TIMEOUT'} "
          f"{time.time() - t_kill:.0f}s after the kill: {count(now.values())}")

    # --- 5. verify every chain + summary -----------------------------------------------
    rows = []
    for idx, job_id in enumerate(ids):
        lin = call(args.api, "GET", f"/jobs/{job_id}/lineage")[1]
        events = lin["events"]
        rows.append({
            "idx": idx,
            "id": job_id,
            "status": lin["job"]["status"],
            "attempts": lin["job"]["attempts"],
            "retried": sum(e["event_type"] == "retried" for e in events),
            "recovered": sum("recovered_from_attempt" in e["payload"] for e in events),
            "running_at_kill": job_id in running_at_kill,
            "valid": lin["chain"]["valid"],
            "events": events,
        })

    print(f"\n{'#':>2}  {'job id':<8}  {'status':<9}  {'attempts':>8}  {'retries':>7}  "
          f"{'crash-recovered':>15}  {'running@kill':>12}  chain")
    print("-" * 86)
    for r in rows:
        print(f"{r['idx']:>2}  {r['id'][:8]:<8}  {r['status']:<9}  {r['attempts']:>8}  {r['retried']:>7}  "
              f"{'yes' if r['recovered'] else '':>15}  {'yes' if r['running_at_kill'] else '':>12}  "
              f"{'VALID' if r['valid'] else 'BROKEN'}")

    n = len(rows)
    summary = [
        ("succeeded", sum(r["status"] == "succeeded" for r in rows)),
        ("failed", sum(r["status"] == "failed" for r in rows)),
        ("cancelled", sum(r["status"] == "cancelled" for r in rows)),
        ("not finished", sum(r["status"] not in TERMINAL for r in rows)),
        ("retried (>=1 retry after a failure)", sum(r["retried"] > 0 for r in rows)),
        ("running when the worker was killed", len(running_at_kill)),
        ("recovered after the kill (redelivered)", sum(r["recovered"] > 0 for r in rows)),
        ("chains valid", sum(r["valid"] for r in rows)),
    ]
    print("\nSUMMARY")
    print("=" * 50)
    for label, value in summary:
        print(f"  {label:<40} {value:>3} / {n}")
    print("=" * 50)

    # --- 6. one crash-recovered job, event by event ----------------------------------------
    example = next((r for r in rows if r["recovered"]), None)
    if example:
        print(f"\nTimeline of job {example['id']} (was running when the worker was killed):")
        t0 = datetime.fromisoformat(example["events"][0]["created_at"])
        for e in example["events"]:
            dt = (datetime.fromisoformat(e["created_at"]) - t0).total_seconds()
            payload = {k: v for k, v in e["payload"].items() if k not in ("celery_task_id",)}
            print(f"  +{dt:6.1f}s  attempt {e['attempt']}  {e['event_type']:<15} {json.dumps(payload)}")

    ok = all(r["status"] in TERMINAL and r["valid"] for r in rows)
    if ok:
        print("\nRESULT: PASS: every job finished and every chain is valid")
    else:
        print("\nRESULT: FAIL: some jobs did not finish or a chain is broken")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
