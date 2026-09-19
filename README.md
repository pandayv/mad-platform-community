# MAD Platform — Community Edition

**Multi-Agent Defense Platform, for accessibility compliance.** A free tool
that scans a website for accessibility problems, checks its own findings
before showing them to you, and gives you a concrete fix for each one, not
just a report. No account needed.

Originally built for the [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/)
on Gemini, Google's Agent Development Kit (ADK), and Google Cloud; this is
the ongoing community fork, running as a free public tool. The
architecture has moved on since the hackathon submission — most notably,
scanning now runs on a separate queue/worker service rather than
in-process — so this document describes what's actually deployed today,
not the original submission.

---

## Try it

**[mad-platform.org](https://mad-platform.org)** is the public-facing
website and our main product: paste in a URL, give an email address to
receive the report, and watch it scan. No account, no access code, free.

**[Architecture diagram](https://pandayv.github.io/mad-platform-community/):**
the full pipeline and the Google Cloud infrastructure behind it.

### What to expect

1. Submit any real URL on the website above.
2. Watch the status page track live progress. A multi-page scan usually
   takes one to three minutes. If there's a burst of traffic ahead of
   you, you'll see a queued state first — scans process one at a time per
   worker instance, and the page tells you it's safe to close the tab.
3. On the completed report, every confirmed finding is listed with a
   suggested fix and a status. Anything flagged "Awaiting internal
   review" is a low-confidence or critical finding a human hasn't
   confirmed yet.
4. The report and a CSV of confirmed findings (in Jira's importer column
   format, so it drops straight into a real ticket tracker if you have
   one) get emailed to the address the scan was submitted with.

## The problem

Website-accessibility lawsuits (ADA-related, in the US) are a real and
growing risk for small businesses, most of whom have no practical way to
know they're exposed. Manual accessibility audits are expensive and slow.
Automated scanners exist, but they're noisy (full of false positives a
non-technical business owner can't triage), and a report alone doesn't fix
anything; someone still has to turn it into work that gets done.

MAD Platform removes that blind spot: point it at a URL, and it finds real
issues, checks its own work before trusting it, explains what matters most
in plain language, and hands you the confirmed ones ready to act on,
while routing the genuinely uncertain ones to a human instead of guessing.

## What it does

1. **Scans a site.** Decides which pages matter most on its own (home,
   contact, forms), then checks them with both deterministic rule checks
   (contrast, missing alt text, heading structure, form labels, ARIA
   misuse, tab order) and AI-assisted review for what rules can't judge,
   like whether alt text is actually descriptive.
2. **Verifies its own findings.** Every flag is independently
   double-checked before it's trusted; false positives get dismissed with
   a documented reason, real findings get a confidence score. Anything
   still uncertain goes to a human reviewer instead of guessing.
3. **Ranks by real-world risk**, not raw technical severity: WCAG
   conformance level, how often that violation type shows up in real
   accessibility litigation, and estimated user impact.
4. **Produces an actionable report:** a styled, self-contained HTML
   report with an overall score, severity breakdown, plain-English
   executive summary, and a concrete suggested fix per finding.
5. **Takes real action.** Exports every confirmed finding as a CSV in
   Jira's importer column format and emails the full report.
6. **Recovers from failure.** A scan interrupted mid-way (crash, redeploy,
   a queue retry) resumes from its last completed checkpoint rather than
   starting over or silently duplicating work.
7. **Keeps its WCAG reference current.** Checks whether the accessibility
   standard itself has changed, on a schedule.
8. **Asks how it did.** A short, open feedback form (star rating,
   comment, optional testimonial opt-in) reachable from the completed
   scan, the report, the report email, and the FAQ alike.

## What a scan looks like

Paste a URL into the web app:

![MAD Platform homepage: hero scan form, community-edition badge](assets/screenshot-homepage-hero.png)

Watch it work, with live phase labels and a per-page checklist:

![Scan in progress: analyzing pages for accessibility issues](assets/screenshot-progress.png)

When it's done, you get a score, a severity breakdown, and a ranked list
of findings with suggested fixes:

![Completed scan result: site score, severity breakdown, executive summary](assets/screenshot-completed.png)

The homepage also lays out how the free tool stacks up against other
scanners, backed by what's actually checked:

![How we compare: MAD Platform vs. free scanners vs. paid audit tools](assets/screenshot-how-we-compare.png)

## Tech stack

- **AI:** Gemini via Vertex AI for every call, real-time or batch
  (`gemini-3.5-flash-lite` for high-volume calls, `gemini-3.7-flash` for
  judgment calls, `gemini-embedding-001` for retrieval)
- **Agent framework:** Google Agent Development Kit (ADK)
- **Compute:** Cloud Run, three scale-to-zero services split by trigger
  type and resource profile —
  - `scan-onboarding`: the public web app. Thin and cheap (1Gi memory,
    concurrency 20, no browser automation), since it only ever enqueues
    work, never runs it.
  - `scan-worker`: not publicly reachable, only Cloud Tasks' dedicated
    invoker identity can call it. Runs the actual pipeline (Playwright,
    Gemini, one scan at a time per instance — 2Gi memory,
    `containerConcurrency=1`).
  - `scan-wcag-poller`: not publicly reachable either, ticked daily by
    Cloud Scheduler to check for WCAG standard updates.

  plus one lightweight Cloud Run Job (`pattern-miner`) for a weekly
  pattern-mining task.
- **Queueing:** Cloud Tasks (`scan-queue`) sits between `scan-onboarding`
  and `scan-worker` — submitting a scan enqueues a task rather than
  running the pipeline in the request handler, so a burst of traffic
  queues instead of falling over, and a scan that dies mid-run gets
  retried without the visitor having to resubmit anything.
- **State:** Firestore, for job checkpoints, findings, escalation queue,
  WCAG knowledge-base embeddings, confirmed learned patterns, and
  anti-abuse quota counters (with a TTL policy on the short-lived ones)
- **Storage:** Cloud Storage, for generated reports
- **Scheduling:** Cloud Scheduler, driving the WCAG freshness check
  (daily) and the pattern miner (weekly)
- **Browser automation:** Playwright, for headless rendering, screenshots,
  and computed-style extraction for real contrast-ratio checking — this
  is the one dependency that lives only in `scan-worker`'s image
- **Web:** FastAPI, powering the scan-submission UI, status API, and
  website's FAQ/legal pages
- **Anti-abuse:** Cloudflare Turnstile (opt-in, off unless a site key is
  configured) plus disposable-email filtering and per-email/per-IP/global
  monthly quota
- **Ticketing:** CSV export by default, in Jira's importer column format,
  so confirmed findings drop straight into a real tracker with no account
  needed; a real Jira integration also exists in code as an opt-in for
  anyone self-hosting this with their own Jira Cloud instance
- **Notifications:** Email via Resend, sending the full report to the
  address a scan was submitted with; Slack (an incoming-webhook alert on
  escalation, a summary on completion) exists in code as an opt-in,
  neither is required
- **Security:** the crawler refuses to fetch private/internal network
  addresses; an access-code gate (Secret Manager) on the internal review
  queue exists and fails closed if unconfigured, for anyone who wants to
  run a private instance instead of a public one
- **Testing:** pytest, 400+ tests covering the pure logic, the LLM-output
  validation boundary, and the FastAPI routes that don't need live GCP —
  see [Running the tests](#running-the-tests)

## Setting this up yourself

### What you need

- A Google Cloud project with billing enabled.
- The `gcloud` CLI, installed and authenticated (`gcloud auth login`).
- Python 3.13+ locally, matching the version the container images run.
- Optional, for real email delivery: a free [Resend](https://resend.com)
  account and API key. Without it, reports still generate and display in
  the browser; only the emailed copy is skipped.
- Optional, for bot mitigation on the public scan form: a
  [Cloudflare Turnstile](https://developers.cloudflare.com/turnstile/)
  site key and secret. Without them, the widget simply doesn't render —
  it's not required to run.
- Optional, for a private instance instead of a public one: a review
  access code (Secret Manager), and/or a real Jira Cloud account and
  Slack workspace instead of the default CSV export.

**IMPORTANT:** Replace `YOUR_PROJECT_ID` below with your actual GCP project
ID, the only value you need to choose here.

```bash
export PROJECT_ID=YOUR_PROJECT_ID
export GOOGLE_CLOUD_PROJECT="$PROJECT_ID"
export GCS_BUCKET_NAME="${PROJECT_ID}-reports"
gcloud config set project "$PROJECT_ID"
```

Every value this codebase actually reads from the environment lives in
`mad_platform/config.py`, with no fallback default for anything that
names a cloud resource — an unset variable fails loudly at startup
instead of silently pointing at the wrong project. That module is the
source of truth for exact variable names if this guide ever drifts from
the code again.

### The fast path: one script

[Use this template](https://github.com/new?template_name=mad-platform-community&template_owner=pandayv)
to get your own copy of this repo (GitHub keeps a permanent "generated
from pandayv/mad-platform-community" link on it), then clone your copy
and run:

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export PROJECT_ID=YOUR_PROJECT_ID
./setup.sh
```

`setup.sh` runs every gcloud command in steps 2 through 10 below, in
order, idempotently (safe to re-run after a partial failure — it checks
whether each resource exists before creating it). It ends with a working
public instance at your own Cloud Run URL.

The numbered steps below are what `setup.sh` automates — read them if you
want to understand what it's doing, customize a step, or run things by
hand instead.

### 1. Clone and set up the local environment

```bash
git clone https://github.com/pandayv/mad-platform-community.git
cd mad-platform-community
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
```

### 2. Enable the APIs this project actually uses

```bash
gcloud services enable \
  run.googleapis.com firestore.googleapis.com secretmanager.googleapis.com \
  storage.googleapis.com aiplatform.googleapis.com cloudscheduler.googleapis.com \
  cloudtasks.googleapis.com cloudbuild.googleapis.com
```

### 3. Create Firestore and a Cloud Storage bucket

```bash
gcloud firestore databases create --database=scan-firestore \
  --location=us-central1 --type=firestore-native
gcloud storage buckets create "gs://${PROJECT_ID}-reports" --location=us-central1
```

The Firestore database name is non-default (`scan-firestore`) on purpose,
so every Firestore client in this codebase passes `database="scan-firestore"`
explicitly. Easy to forget if you're used to the client library's default;
connects to an empty database if missed.

Then turn on the retention the privacy page describes. **This is not
optional if you are running this publicly** — the code writes an
`expires_at` timestamp on every record, but Firestore only acts on it
when a TTL policy exists on that field, and the report HTML in Cloud
Storage has no expiry of its own at all:

```bash
for c in scan_jobs escalations feedback usage_counters \
         email_verifications verified_devices; do
  gcloud firestore fields ttls update expires_at \
    --collection-group="$c" --database=scan-firestore --enable-ttl --async
done

cat > /tmp/reports-lifecycle.json <<'JSON'
{"lifecycle": {"rule": [
  {"action": {"type": "Delete"},
   "condition": {"age": 365, "matchesPrefix": ["reports/"]}}
]}}
JSON
gcloud storage buckets update "gs://${PROJECT_ID}-reports" \
  --lifecycle-file=/tmp/reports-lifecycle.json
```

The 365 days matches `firestore_client.SCAN_RECORD_RETENTION_DAYS` — change
them together. `setup.sh` runs all of this for you.

### 4. Authenticate locally and confirm Vertex AI works

```bash
gcloud auth application-default login
```

Model availability varies by project; confirm what's actually there
before assuming a model name works:

```bash
python -c "from google import genai; c = genai.Client(vertexai=True, project='$PROJECT_ID', location='global'); [print(m.name) for m in c.models.list()]"
```

The client location must be `global`, not a region like `us-central1`;
some models list in a region's catalog but 404 when actually called
there. This is independent of which region Cloud Run itself deploys to.

### 5. Test the pipeline locally, before deploying anything

```bash
export MAD_APP_BASE_URL="http://localhost:8000"
python run_scan.py https://example.com
```

This exercises the real pipeline end to end against your real GCP
project (Vertex AI, Firestore) with the default CSV ticket sink, no
Cloud Run deployment needed yet. Confirms steps 2-4 actually worked
before you spend time deploying.

### 6. Create an Artifact Registry repo for the container images

```bash
gcloud artifacts repositories create mad-platform \
  --repository-format=docker --location=us-central1

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
```

### 7. Deploy `scan-worker` and its `scan-queue` (deploy this before `scan-onboarding`)

`scan-onboarding` enqueues into `scan-queue` and needs to know
`scan-worker`'s URL and invoker identity at deploy time, so this has to
exist first.

```bash
gcloud iam service-accounts create scan-worker-sa
gcloud iam service-accounts create scan-queue-invoker-sa
SA_WORKER="scan-worker-sa@${PROJECT_ID}.iam.gserviceaccount.com"
SA_QUEUE_INVOKER="scan-queue-invoker-sa@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_WORKER}" --role="roles/datastore.user"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_WORKER}" --role="roles/aiplatform.user"
gcloud storage buckets add-iam-policy-binding "gs://${PROJECT_ID}-reports" \
  --member="serviceAccount:${SA_WORKER}" --role="roles/storage.objectAdmin"

gcloud builds submit --config=cloudbuild.worker.yaml --region=us-central1 \
  --substitutions=_IMAGE="us-central1-docker.pkg.dev/${PROJECT_ID}/mad-platform/scan-worker:latest" .
gcloud run deploy scan-worker \
  --image="us-central1-docker.pkg.dev/${PROJECT_ID}/mad-platform/scan-worker:latest" \
  --region=us-central1 --service-account="$SA_WORKER" \
  --memory=2Gi --cpu=1 --no-cpu-throttling --concurrency=1 --max-instances=5 \
  --set-env-vars=GCS_BUCKET_NAME="${PROJECT_ID}-reports",GOOGLE_CLOUD_PROJECT="${PROJECT_ID}",MAD_APP_BASE_URL="https://YOUR-DOMAIN-OR-CLOUD-RUN-URL" \
  --no-allow-unauthenticated

gcloud run services add-iam-policy-binding scan-worker --region=us-central1 \
  --member="serviceAccount:${SA_QUEUE_INVOKER}" --role="roles/run.invoker"

gcloud tasks queues create scan-queue --location=us-central1 \
  --max-concurrent-dispatches=5 --max-attempts=3

WORKER_URL=$(gcloud run services describe scan-worker --region=us-central1 --format='value(status.url)')
echo "Set SCAN_WORKER_URL=${WORKER_URL} for the scan-onboarding deploy below."
```

`MAD_APP_BASE_URL` is the public URL report and review links get built
from — set it to `scan-onboarding`'s URL once you know it (you can
redeploy this service to fix it up after step 8, it's read at request
time via `mad_platform/config.py`, not baked in).

### 8. Deploy `scan-onboarding` (the public app)

```bash
gcloud iam service-accounts create scan-onboarding-sa
SA_ONBOARDING="scan-onboarding-sa@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_ONBOARDING}" --role="roles/datastore.user"
gcloud storage buckets add-iam-policy-binding "gs://${PROJECT_ID}-reports" \
  --member="serviceAccount:${SA_ONBOARDING}" --role="roles/storage.objectViewer"
# Cloud Tasks needs the enqueuing identity to be allowed to "act as" the
# invoker service account it puts in each task's OIDC config:
gcloud iam service-accounts add-iam-policy-binding "$SA_QUEUE_INVOKER" \
  --member="serviceAccount:${SA_ONBOARDING}" --role="roles/iam.serviceAccountUser"

gcloud builds submit --tag="us-central1-docker.pkg.dev/${PROJECT_ID}/mad-platform/scan-onboarding" \
  --region=us-central1 .
gcloud run deploy scan-onboarding \
  --image="us-central1-docker.pkg.dev/${PROJECT_ID}/mad-platform/scan-onboarding:latest" \
  --region=us-central1 --service-account="$SA_ONBOARDING" \
  --no-cpu-throttling --memory=1Gi --concurrency=20 --max-instances=3 --min-instances=0 \
  --set-env-vars=GCS_BUCKET_NAME="${PROJECT_ID}-reports",GOOGLE_CLOUD_PROJECT="${PROJECT_ID}",MAD_APP_BASE_URL="https://YOUR-DOMAIN-OR-CLOUD-RUN-URL",SCAN_WORKER_URL="${WORKER_URL}",SCAN_QUEUE_INVOKER_SA="${SA_QUEUE_INVOKER}" \
  --allow-unauthenticated
```

No review access code is required by default (`MAD_REVIEW_CODE` unset) —
this deploys a fully public, free instance, matching the live one above,
and the internal review queue simply refuses everyone until you configure
one (it fails closed, not open). If you want a private review queue, set
`MAD_REVIEW_CODE` as a Secret Manager secret and pass it with
`--set-secrets` (see `mad_platform/config.py`'s `review_code()` for the
exact variable name).

### 9. Deploy `scan-wcag-poller` and its daily-freshness Scheduler trigger

Not public. Only a dedicated invoker identity, not the poller's own
account, can call it, so a compromised poller can't grant itself more
access than it started with.

```bash
gcloud iam service-accounts create scan-wcag-poller-sa
gcloud iam service-accounts create scan-scheduler-invoker-sa
SA_WCAG="scan-wcag-poller-sa@${PROJECT_ID}.iam.gserviceaccount.com"
SA_SCHEDULER="scan-scheduler-invoker-sa@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_WCAG}" --role="roles/datastore.user"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_WCAG}" --role="roles/aiplatform.user"

gcloud builds submit --config=cloudbuild.wcag_poller.yaml --region=us-central1 \
  --substitutions=_IMAGE="us-central1-docker.pkg.dev/${PROJECT_ID}/mad-platform/scan-wcag-poller:latest" .
gcloud run deploy scan-wcag-poller \
  --image="us-central1-docker.pkg.dev/${PROJECT_ID}/mad-platform/scan-wcag-poller:latest" \
  --region=us-central1 --service-account="$SA_WCAG" --memory=512Mi --max-instances=1 \
  --set-env-vars=GOOGLE_CLOUD_PROJECT="${PROJECT_ID}" \
  --no-allow-unauthenticated

gcloud run services add-iam-policy-binding scan-wcag-poller --region=us-central1 \
  --member="serviceAccount:${SA_SCHEDULER}" --role="roles/run.invoker"

WCAG_URL=$(gcloud run services describe scan-wcag-poller --region=us-central1 --format='value(status.url)')
gcloud scheduler jobs create http scan-wcag-poller-tick \
  --location=us-central1 --schedule="0 4 * * *" --uri="$WCAG_URL" \
  --http-method=POST --oidc-service-account-email="$SA_SCHEDULER"
```

### 10. Deploy the pattern-miner (a Cloud Run Job, not a Service)

Run-to-completion rather than request-driven, since this is a periodic
batch job with no live-request latency to protect. Calls Gemini Flash via
Vertex AI, the same model tier as the pipeline's other judgment calls --
no self-hosted model, no separate image-bake step, just a small Python
image like `scan-wcag-poller`'s.

```bash
gcloud iam service-accounts create pattern-miner-sa
SA_MINER="pattern-miner-sa@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_MINER}" --role="roles/datastore.user"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA_MINER}" --role="roles/aiplatform.user"

gcloud builds submit --config=cloudbuild.pattern_miner.yaml --region=us-central1 \
  --substitutions=_IMAGE="us-central1-docker.pkg.dev/${PROJECT_ID}/mad-platform/pattern-miner:latest" .
gcloud run jobs create pattern-miner \
  --image="us-central1-docker.pkg.dev/${PROJECT_ID}/mad-platform/pattern-miner:latest" \
  --region=us-central1 --service-account="$SA_MINER" \
  --memory=512Mi --cpu=1 --task-timeout=300 --max-retries=0 \
  --set-env-vars=GOOGLE_CLOUD_PROJECT="${PROJECT_ID}"

gcloud run jobs add-iam-policy-binding pattern-miner --region=us-central1 \
  --member="serviceAccount:${SA_SCHEDULER}" --role="roles/run.invoker"

# Weekly: dismissal history accumulates slowly relative to scan volume.
gcloud scheduler jobs create http pattern-miner-tick \
  --location=us-central1 --schedule="0 3 * * 0" \
  --uri="https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/pattern-miner:run" \
  --http-method=POST --oauth-service-account-email="$SA_SCHEDULER" \
  --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
```

To see it run immediately rather than waiting for the schedule:
`gcloud run jobs execute pattern-miner --region=us-central1 --wait`.

### 11. Optional: real email delivery, bot mitigation, and swapping CSV for Jira/Slack

Email, via [Resend](https://resend.com) — without this, reports still
generate and display in the browser, only the emailed copy is skipped.

```bash
printf '%s' "YOUR_RESEND_API_KEY" | gcloud secrets create resend-api-key --data-file=-
```

Cloudflare Turnstile, to put a bot challenge in front of the scan form —
without this, the form simply has no challenge:

```bash
printf '%s' "YOUR_TURNSTILE_SECRET_KEY" | gcloud secrets create turnstile-secret-key --data-file=-
# TURNSTILE_SITE_KEY is not secret -- it's fine as a plain env var.
```

Jira, for real ticket filing instead of the default CSV export. Create an
API token at `id.atlassian.com/manage-profile/security/api-tokens`, then:

```bash
printf '%s' "https://YOUR-SITE.atlassian.net" | gcloud secrets create jira-url --data-file=-
printf '%s' "YOUR_JIRA_EMAIL" | gcloud secrets create jira-email --data-file=-
printf '%s' "YOUR_API_TOKEN" | gcloud secrets create jira-api-token --data-file=-
printf '%s' "YOUR_PROJECT_KEY" | gcloud secrets create jira-project-key --data-file=-
```

Slack, for real-time alerts and scan-complete summaries alongside email.
Create an Incoming Webhook at `api.slack.com/apps` (your app, then
Incoming Webhooks), then:

```bash
printf '%s' "https://hooks.slack.com/services/YOUR/WEBHOOK/URL" | \
  gcloud secrets create slack-webhook-url --data-file=-
```

Grant access and redeploy `scan-onboarding` with the new secrets (Jira
and Slack are only ever called from `scan-worker`, since that's where the
pipeline actually runs — grant those two secrets to `scan-worker-sa`
instead):

```bash
for secret in resend-api-key turnstile-secret-key; do
  gcloud secrets add-iam-policy-binding "$secret" \
    --member="serviceAccount:${SA_ONBOARDING}" --role="roles/secretmanager.secretAccessor"
done
for secret in resend-api-key jira-url jira-email jira-api-token jira-project-key slack-webhook-url; do
  gcloud secrets add-iam-policy-binding "$secret" \
    --member="serviceAccount:${SA_WORKER}" --role="roles/secretmanager.secretAccessor"
done

gcloud run deploy scan-onboarding \
  --image="us-central1-docker.pkg.dev/${PROJECT_ID}/mad-platform/scan-onboarding:latest" \
  --region=us-central1 --set-secrets=RESEND_API_KEY=resend-api-key:latest,TURNSTILE_SECRET_KEY=turnstile-secret-key:latest \
  --set-env-vars=TURNSTILE_SITE_KEY=YOUR_TURNSTILE_SITE_KEY

gcloud run deploy scan-worker \
  --image="us-central1-docker.pkg.dev/${PROJECT_ID}/mad-platform/scan-worker:latest" \
  --region=us-central1 --set-secrets=RESEND_API_KEY=resend-api-key:latest,JIRA_URL=jira-url:latest,JIRA_EMAIL=jira-email:latest,JIRA_API_TOKEN=jira-api-token:latest,JIRA_PROJECT_KEY=jira-project-key:latest,SLACK_WEBHOOK_URL=slack-webhook-url:latest
```

### 12. Verify

```bash
gcloud run services describe scan-onboarding --region=us-central1 --format='value(status.url)'
```

Open that URL, submit a real site to scan, and confirm it completes —
watch the status page move from "queued" (if anything else is ahead of
it) to "in progress" to a finished report.

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

No GCP credentials, no Firestore emulator, and no network access are
needed — that's deliberate. Every module in this codebase constructs its
GCP clients lazily (see `mad_platform/config.py` and the accessor
functions in `mad_platform/state/`, `mad_platform/tools/`), so importing
and testing the pure logic, the LLM-output validation boundary, and the
routes that don't need a live backend costs nothing and needs nothing.

## Project structure

```
mad_platform/
  agents/        # Orchestrator, Analyst, Editor, Reporter, Action Agent,
                  # WCAG auto-heal, Pattern Miner (persistent memory),
                  # LLM-output index validation (shared trust boundary)
  tools/         # Crawler, rule checks, AI checks, ADK client, RAG,
                  # WCAG version fetch, issue sink, Slack/email notify,
                  # anti-abuse pre-filters, SSRF-safe URL guard, retry
                  # classification, untrusted-content delimiting
  state/         # Firestore + Cloud Storage clients (lazy singletons)
  web/           # scan-onboarding's app (submission UI, status page,
                  # internal review queue, open feedback form),
                  # scan-worker's app (the pipeline's push target),
                  # scan-wcag-poller's app, shared theme/charts
  data/          # Curated WCAG success-criteria corpus
  severity.py    # The severity vocabulary, defined once (a real Literal
                  # type, not five independently-drifting string lists)
  config.py      # The one place required environment configuration is
                  # read -- no defaults for anything naming a cloud
                  # resource, values read lazily so import stays
                  # side-effect-free
docs/            # Self-hosted architecture diagram (GitHub Pages)
tests/           # pytest suite -- pure logic, LLM-boundary validation,
                  # and routes that don't need live GCP (see above)
setup.sh                       # One-command deploy -- automates the numbered steps above
run_scan.py                    # CLI entry point for a one-time scan
review_escalations.py          # Internal review queue CLI (web UI is the primary surface)
check_wcag_version.py          # Manual trigger for the WCAG freshness check
mine_patterns.py               # Manual trigger for the pattern miner
Dockerfile                     # scan-onboarding -- no Playwright, thin and cheap
Dockerfile.worker               # scan-worker -- the one image with Playwright/Chromium
Dockerfile.wcag_poller / Dockerfile.pattern_miner
cloudbuild.worker.yaml / cloudbuild.wcag_poller.yaml / cloudbuild.pattern_miner.yaml
```

## Support this project

MAD Platform Community is free, with no ads and no paywall on the actual
scan. 
You can support the project at
[buymeacoffee.com/madplatform](https://buymeacoffee.com/madplatform) — a
link to the same page is in the site footer.

## License

[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0).

AGPL rather than MIT or Apache-2.0 because this is a network service, and
AGPL carries the obligation across that boundary: anyone who runs a
modified version of this code as a hosted service has to make their
source available to its users, not just to people they distribute a
binary to.

## Built during the hackathon submission window

Solo build by Vipul Panday, drawing on a professional background in risk
management and compliance. Now maintained as a free community edition.
