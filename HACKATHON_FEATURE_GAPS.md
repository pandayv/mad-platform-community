# Hackathon features not in the Community edition

The Community edition started as a copy of the hackathon build
(`github.com/pandayv/mad-platform`, last commit 2026-08-30). Some features
were left out on purpose because they fit a judged demo, not a free public
tool. This file lists each one: what it did, why Community runs without it,
and what it would take to bring it back.

The hackathon source stays in GitHub and in the local
`AllThingsAgentic_Hackathon` folder. Its Google Cloud project
(`project-d7e6174e-cca7-4d16-9d5`) was shut down on 2026-11-20. Nothing
below needs that project to come back.

Compared on 2026-11-20 from both codebases and `DECISIONS_LOG.md`.

## At a glance

| Feature | Hackathon | Community today | Effort to restore |
|---|---|---|---|
| Live Jira ticket filing | On when `JIRA_URL` set | CSV export (Jira-importer columns) | Small: code still present |
| Slack alerts | Configured | Code present, no webhook set | Small: set one secret |
| Per-finding "needs SME review" Slack alert | On | Removed | Small |
| Self-hosted Gemma pattern miner | Gemma 3 4B via Ollama | Gemini Flash | Medium |
| Human gate on major WCAG knowledge-base changes | Admin approves | Auto-refresh, FYI alert only | Small to medium |
| Access-code gate on the scan form | On | Removed | Small |
| One admin review queue for everything | Yes | Split by who can decide | n/a (Community's design is the better fit) |

## 1. Live Jira ticket filing

**What it did.** Every confirmed finding became a real Jira issue during
the scan. The report showed a "Filed: KEY-123" badge for each one.

**Why Community dropped it.** A free tool has no Jira site to file into
for each visitor. Community exports a CSV with the columns Jira's importer
expects, so an owner can import it into their own tracker.

**Where it stands.** `JiraIssueSink` in `mad_platform/tools/issue_sink.py`
matches the hackathon version apart from its docstring. What's missing is
the wiring, since `_issue_sink()` in `mad_platform/web/app.py` and the
worker in `mad_platform/web/worker_app.py` always return `CsvIssueSink`.

**To bring it back.**
- Pick the sink from config again, the way the hackathon did, by using
  `JiraIssueSink()` when `JIRA_URL` is set and the default otherwise.
- Add the four secrets (`jira-url`, `jira-email`, `jira-api-token`,
  `jira-project-key`) and pass them to scan-worker as env vars.
- The real design question is whose Jira gets the tickets. A single shared
  Jira only makes sense for a hosted or paid tier. A per-owner Jira needs
  per-user credentials, which Community doesn't store.

## 2. Slack alerts

**What it did.** It sent a real-time alert to a private channel for
high-stakes events, plus a summary when each scan finished.

**Where it stands.** `notify.alert()` and `notify.summary()` are still in
Community and still called from the orchestrator, the WCAG auto-heal and
the pattern miner. They do nothing because `SLACK_WEBHOOK_URL` isn't set.

**To bring it back.** Create a Slack incoming webhook, store it as the
`slack-webhook-url` secret, and pass it as `SLACK_WEBHOOK_URL` to the
services that send alerts.

## 3. Per-finding "needs SME review" alert

**What it did.** When a finding was routed to human review,
`action_agent.py` posted a Slack alert to page the admin.

**Why Community dropped it.** Findings now go to the scan's own owner
through a private review link in the report and the report email. Paging
an admin for something the owner already sees adds noise.

**To bring it back.** Restore the `notify.alert(...)` call in
`action_agent.route_and_file()`. Worth it only if a staffed review team
exists.

## 4. Self-hosted Gemma pattern miner

**What it did.** A weekly job ran Gemma 3 4B locally through Ollama, baked
into the container image, to find patterns in the findings Editor
dismissed. The goal was a second, self-hosted model for batch work.

**Why Community dropped it.** It existed for the hackathon's bonus
criterion (show a self-hosted, non-Gemini model). Call volume is low, and a
wrong pattern quietly changes what every later scan flags, so Community
treats it as a judgment call for Gemini Flash. That also cut the job from
4 GiB and 4 CPUs to 512 MiB and 1 CPU, and the build from about 9 minutes
to about 1.

**Where the code lives.** `mad_platform/tools/gemma_client.py`,
`pattern_miner_entrypoint.sh` and `Dockerfile.pattern_miner` in the
hackathon repo.

**To bring it back.** Restore those three files and switch
`pattern_miner.py` back to `gemma_client`. Expect higher job cost. This
makes sense if there's a reason to keep this data off a hosted model.

## 5. Human gate on major WCAG knowledge-base changes

**What it did.** When the daily WCAG check saw a major version change, it
opened a `kb_version_change` escalation. An admin approved it before the
knowledge base refreshed (`resolve_kb_escalation()` in
`wcag_auto_heal.py`, shown in the `/review` queue and in
`review_escalations.py`).

**Why Community dropped it.** The approve button called the same
`embed_and_store_corpus()` as the automatic path, so the gate protected
nothing. Community always auto-refreshes and sends an FYI alert on a major
change.

**To bring it back.** It's only useful if approval comes with a real step,
such as an expert editing the curated corpus in `mad_platform/data/` for a
new conformance model. Restore the escalation, its handler and its review
renderers together.

## 6. Access-code gate on the scan form

**What it did.** The public scan form needed a shared code
(`MAD_ACCESS_CODE`, secret `mad-ui-access-code`) to protect a demo's
limited credits.

**Why Community dropped it.** Community is meant to be public and
self-serve. It handles abuse with email verification, per-email and
per-IP daily quotas, a monthly cap, a honeypot and timing checks, and safe
URL validation.

**To bring it back.** It's a small check in front of `/scan/start`. Worth
it for a private beta or a closed pilot.

## Not a gap

- **Running scans inside the public app.** The hackathon ran each scan in
  the request handler (`POST /scan`). Community queues it in Cloud Tasks
  and runs it on `scan-worker`. That's an upgrade.
- **Recurring monitoring.** It was on the hackathon roadmap and isn't
  built in either edition. The roadmap named two problems to solve first:
  filing tickets once per site across repeated runs, not once per scan,
  and teaching the review queue a site's own review history so the same
  confirmed pattern doesn't re-escalate on every run.
