"""IssueSink: the ticketing abstraction Action Agent files through.

The filing logic sits behind this interface so an additional tracker
could be added later without changing Orchestrator or Reporter.

**CsvIssueSink is the real implementation.** Every scan the deployed
services run files through it (web/app.py and web/worker_app.py both
construct one, and nothing offers a choice), because the community fork's
whole point is that a visitor needs no ticket-tracker credentials --
DECISIONS_LOG.md records that decision.

This docstring used to say "JiraIssueSink is the real implementation",
which was true of the original build and has been wrong since the fork.
That is not a cosmetic error: a reader forms a model of how ticketing
works from exactly this line, and orchestrator.py was still logging
"Jira ticket filed" for a CSV row on the strength of the same
misunderstanding. JiraIssueSink is now legacy -- kept because
review_escalations.py can still opt into it when JIRA_URL is set, not
because anything in the deployed path uses it. MockIssueSink is for tests
and for run_scan.py's default.
"""

from __future__ import annotations

import csv
import io
import os
from abc import ABC, abstractmethod

import requests


class IssueSink(ABC):
    @abstractmethod
    def create_issue(self, title: str, description: str) -> str:
        """Creates an issue, returns an external ticket ID/URL."""


class JiraIssueSink(IssueSink):
    """Legacy, and not on any deployed path -- see the module docstring.
    Reachable only from review_escalations.py when JIRA_URL is set.
    """

    def __init__(self) -> None:
        self.base_url = os.environ["JIRA_URL"].rstrip("/")
        self.email = os.environ["JIRA_EMAIL"]
        self.api_token = os.environ["JIRA_API_TOKEN"]
        self.project_key = os.environ["JIRA_PROJECT_KEY"]

    def create_issue(self, title: str, description: str) -> str:
        resp = requests.post(
            f"{self.base_url}/rest/api/2/issue",
            auth=(self.email, self.api_token),
            json={
                "fields": {
                    "project": {"key": self.project_key},
                    "summary": title,
                    "description": description,
                    "issuetype": {"name": "Task"},
                }
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["key"]


class CsvIssueSink(IssueSink):
    """The community fork's default: no ticket-tracker credentials needed
    from the visitor at all. Rows accumulate in memory across a scan, then
    export() hands back a CSV any tracker's bulk importer can read,
    Jira's CSV importer maps "Summary"/"Description" columns directly, no
    plugin or API access required on the user's side.

    create_issue()'s return value is used purely as an opaque idempotency
    ID today (see firestore_client.record_ticket_for_finding) -- nothing
    parses it as a real ticket key -- so a synthetic id here is a clean
    drop-in, not a special case downstream.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []

    def create_issue(self, title: str, description: str) -> str:
        issue_id = f"CSV-{len(self.rows) + 1}"
        self.rows.append({"id": issue_id, "Summary": title, "Description": description})
        return issue_id

    def export(self) -> str:
        """Returns the accumulated rows as CSV text, ready for download.
        Called once per completed scan, after route_and_file finishes --
        there's no batch/finalize hook on the IssueSink interface itself,
        callers just hold the sink instance and call export() directly.
        """
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=["Summary", "Description"])
        writer.writeheader()
        for row in self.rows:
            writer.writerow({"Summary": _defuse_formula(row["Summary"]), "Description": _defuse_formula(row["Description"])})
        return buffer.getvalue()


def _defuse_formula(value: str) -> str:
    """Summary/Description ultimately trace back to scanned page content --
    an attacker's own site, same as anywhere else scanned HTML flows into
    this app. A cell starting with =, +, -, or @ is executed as a formula
    by Excel/Sheets on open ("CSV/formula injection") -- a leading
    apostrophe forces text interpretation without changing what's visibly
    displayed.
    """
    if value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


class MockIssueSink(IssueSink):
    """For testing without live Jira credentials -- keeps created issues
    in memory so tests can assert against them.
    """

    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []

    def create_issue(self, title: str, description: str) -> str:
        ticket_id = f"MOCK-{len(self.created) + 1}"
        self.created.append((ticket_id, title, description))
        return ticket_id
