"""Notifications: Slack (optional, kept from the hackathon build) + email
(the community fork's default, since a random small business owner almost
certainly doesn't have a Slack workspace waiting for this).

Slack: fire-and-forget webhook posts, two visually distinct flavors --
alert() for anything that needs a human to look now, summary() for a
completed scan's results. Unchanged from the original build; still
optional, still a no-op if SLACK_WEBHOOK_URL isn't set.

Email (send_report_email): the community fork's real delivery channel.
Sends a compact, email-safe summary (not the full report page -- see
reporter.draft_email_summary for why) plus a review-queue callout if
anything's pending, with the complete report attached as real files
(HTML + CSV) so nothing is lost, just laid out differently than the site.
Uses Resend (see RESEND_API_KEY); swap providers here only, nothing else
in the codebase should know which one is in use.

Both channels are best-effort: an outage or missing credentials must
never break the scan pipeline they're reporting on, failures are logged
and swallowed, never raised.
"""

from __future__ import annotations

import base64
import html
import logging
import os

import requests

logger = logging.getLogger("mad_platform.notify")

_TIMEOUT_S = 10
_ALERT_COLOR = "#B91C1C"
_SUMMARY_COLOR = "#2563EB"
_RESEND_API_URL = "https://api.resend.com/emails"
_FROM_ADDRESS = os.environ.get("MAD_EMAIL_FROM", "MAD Platform <scans@mad-platform.org>")


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


def send_verification_code_email(to_email: str, code: str) -> bool:
    """The one-time code a visitor types back in to verify they actually
    control the email they entered -- see firestore_client.generate_email_code
    for the code/expiry/attempt-limit logic this delivers.

    Unlike send_report_email, this is NOT best-effort: a report that fails
    to email is still viewable on the site, but a verification code that
    fails to send leaves the visitor completely stuck (no code, no way to
    ever get one for that attempt). Returns whether the send actually
    succeeded so the caller can show a real error and let them retry,
    instead of silently claiming "check your email" for a code that never
    left this process.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        logger.info("RESEND_API_KEY not set, cannot send verification code")
        return False

    body_html = f"""
<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:420px;margin:0 auto;padding:24px 16px">
  <div style="font-size:12px;letter-spacing:0.05em;text-transform:uppercase;color:#0B6E66;font-weight:700;margin-bottom:16px">MAD Platform</div>
  <p style="font-size:14px;color:#12181A;line-height:1.6">Your verification code:</p>
  <div style="font-family:'SF Mono',ui-monospace,monospace;font-size:32px;font-weight:800;letter-spacing:0.15em;color:#12181A;background:#F5F7F7;border-radius:10px;padding:18px 0;text-align:center;margin:16px 0">{html.escape(code)}</div>
  <p style="font-size:13px;color:#5B6B6A;line-height:1.6">Expires in 10 minutes. If you didn't request this, you can ignore this email.</p>
</div>
"""
    try:
        resp = requests.post(
            _RESEND_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": _FROM_ADDRESS,
                "to": [to_email],
                "subject": f"Your verification code: {code}",
                "html": body_html,
            },
            timeout=_TIMEOUT_S,
        )
        if not resp.ok:
            logger.error("Verification code email failed: %s %s", resp.status_code, resp.text)
            return False
        logger.info("Verification code sent to %s", to_email)
        return True
    except requests.RequestException:
        logger.exception("Verification code email failed")
        return False


def send_report_email(
    to_email: str,
    url: str,
    email_summary_html: str,
    review_lines: list[str] | None = None,
    review_url: str | None = None,
    attachments: list[tuple[str, bytes]] | None = None,
) -> None:
    """Delivered to whoever submitted the scan. email_summary_html is the
    compact, email-client-safe rendering from reporter.draft_email_summary
    -- deliberately NOT the same HTML as the web report (see that function's
    docstring: nesting the full standalone report page inside an email body
    is invalid HTML, and Gmail/most clients strip the inner <style>/<head>
    entirely, silently dropping all styling). review_lines, when given, is
    a short plain-language callout above the summary (what's pending, why),
    since a business owner who never opens the review link should still
    know something needs their attention. review_url is that owner's own
    scoped review-queue link (see firestore_client.verify_review_token) --
    without it, the callout still explains what's pending but has nothing
    to click. attachments carries the complete report as real files
    (filename, raw bytes) -- currently the full HTML report and the CSV
    ticket export -- so nothing from the full report is lost, it's just not
    crammed into the email body itself.

    Best-effort like the Slack functions above: missing RESEND_API_KEY or
    a delivery failure is logged and swallowed, never raised -- a report
    that failed to email is still saved and viewable on the site, this is
    a convenience channel, not the source of truth.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        logger.info("RESEND_API_KEY not set, skipping report email")
        return

    # url and review_lines both trace back to the site being scanned (the
    # submitted URL and page URLs discovered while crawling it) -- an
    # attacker's own site, so its content is untrusted the same way a
    # scanned page's HTML is. review_url is server-generated (job_id +
    # review_token), never derived from the scanned site, so it's safe to
    # drop into an href unescaped.
    review_block = ""
    if review_lines:
        heading = "1 finding needs your review" if len(review_lines) == 1 else f"{len(review_lines)} findings need your review"
        items = "".join(f"<li>{html.escape(line)}</li>" for line in review_lines)
        action = (
            f"<p><a href='{review_url}' style='font-weight:600'>Review {'this' if len(review_lines) == 1 else 'these'} now &rarr;</a></p>"
            if review_url
            else "<p>Confirm or dismiss from the report below.</p>"
        )
        review_block = (
            "<div style='background:#FFF7ED;border:1px solid #FDBA74;"
            "border-radius:8px;padding:16px;margin-top:24px'>"
            f"<strong>{heading}</strong>"
            f"<ul>{items}</ul>{action}"
            "</div>"
        )

    # review_block comes after the summary, not before -- the score and
    # findings are what someone opens this email to see first; a pending
    # review is a secondary action item, not the headline.
    body_html = f"<div style='padding:24px 16px'>{email_summary_html}{review_block}</div>"

    payload = {
        "from": _FROM_ADDRESS,
        "to": [to_email],
        "subject": f"Your accessibility scan is ready: {url}",
        "html": body_html,
    }
    if attachments:
        payload["attachments"] = [
            {"filename": filename, "content": base64.b64encode(content).decode("ascii")}
            for filename, content in attachments
        ]

    try:
        resp = requests.post(
            _RESEND_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=_TIMEOUT_S,
        )
        if not resp.ok:
            # raise_for_status()'s own exception message doesn't include the
            # response body, and Resend's actual reason (invalid key, domain
            # not verified, rate limit, etc.) only shows up there -- logging
            # it directly here beats trying to infer the cause from an HTTP
            # status code alone.
            logger.error("Report email failed: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()
        logger.info("Report email sent to %s", to_email)
    except Exception:
        logger.exception("Report email failed")
