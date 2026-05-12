# scripts/marathon_campaign/__main__.py
#
# Role: CLI entrypoint for marathon campaign operations.
#
# Connects to:
#   scripts/marathon_campaign/runner.py - run/status command handlers

from __future__ import annotations

import argparse
import asyncio

from .runner import default_runner_id, run_campaign, status_campaign


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

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.cmd == "run":
        runner_id = args.runner_id or default_runner_id()
        asyncio.run(
            run_campaign(
                campaign_key=args.campaign,
                runner_id=runner_id,
                mock=bool(args.mock_pull),
                max_seeds=args.max_seeds,
            )
        )
        return

    if args.cmd == "status":
        status_campaign(args.campaign)
        return

    raise ValueError(f"unsupported command: {args.cmd}")


if __name__ == "__main__":
    main()
