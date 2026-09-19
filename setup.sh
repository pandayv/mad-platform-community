#!/usr/bin/env bash
# One-command deploy of every piece described in README.md's "Setting this
# up yourself" section, in the same order, against your own GCP project.
#
# This does not make the app free to run or remove the need for your own
# GCP billing account -- it collapses the ~12 manual sections of gcloud
# commands in the README into one idempotent script, so standing up a
# fresh instance is `export PROJECT_ID=... && ./setup.sh` instead of
# copy-pasting ~200 lines by hand. Safe to re-run: every step checks
# whether its resource already exists before creating it, so re-running
# after a partial failure resumes rather than erroring on "already
# exists" or double-creating anything.
#
# What this script does NOT do, on purpose:
#   - Configure optional integrations (Resend email, Turnstile, Jira,
#     Slack) -- see README section 11, since each needs a real secret
#     value only you have.
#   - Set up a custom domain -- Cloud Run's default *.run.app URL works
#     fine; mapping mad-platform.org itself was a manual one-time step
#     outside this script.
#   - Anything to do with billing, project creation, or IAM roles beyond
#     what each service genuinely needs to run (least privilege, matching
#     the roles listed in the README).
#
# Requires: gcloud CLI, authenticated (`gcloud auth login` and
# `gcloud auth application-default login`), and PROJECT_ID set to a real
# GCP project with billing enabled.

set -euo pipefail

if [[ -z "${PROJECT_ID:-}" ]]; then
  echo "Set PROJECT_ID first: export PROJECT_ID=your-gcp-project-id" >&2
  exit 1
fi

REGION="${REGION:-us-central1}"
export GOOGLE_CLOUD_PROJECT="$PROJECT_ID"
GCS_BUCKET_NAME="${GCS_BUCKET_NAME:-${PROJECT_ID}-reports}"
AR_REPO="mad-platform"

echo "==> Project: $PROJECT_ID   Region: $REGION   Bucket: gs://${GCS_BUCKET_NAME}"
gcloud config set project "$PROJECT_ID" >/dev/null

# ---- helpers: each is "does this already exist? if not, create it" ----

sa_email() { echo "${1}@${PROJECT_ID}.iam.gserviceaccount.com"; }

ensure_service_account() {
  local name="$1"
  if gcloud iam service-accounts describe "$(sa_email "$name")" >/dev/null 2>&1; then
    echo "    service account $name already exists"
  else
    gcloud iam service-accounts create "$name"
  fi
}

ensure_project_role() {
  local member="$1" role="$2"
  # add-iam-policy-binding is itself idempotent (re-adding an existing
  # binding is a no-op), so this doesn't need its own existence check --
  # unlike resource creation, which errors on a duplicate.
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${member}" --role="$role" >/dev/null
}

# ---- 1. APIs ----
echo "==> Enabling required APIs"
gcloud services enable \
  run.googleapis.com firestore.googleapis.com secretmanager.googleapis.com \
  storage.googleapis.com aiplatform.googleapis.com cloudscheduler.googleapis.com \
  cloudtasks.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# ---- 2. Firestore + GCS bucket ----
echo "==> Firestore database (scan-firestore) and reports bucket"
if gcloud firestore databases describe --database=scan-firestore >/dev/null 2>&1; then
  echo "    Firestore database scan-firestore already exists"
else
  gcloud firestore databases create --database=scan-firestore \
    --location="$REGION" --type=firestore-native
fi
if gcloud storage buckets describe "gs://${GCS_BUCKET_NAME}" >/dev/null 2>&1; then
  echo "    bucket gs://${GCS_BUCKET_NAME} already exists"
else
  gcloud storage buckets create "gs://${GCS_BUCKET_NAME}" --location="$REGION"
fi

