"""Cheap, dependency-free checks that run before a /scan submission is
allowed to create a job and enter the queue.

Deliberately layered *before* firestore_client.check_and_reserve_scan_quota:
that function's per-email/per-IP/monthly counters are the budget gate, but
they still charge quota to a submission with a fake email or a nonsense
URL. These checks catch the cheap, obvious junk first, for free, so it
never reaches quota accounting or the queue at all.
"""

from __future__ import annotations

import re
import socket

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Domains that exist only to receive throwaway/burner mail. Not exhaustive —
# new disposable-email services appear constantly — but it catches the
# handful that show up in nearly every public-form abuse case, for zero
# ongoing cost. Not a substitute for the rate limits, a first filter.
_DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com",
    "guerrillamail.com",
    "10minutemail.com",
    "10minutemail.net",
    "tempmail.com",
    "temp-mail.org",
    "throwawaymail.com",
    "yopmail.com",
    "trashmail.com",
    "getnada.com",
    "sharklasers.com",
    "dispostable.com",
    "fakeinbox.com",
    "mailcatch.com",
    "mintemail.com",
}

# A legitimate visitor takes at least this long to load the page, read two
# fields, and click submit. A bot that fills and posts the form the instant
# it's fetched will trip this — cheaper than a CAPTCHA and invisible to
# real users.
MIN_FORM_FILL_SECONDS = 2.0


def email_looks_valid(email: str) -> tuple[bool, str]:
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        return False, "Please enter a valid email address."

    domain = email.rsplit("@", 1)[-1]
    if domain in _DISPOSABLE_EMAIL_DOMAINS:
        return False, "Please use a permanent email address — we can't deliver reports to disposable inboxes."

    try:
        socket.getaddrinfo(domain, None)
    except socket.gaierror:
        return False, "That email domain doesn't seem to exist — please double-check it."

    return True, ""
