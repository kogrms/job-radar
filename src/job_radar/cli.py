"""Operator CLI: `job-radar steps status`, `job-radar steps retry-failed`, `job-radar backfill`."""

import argparse
import asyncio
import sys

from job_radar.config import get_settings
from job_radar.db import make_engine, make_sessionmaker
from job_radar.steps import ops
from job_radar.steps.chain import CHAIN


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-radar")
    sub = parser.add_subparsers(dest="command", required=True)

    steps = sub.add_parser("steps", help="inspect and nudge the step pipeline")
    steps_sub = steps.add_subparsers(dest="steps_command", required=True)
    steps_sub.add_parser("status", help="counts by step and status, stuck runs, oldest waiting_human")
    retry = steps_sub.add_parser("retry-failed", help="put failed runs of one step back to pending")
    retry.add_argument("--step", required=True, choices=[s.name for s in CHAIN])
    retry.add_argument("--limit", type=int, default=None)

    backfill = sub.add_parser(
        "backfill", help="release skipped/baseline rows of open vacancies into the pipeline"
    )
    backfill.add_argument("--step", required=True, choices=[s.name for s in CHAIN])
    backfill.add_argument("--limit", type=int, required=True)
    backfill.add_argument("--source", default=None)
    backfill.add_argument("--dry-run", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    sessionmaker = make_sessionmaker(engine)
    try:
        if args.command == "steps" and args.steps_command == "status":
            async with sessionmaker() as session:
                status = await ops.steps_status(session, lease_seconds=settings.step_lease_seconds)
            for step, state, n in status.counts:
                print(f"{step:24} {state:14} {n}")
            if not status.counts:
                print("no step runs")
            if status.stuck_running:
                print(f"ATTENTION: {status.stuck_running} runs 'running' for over 3 leases")
            if status.oldest_waiting_human_s is not None:
                print(f"oldest waiting_human: {status.oldest_waiting_human_s / 3600:.1f} h")
        elif args.command == "steps" and args.steps_command == "retry-failed":
            async with sessionmaker() as session, session.begin():
                n = await ops.retry_failed(session, args.step, limit=args.limit)
            print(f"requeued {n} failed runs of {args.step}")
        elif args.command == "backfill":
            async with sessionmaker() as session, session.begin():
                n = await ops.backfill(
                    session, args.step, limit=args.limit, source=args.source, dry_run=args.dry_run
                )
            verb = "would release" if args.dry_run else "released"
            print(f"{verb} {n} baseline rows of {args.step}")
            # LLM steps (week 2 PR 7): print the cost estimate here before releasing anything.
        return 0
    finally:
        await engine.dispose()


def main() -> None:
    sys.exit(asyncio.run(_run(_parser().parse_args())))


if __name__ == "__main__":
    main()