# ---- 2b. Retention: the privacy page's promise, made real ----
#
# The privacy page says a scan record "is kept for up to 12 months and
# then removed", and that retention "is enforced automatically by the
# database itself (a Firestore TTL policy, for anyone checking), not just
# written here as a promise". Every collection below already writes an
# `expires_at` field -- but a TTL *policy* has to exist in the project for
# Firestore to act on it, and nothing in this repository created one. A
# fresh deploy therefore produced a service that wrote expiry timestamps
# and deleted nothing, under a page saying it deletes itself on schedule.
#
# The same gap existed on the other side: the stored report HTML in GCS
# *is* the scan record in every sense that matters to a user (the URL,
# every finding, the suggested fixes, the scan's review token), and it had
# no expiry at all, so it outlived the Firestore document that referenced
# it. 365 days here matches firestore_client.SCAN_RECORD_RETENTION_DAYS;
# change them together.
echo "==> Retention policies (Firestore TTL + GCS lifecycle)"
for collection in scan_jobs escalations feedback usage_counters \
                  email_verifications verified_devices; do
  # Idempotent: re-running against an already-enabled field is a no-op.
  gcloud firestore fields ttls update expires_at \
    --collection-group="$collection" --database=scan-firestore \
    --enable-ttl --async >/dev/null 2>&1 \
    && echo "    TTL on ${collection}.expires_at" \
    || echo "    TTL on ${collection}.expires_at (already set, or the collection does not exist yet)"
done

LIFECYCLE_JSON="$(mktemp)"
cat > "$LIFECYCLE_JSON" <<'JSON'
{
  "lifecycle": {
    "rule": [
      {
        "action": {"type": "Delete"},
        "condition": {"age": 365, "matchesPrefix": ["reports/"]}
      }
    ]
  }
}
JSON
gcloud storage buckets update "gs://${GCS_BUCKET_NAME}" \
  --lifecycle-file="$LIFECYCLE_JSON" >/dev/null
rm -f "$LIFECYCLE_JSON"
echo "    GCS lifecycle: reports/ deleted after 365 days"

# ---- 3. Artifact Registry ----
echo "==> Artifact Registry repo"
if gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" >/dev/null 2>&1; then
  echo "    repo $AR_REPO already exists"
else
  gcloud artifacts repositories create "$AR_REPO" --repository-format=docker --location="$REGION"
fi
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
ensure_project_role "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" "roles/artifactregistry.writer"

IMG_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}"

# ---- 4. scan-worker + scan-queue ----
echo "==> Deploying scan-worker"
ensure_service_account scan-worker-sa
ensure_service_account scan-queue-invoker-sa
SA_WORKER=$(sa_email scan-worker-sa)
SA_QUEUE_INVOKER=$(sa_email scan-queue-invoker-sa)

ensure_project_role "$SA_WORKER" "roles/datastore.user"
ensure_project_role "$SA_WORKER" "roles/aiplatform.user"
gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET_NAME}" \
  --member="serviceAccount:${SA_WORKER}" --role="roles/storage.objectAdmin" >/dev/null

gcloud builds submit --config=cloudbuild.worker.yaml --region="$REGION" \
  --substitutions=_IMAGE="${IMG_BASE}/scan-worker:latest" .

# MAD_APP_BASE_URL is a placeholder on this first deploy -- scan-onboarding
# doesn't have a URL yet (it's deployed in the next step, and needs
# scan-worker's URL to do it). Fixed up for real once onboarding exists,
# at the bottom of this script, instead of leaving it as a manual
# follow-up step the way the README's walkthrough does.
if gcloud run services describe scan-worker --region="$REGION" >/dev/null 2>&1; then
  gcloud run deploy scan-worker \
    --image="${IMG_BASE}/scan-worker:latest" \
    --region="$REGION" --service-account="$SA_WORKER" \
    --memory=2Gi --cpu=1 --no-cpu-throttling --concurrency=1 --max-instances=5 \
    --update-env-vars=GCS_BUCKET_NAME="${GCS_BUCKET_NAME}",GOOGLE_CLOUD_PROJECT="${PROJECT_ID}" \
    --no-allow-unauthenticated
else
  gcloud run deploy scan-worker \
    --image="${IMG_BASE}/scan-worker:latest" \
    --region="$REGION" --service-account="$SA_WORKER" \
    --memory=2Gi --cpu=1 --no-cpu-throttling --concurrency=1 --max-instances=5 \
    --set-env-vars=GCS_BUCKET_NAME="${GCS_BUCKET_NAME}",GOOGLE_CLOUD_PROJECT="${PROJECT_ID}",MAD_APP_BASE_URL="https://placeholder.invalid" \
    --no-allow-unauthenticated
