"""Notifications: Slack (optional, kept from the hackathon build) + email
(the community fork's default, since a random small business owner almost
certainly doesn't have a Slack workspace waiting for this).

Slack: fire-and-forget webhook posts, two visually distinct flavors --
alert() for anything that needs a human to look now, summary() for a
completed scan's results. Unchanged from the original build; still
optional, still a no-op if SLACK_WEBHOOK_URL isn't set.

Email (send_report_email): the community fork's real delivery channel.
Sends the full rendered report, not just a link, plus a review-queue
summary if anything's pending -- a business owner who never comes back to
the site should still get everything. Uses Resend (see RESEND_API_KEY);
swap providers here only, nothing else in the codebase should know which
one is in use.

Both channels are best-effort: an outage or missing credentials must
never break the scan pipeline they're reporting on, failures are logged
and swallowed, never raised.
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger("mad_platform.notify")

_TIMEOUT_S = 10
_ALERT_COLOR = "#B91C1C"
_SUMMARY_COLOR = "#2563EB"
_RESEND_API_URL = "https://api.resend.com/emails"
_FROM_ADDRESS = os.environ.get("MAD_EMAIL_FROM", "MAD Platform <scans@madplatform.dev>")


def _post(payload: dict) -> None:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        return
    try:
        resp = requests.post(webhook_url, json=payload, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        logger.info("Slack notification posted")
    except Exception:
        logger.exception("Slack notification failed")


def _send(icon: str, color: str, title: str, lines: list[str]) -> None:
    text = f"{icon} *{title}*\n" + "\n".join(f"• {line}" for line in lines)
    _post({"attachments": [{"color": color, "text": text}]})


def alert(title: str, lines: list[str]) -> None:
    """Needs a human now -- an escalated finding or a KB version change
    awaiting review.
    """
    _send(":rotating_light:", _ALERT_COLOR, title, lines)


def summary(title: str, lines: list[str]) -> None:
    """A completed scan's results -- informational, not actionable on its
    own.
    """
    _send(":bar_chart:", _SUMMARY_COLOR, title, lines)


def send_report_email(
    to_email: str,
    url: str,
    report_html: str,
    review_lines: list[str] | None = None,
) -> None:
    """The full report, delivered to whoever submitted the scan. report_html
    is the same HTML already generated for the web report (see
    orchestrator.py's draft_report/storage_client.save_report) -- reused
    directly as the email body rather than a second template, so the two
    never drift apart. review_lines, when given, is rendered as a short
    plain-language summary block above the report (what's pending, why),
    since a business owner who never opens the review link should still
    know something needs their attention.

    Best-effort like the Slack functions above: missing RESEND_API_KEY or
    a delivery failure is logged and swallowed, never raised -- a report
    that failed to email is still saved and viewable on the site, this is
    a convenience channel, not the source of truth.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        logger.info("RESEND_API_KEY not set, skipping report email")
        return

    review_block = ""
    if review_lines:
        items = "".join(f"<li>{line}</li>" for line in review_lines)
        review_block = (
            "<div style='background:#FFF7ED;border:1px solid #FDBA74;"
            "border-radius:8px;padding:16px;margin-bottom:20px'>"
            "<strong>A few findings need your review</strong>"
            f"<ul>{items}</ul>"
            "<p>Use the private review link from your scan confirmation to confirm or dismiss these.</p>"
            "</div>"
        )

    body_html = f"<p>Your accessibility scan of {url} is complete.</p>{review_block}{report_html}"

    try:
        resp = requests.post(
            _RESEND_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": _FROM_ADDRESS,
                "to": [to_email],
                "subject": f"Your accessibility scan is ready: {url}",
                "html": body_html,
            },
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        logger.info("Report email sent to %s", to_email)
    except Exception:
        logger.exception("Report email failed")
