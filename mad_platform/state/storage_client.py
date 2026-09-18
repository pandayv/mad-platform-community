"""Persists the generated report to Cloud Storage.

Kept private (no public read access) -- these are real findings about a
specific site's compliance gaps, not something to expose by default.
Access for now is the Cloud Console browser link below, which works for
anyone with viewer access on the project; a real "email this to the site
owner" flow would need signed URLs instead, which need a service account
key or IAM SignBlob permission not yet configured.
"""

from __future__ import annotations

from functools import lru_cache

from google.cloud import storage

from mad_platform import config


@lru_cache(maxsize=1)
def get_client() -> storage.Client:
    """Lazily-built, process-wide GCS client. Not at import time: see
    firestore_client.get_client's docstring for why (import must not need
    live credentials), and mad_platform/config.py for why the project and
    bucket names have no fallback defaults any more -- the ones they had
    pointed at the hackathon project this repo must never touch.
    """
    return storage.Client(project=config.project_id())


@lru_cache(maxsize=1)
def _bucket() -> storage.Bucket:
    return get_client().bucket(config.gcs_bucket_name())


def save_report(job_id: str, report_html: str) -> str:
    """Saves the report, returns its gs:// URI."""
    blob_path = f"reports/{job_id}.html"
    blob = _bucket().blob(blob_path)
    blob.upload_from_string(report_html, content_type="text/html")
    return f"gs://{config.gcs_bucket_name()}/{blob_path}"


def read_report(job_id: str) -> str | None:
    blob = _bucket().blob(f"reports/{job_id}.html")
    return blob.download_as_text() if blob.exists() else None


def console_object_url(job_id: str) -> str:
    """Cloud Console link to the specific report object -- opens a preview/
    download UI, requires the viewer to be logged into the GCP project.
    """
    return (
        f"https://console.cloud.google.com/storage/browser/_details/"
        f"{config.gcs_bucket_name()}/reports/{job_id}.html?project={config.project_id()}"
    )


def console_folder_url() -> str:
    """Cloud Console link to the whole reports/ folder -- the standing,
    reusable link the user can bookmark rather than one per scan.
    """
    return (
        f"https://console.cloud.google.com/storage/browser/"
        f"{config.gcs_bucket_name()}/reports?project={config.project_id()}"
    )