fi

gcloud run services add-iam-policy-binding scan-worker --region="$REGION" \
  --member="serviceAccount:${SA_QUEUE_INVOKER}" --role="roles/run.invoker" >/dev/null

if gcloud tasks queues describe scan-queue --location="$REGION" >/dev/null 2>&1; then
  echo "    queue scan-queue already exists"
else
  gcloud tasks queues create scan-queue --location="$REGION" \
    --max-concurrent-dispatches=5 --max-attempts=3
fi

WORKER_URL=$(gcloud run services describe scan-worker --region="$REGION" --format='value(status.url)')
echo "    scan-worker: $WORKER_URL"

# ---- 5. scan-onboarding (the public app) ----
echo "==> Deploying scan-onboarding"
ensure_service_account scan-onboarding-sa
SA_ONBOARDING=$(sa_email scan-onboarding-sa)

ensure_project_role "$SA_ONBOARDING" "roles/datastore.user"
gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET_NAME}" \
  --member="serviceAccount:${SA_ONBOARDING}" --role="roles/storage.objectViewer" >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$SA_QUEUE_INVOKER" \
  --member="serviceAccount:${SA_ONBOARDING}" --role="roles/iam.serviceAccountUser" >/dev/null

gcloud builds submit --tag="${IMG_BASE}/scan-onboarding" --region="$REGION" .

if gcloud run services describe scan-onboarding --region="$REGION" >/dev/null 2>&1; then
  gcloud run deploy scan-onboarding \
    --image="${IMG_BASE}/scan-onboarding:latest" \
    --region="$REGION" --service-account="$SA_ONBOARDING" \
    --no-cpu-throttling --memory=1Gi --concurrency=20 --max-instances=3 --min-instances=0 \
    --update-env-vars=GCS_BUCKET_NAME="${GCS_BUCKET_NAME}",GOOGLE_CLOUD_PROJECT="${PROJECT_ID}",SCAN_WORKER_URL="${WORKER_URL}",SCAN_QUEUE_INVOKER_SA="${SA_QUEUE_INVOKER}" \
    --allow-unauthenticated
else
  gcloud run deploy scan-onboarding \
    --image="${IMG_BASE}/scan-onboarding:latest" \
    --region="$REGION" --service-account="$SA_ONBOARDING" \
    --no-cpu-throttling --memory=1Gi --concurrency=20 --max-instances=3 --min-instances=0 \
    --set-env-vars=GCS_BUCKET_NAME="${GCS_BUCKET_NAME}",GOOGLE_CLOUD_PROJECT="${PROJECT_ID}",MAD_APP_BASE_URL="https://placeholder.invalid",SCAN_WORKER_URL="${WORKER_URL}",SCAN_QUEUE_INVOKER_SA="${SA_QUEUE_INVOKER}" \
    --allow-unauthenticated
fi

APP_URL=$(gcloud run services describe scan-onboarding --region="$REGION" --format='value(status.url)')
echo "    scan-onboarding: $APP_URL"

# Now that the real public URL exists, fix up MAD_APP_BASE_URL on both
# services -- this is the step the README leaves as a manual follow-up
# ("you can redeploy this service to fix it up after step 8"); doing it
# here means the script produces a fully working instance in one run.
echo "==> Setting the real MAD_APP_BASE_URL on both services"
gcloud run services update scan-onboarding --region="$REGION" \
  --update-env-vars=MAD_APP_BASE_URL="${APP_URL}" >/dev/null
gcloud run services update scan-worker --region="$REGION" \
  --update-env-vars=MAD_APP_BASE_URL="${APP_URL}" >/dev/null

# ---- 6. scan-wcag-poller (daily WCAG freshness check) ----
echo "==> Deploying scan-wcag-poller"
ensure_service_account scan-wcag-poller-sa
ensure_service_account scan-scheduler-invoker-sa
SA_WCAG=$(sa_email scan-wcag-poller-sa)
SA_SCHEDULER=$(sa_email scan-scheduler-invoker-sa)

