# Full DIY setup

This is the manual, step-by-step version of what [`setup.sh`](setup.sh)
automates. Read it if you want to understand what each step is doing,
customize one, or run things by hand instead of with the script. See the
[main README](README.md#setting-this-up-yourself) for prerequisites and
the fast path.

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

## 1. Clone and set up the local environment

```bash
git clone https://github.com/pandayv/mad-platform-community.git
cd mad-platform-community
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
```

## 2. Enable the APIs this project actually uses

```bash
gcloud services enable \
  run.googleapis.com firestore.googleapis.com secretmanager.googleapis.com \
  storage.googleapis.com aiplatform.googleapis.com cloudscheduler.googleapis.com \
  cloudtasks.googleapis.com cloudbuild.googleapis.com
```

## 3. Create Firestore and a Cloud Storage bucket

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

## 4. Authenticate locally and confirm Vertex AI works

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

## 5. Test the pipeline locally, before deploying anything

```bash
export MAD_APP_BASE_URL="http://localhost:8000"
python run_scan.py https://example.com
```

This exercises the real pipeline end to end against your real GCP
project (Vertex AI, Firestore) with the default CSV ticket sink, no
Cloud Run deployment needed yet. Confirms steps 2-4 actually worked
before you spend time deploying.

## 6. Create an Artifact Registry repo for the container images

```bash
gcloud artifacts repositories create mad-platform \
  --repository-format=docker --location=us-central1

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
```

## 7. Deploy `scan-worker` and its `scan-queue` (deploy this before `scan-onboarding`)

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

## 8. Deploy `scan-onboarding` (the public app)

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
this deploys a fully public, free instance, matching the live one, and
the internal review queue simply refuses everyone until you configure
one (it fails closed, not open). If you want a private review queue, set
`MAD_REVIEW_CODE` as a Secret Manager secret and pass it with
`--set-secrets` (see `mad_platform/config.py`'s `review_code()` for the
exact variable name).

## 9. Deploy `scan-wcag-poller` and its daily-freshness Scheduler trigger

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

## 10. Deploy the pattern-miner (a Cloud Run Job, not a Service)

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

## 11. Optional: real email delivery, bot mitigation, and swapping CSV for Jira/Slack

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

## 12. Verify

```bash
gcloud run services describe scan-onboarding --region=us-central1 --format='value(status.url)'
```

Open that URL, submit a real site to scan, and confirm it completes —
watch the status page move from "queued" (if anything else is ahead of
it) to "in progress" to a finished report.

Then jump back to the main README's [Running the tests](README.md#running-the-tests).
