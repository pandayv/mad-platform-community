"""Manual/demo entry point for the WCAG freshness-check loop
(mad_platform/agents/wcag_auto_heal.py) -- the same logic the real
Cloud Scheduler tick runs against scan-wcag-poller, runnable directly
without waiting up to a day for a real tick.

Usage:
    .venv/bin/python check_wcag_version.py                    # real check
    .venv/bin/python check_wcag_version.py --simulate 3.0      # demo: force a "major" version change
    .venv/bin/python check_wcag_version.py --simulate 2.9      # demo: force a "minor" version change
"""

from __future__ import annotations

import argparse
import asyncio

from mad_platform.agents.wcag_auto_heal import run_wcag_freshness_check


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the WCAG knowledge-base freshness check.")
    parser.add_argument(
        "--simulate",
        metavar="VERSION",
        help="Override the real W3C fetch with a fake 'current' version, to demo the change-detected branch on demand",
    )
    args = parser.parse_args()

    result = await run_wcag_freshness_check(simulate_current_version=args.simulate)

    print(f"Action: {result['action']}")
    if result["action"] == "no_change":
        print(f"Already up to date at WCAG {result['version']}.")
    elif result["action"] == "initialized":
        print(f"First run -- recorded current version as WCAG {result['version']}.")
    elif result["action"] == "auto_refreshed":
        print(f"WCAG {result['old_version']} -> {result['new_version']}: auto-refreshed (classified {result['change_type']}, confidence {result['confidence']:.2f}).")
        print(f"Reasoning: {result['reasoning']}")
        if result["change_type"] == "major":
            print("This is a structural/conformance-model shift -- the curated corpus content itself may need a manual update, not just re-embedding.")


if __name__ == "__main__":
    asyncio.run(main())