ensure_project_role "$SA_WCAG" "roles/datastore.user"
ensure_project_role "$SA_WCAG" "roles/aiplatform.user"

gcloud builds submit --config=cloudbuild.wcag_poller.yaml --region="$REGION" \
  --substitutions=_IMAGE="${IMG_BASE}/scan-wcag-poller:latest" .
gcloud run deploy scan-wcag-poller \
  --image="${IMG_BASE}/scan-wcag-poller:latest" \
  --region="$REGION" --service-account="$SA_WCAG" --memory=512Mi --max-instances=1 \
  --set-env-vars=GOOGLE_CLOUD_PROJECT="${PROJECT_ID}" \
  --no-allow-unauthenticated

gcloud run services add-iam-policy-binding scan-wcag-poller --region="$REGION" \
  --member="serviceAccount:${SA_SCHEDULER}" --role="roles/run.invoker" >/dev/null

WCAG_URL=$(gcloud run services describe scan-wcag-poller --region="$REGION" --format='value(status.url)')
if gcloud scheduler jobs describe scan-wcag-poller-tick --location="$REGION" >/dev/null 2>&1; then
  echo "    scheduler job scan-wcag-poller-tick already exists"
else
  gcloud scheduler jobs create http scan-wcag-poller-tick \
    --location="$REGION" --schedule="0 4 * * *" --uri="$WCAG_URL" \
    --http-method=POST --oidc-service-account-email="$SA_SCHEDULER"
fi

# ---- 7. pattern-miner (weekly Cloud Run Job) ----
echo "==> Deploying pattern-miner"
ensure_service_account pattern-miner-sa
SA_MINER=$(sa_email pattern-miner-sa)
ensure_project_role "$SA_MINER" "roles/datastore.user"
ensure_project_role "$SA_MINER" "roles/aiplatform.user"

gcloud builds submit --config=cloudbuild.pattern_miner.yaml --region="$REGION" \
  --substitutions=_IMAGE="${IMG_BASE}/pattern-miner:latest" .
if gcloud run jobs describe pattern-miner --region="$REGION" >/dev/null 2>&1; then
  gcloud run jobs update pattern-miner \
    --image="${IMG_BASE}/pattern-miner:latest" \
    --region="$REGION" --service-account="$SA_MINER" \
    --memory=512Mi --cpu=1 --task-timeout=300 --max-retries=0 \
    --update-env-vars=GOOGLE_CLOUD_PROJECT="${PROJECT_ID}"
else
  gcloud run jobs create pattern-miner \
    --image="${IMG_BASE}/pattern-miner:latest" \
    --region="$REGION" --service-account="$SA_MINER" \
    --memory=512Mi --cpu=1 --task-timeout=300 --max-retries=0 \
    --set-env-vars=GOOGLE_CLOUD_PROJECT="${PROJECT_ID}"
fi

gcloud run jobs add-iam-policy-binding pattern-miner --region="$REGION" \
  --member="serviceAccount:${SA_SCHEDULER}" --role="roles/run.invoker" >/dev/null

if gcloud scheduler jobs describe pattern-miner-tick --location="$REGION" >/dev/null 2>&1; then
  echo "    scheduler job pattern-miner-tick already exists"
else
  gcloud scheduler jobs create http pattern-miner-tick \
    --location="$REGION" --schedule="0 3 * * 0" \
    --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/pattern-miner:run" \
    --http-method=POST --oauth-service-account-email="$SA_SCHEDULER" \
    --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
fi

cat <<EOF

==> Done. Your instance is live at: ${APP_URL}

No review-access code is configured, so this is a fully public instance
(matching mad-platform.org) -- the SME review queue fails closed until
you set MAD_REVIEW_CODE yourself (see README section 8).

Optional next steps (README section 11), none required for the scanner
itself to work:
  - Resend, for emailing the report instead of only displaying it.
  - Cloudflare Turnstile, for a bot challenge on the scan form.
  - Jira / Slack, in place of the default CSV export and no notifications.

Verify it end to end: open ${APP_URL}, submit a real site to scan, and
confirm it completes.
EOF
