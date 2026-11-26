"""Submit N jobs, wait for them to finish, print final status and chain validity.

Run inside the api container (no local Python needed):
    docker compose exec api python scripts/submit_jobs.py
    docker compose exec api python scripts/submit_jobs.py --n 10 --cancel 2
"""
import argparse
import json
import os
import time
import urllib.error
import urllib.request
import uuid

API = os.getenv("API_URL", "http://localhost:8000")
TERMINAL = {"succeeded", "failed", "cancelled"}


def call(method: str, path: str, headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(API + path, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=10, help="number of jobs")
    parser.add_argument("--cancel", type=int, default=0, help="cancel this many jobs right after submitting")
    parser.add_argument("--timeout", type=int, default=180, help="seconds to wait for all jobs")
    args = parser.parse_args()

    run = uuid.uuid4().hex[:8]
    jobs = []
    for i in range(args.n):
        code, job = call("POST", "/jobs", {"Idempotency-Key": f"demo-{run}-{i}"})
        jobs.append(job)
        print(f"submitted #{i:<2} {job['id']}  HTTP {code}")

    # Idempotency: same key again -> same job, HTTP 200, nothing queued twice.
    code, again = call("POST", "/jobs", {"Idempotency-Key": f"demo-{run}-0"})
    print(f"\nresubmit key demo-{run}-0 -> HTTP {code}, same job: {again['id'] == jobs[0]['id']}")

    for job in jobs[-args.cancel:] if args.cancel else []:
        code, body = call("POST", f"/jobs/{job['id']}/cancel")
        print(f"cancel {job['id']} -> HTTP {code} status={body.get('status', body)}")

    print(f"\nwaiting for {args.n} jobs (up to {args.timeout}s)...")
    deadline = time.time() + args.timeout
    final: dict[str, dict] = {}
    while time.time() < deadline:
        for job in jobs:
            if job["id"] not in final:
                _, cur = call("GET", f"/jobs/{job['id']}")
                if cur["status"] in TERMINAL:
                    final[job["id"]] = cur
        if len(final) == len(jobs):
            break
        time.sleep(1)

    print(f"\n{'#':<3} {'job id':<36}  {'status':<10} {'attempts':>8} {'events':>6}  chain")
    counts: dict[str, int] = {}
    all_valid = True
    for i, job in enumerate(jobs):
        _, lin = call("GET", f"/jobs/{job['id']}/lineage")
        status = lin["job"]["status"]
        valid = lin["chain"]["valid"]
        all_valid &= valid
        counts[status] = counts.get(status, 0) + 1
        types = " ".join(e["event_type"] for e in lin["events"])
        print(f"{i:<3} {job['id']}  {status:<10} {lin['job']['attempts']:>8} {len(lin['events']):>6}  "
              f"{'VALID' if valid else 'BROKEN'}")
        print(f"    {types}")

    print(f"\nsummary: {counts}   all chains valid: {all_valid}")


if __name__ == "__main__":
    main()
