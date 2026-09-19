"""Manual/demo entry point for the WCAG freshness-check loop
(mad_platform/agents/wcag_auto_heal.py) -- the same logic the real
Cloud Scheduler tick runs against scan-wcag-poller, runnable directly
without waiting up to a day for a real tick.

Usage:
    .venv/bin/python check_wcag_version.py                        # real check
    .venv/bin/python check_wcag_version.py --force-version 3.0    # force a "major" change, FOR REAL
    .venv/bin/python check_wcag_version.py --force-version 2.9    # force a "minor" change, FOR REAL

`--force-version` was called `--simulate`, and the name was a lie. It does
not simulate anything: past the version comparison it runs the real
refresh -- 19 live Gemini embedding calls, a real Firestore write per
criterion, and `fs.set_kb_version(<whatever you typed>)` against the
production database. `--simulate 3.0` left the production knowledge base
permanently recorded as version 3.0, after which the next real tick read
stored == "3.0", compared it against the real fetch, and behaved as if the
standard had gone backwards. "Simulate" reads as a dry run; the behaviour
was an unreversible state change on a shared resource, which is the worst
possible pairing.

The flag is kept -- demonstrating the change-detected branch on demand is
genuinely useful, and a real WCAG version change is rare -- but it now says
what it does, prints what it is about to do, and asks first.
"""

from __future__ import annotations

import argparse
import asyncio

from mad_platform.agents.wcag_auto_heal import run_wcag_freshness_check


def _confirm(version: str, assume_yes: bool) -> bool:
    print(
        f"--force-version {version} will run the REAL refresh against the production\n"
        f"knowledge base: re-embed every criterion in mad_platform/data/wcag_corpus.py\n"
        f"(live Gemini calls plus a Firestore write each) and permanently record\n"
        f"{version!r} as the stored knowledge-base version.\n"
        f"\n"
        f"That is not reversible from here: the next real check will compare against\n"
        f"{version!r} and behave accordingly.\n"
    )
    if assume_yes:
        print("--yes given, proceeding.\n")
        return True
    return input("Type the version again to confirm: ").strip() == version


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run the WCAG knowledge-base freshness check.")
    parser.add_argument(
        "--force-version",
        metavar="VERSION",
        help=(
            "Treat VERSION as the current WCAG version instead of fetching it from W3C. "
            "NOT a dry run: this performs the real re-embed and permanently records VERSION "
            "as the stored knowledge-base version. Asks for confirmation first."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt for --force-version (for non-interactive use).",
    )
    args = parser.parse_args()

    if args.force_version and not _confirm(args.force_version, args.yes):
        print("Aborted -- nothing was written.")
        return

    result = await run_wcag_freshness_check(simulate_current_version=args.force_version)

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
