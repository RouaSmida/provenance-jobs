"""Edit one audit event directly in Postgres and show that verify_chain() catches it.

    docker compose exec api python scripts/tamper.py            # latest job, then restore
    docker compose exec api python scripts/tamper.py <job_id>
    docker compose exec api python scripts/tamper.py --keep     # leave the chain broken

Steps:
  1. verify the chain (valid)
  2. try a normal UPDATE -> blocked by the insert-only trigger
  3. bypass the trigger like a superuser/DBA could, change one payload
  4. verify again -> invalid, pointing at the edited event
  5. restore the original payload -> valid again (unless --keep)
"""
import argparse
import copy
import sys
import uuid

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.db import SessionLocal
from app.models import Event
from app.provenance import get_events, verify_chain


def show(label: str, job_id: uuid.UUID) -> dict:
    with SessionLocal() as s:  # fresh session: no cached rows
        report = verify_chain(s, job_id)
    mark = "VALID" if report["valid"] else "BROKEN"
    print(f"[{label}] chain {mark}: {report}")
    return report


def raw_update(event_id: int, payload: dict, bypass_trigger: bool) -> None:
    with SessionLocal.begin() as s:
        if bypass_trigger:
            # Disables triggers for this transaction only. Needs superuser, which our dev
            # DB user is. In production the app role would have INSERT/SELECT only.
            s.execute(text("SET LOCAL session_replication_role = replica"))
        s.execute(update(Event).where(Event.id == event_id).values(payload=payload))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id", nargs="?", help="job to tamper with (default: latest job with >= 3 events)")
    parser.add_argument("--keep", action="store_true", help="don't restore the original payload")
    args = parser.parse_args()

    with SessionLocal() as s:
        if args.job_id:
            job_id = uuid.UUID(args.job_id)
        else:
            job_id = s.scalar(
                select(Event.job_id).group_by(Event.job_id)
                .having(func.count() >= 3).order_by(func.max(Event.id).desc()).limit(1)
            )
            if job_id is None:
                sys.exit("no job with >= 3 events yet; run scripts/submit_jobs.py first")
        events = get_events(s, job_id)

    print(f"job {job_id}: {len(events)} events")
    for e in events:
        print(f"  #{e.id:<5} {e.event_type:<16} prev={e.prev_hash[:10]:<10} hash={e.hash[:10]}")

    # Pick a step that has a score if there is one (the "interesting" lie), else the middle event.
    target = next((e for e in events if "score" in e.payload.get("output", {})), events[len(events) // 2])
    original = copy.deepcopy(target.payload)
    tampered = copy.deepcopy(original)
    if "score" in tampered.get("output", {}):
        tampered["output"]["score"] = 0.9999
    else:
        tampered["tampered"] = True

    print()
    show("before", job_id)

    print(f"\nplain UPDATE of event #{target.id} ...")
    try:
        raw_update(target.id, tampered, bypass_trigger=False)
        print("  !! update was NOT blocked")
    except DBAPIError as err:
        print(f"  blocked by trigger: {err.orig}".splitlines()[0])

    print(f"\nbypassing the trigger, changing event #{target.id} ({target.event_type}):")
    print(f"  {original}\n  -> {tampered}")
    raw_update(target.id, tampered, bypass_trigger=True)
    show("after tamper", job_id)

    if not args.keep:
        raw_update(target.id, original, bypass_trigger=True)
        show("after restore", job_id)


if __name__ == "__main__":
    main()
