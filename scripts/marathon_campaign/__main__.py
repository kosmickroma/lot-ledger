# scripts/marathon_campaign/__main__.py
#
# Role: CLI entrypoint for marathon campaign operations.
#
# Connects to:
#   scripts/marathon_campaign/runner.py - run/status command handlers

from __future__ import annotations

import argparse
import asyncio

from .runner import (
    default_runner_id,
    get_run_end_reason,
    operator_requeue_seed,
    operator_skip_seed,
    run_campaign,
    status_campaign,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Marathon campaign CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_parser = sub.add_parser("run", help="Run marathon campaign loop")
    run_parser.add_argument("--campaign", required=True, help="Campaign key")
    run_parser.add_argument("--runner-id", default=None, help="Override runner id")
    run_parser.add_argument("--mock-pull", action="store_true", help="Use fake pull jobs for smoke testing")
    run_parser.add_argument("--max-seeds", type=int, default=None, help="Max seeds to process before clean exit")

    status_parser = sub.add_parser("status", help="Show campaign FSM status counts")
    status_parser.add_argument("--campaign", required=True, help="Campaign key")

    skip_parser = sub.add_parser("skip", help="Mark a seed as skipped")
    skip_parser.add_argument("--seed-id", type=int, required=True, help="Seed id to skip")
    skip_parser.add_argument("--reason", default="", help="Operator reason for skipping")

    requeue_parser = sub.add_parser("requeue", help="Requeue a failed_final seed")
    requeue_parser.add_argument("--seed-id", type=int, required=True, help="Seed id to requeue")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.cmd == "run":
        runner_id = args.runner_id or default_runner_id()
        seeds_processed = asyncio.run(
            run_campaign(
                campaign_key=args.campaign,
                runner_id=runner_id,
                mock=bool(args.mock_pull),
                max_seeds=args.max_seeds,
            )
        )
        run_end_reason = get_run_end_reason() or "unknown"
        print(
            f"[marathon-runner] campaign={args.campaign} "
            f"run_end_reason={run_end_reason} seeds_processed={int(seeds_processed)}"
        )
        return

    if args.cmd == "status":
        status_campaign(args.campaign)
        return

    if args.cmd == "skip":
        operator_skip_seed(args.seed_id, reason=args.reason)
        print(f"[marathon-runner] seed_id={args.seed_id} transitioned_to=skipped")
        return

    if args.cmd == "requeue":
        operator_requeue_seed(args.seed_id)
        print(f"[marathon-runner] seed_id={args.seed_id} transitioned_to=queued attempts_reset=0")
        return

    raise ValueError(f"unsupported command: {args.cmd}")


if __name__ == "__main__":
    main()
