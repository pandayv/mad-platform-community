"""WCAG auto-heal: freshness-check + refresh loop for the shared knowledge
base. Tunes the system's knowledge, not its judgment.

Detects a version change and refreshes automatically, always -- "refresh"
means re-embedding the curated corpus (mad_platform/data/wcag_corpus.py)
and updating the stored version pointer, the exact same action regardless
of how the change classifies. It does not dynamically fetch and ingest
new WCAG success-criteria text from W3C.

There used to be a human-review gate on a "major" classification, on the
theory that a structural conformance-model shift (e.g. a hypothetical
WCAG 3.0) is riskier to blindly re-embed. In practice the gate's own
"confirm" action called this exact same embed_and_store_corpus(), so it
never actually protected against anything -- confirming and auto-refreshing
did the identical thing. A genuine conformance-model shift needs the
curated corpus file's *content* rewritten by a person before any re-embed
is meaningful either way, and no code path here (gated or not) does that;
it's always a manual edit to wcag_corpus.py, independent of this check.
So: always refresh, and treat "major" purely as an FYI signal that the
corpus content itself might be worth a manual look, not a blocker.

Minor-vs-major classification leans on the model's own general knowledge
of WCAG's versioning history (2.0 -> 2.1 -> 2.2 are documented, publicly
well-known additive revisions within the same conformance model; a jump
to WCAG 3.0 is documented as a structurally different scoring model) --
stable, publicly established domain knowledge, not something that needs a
freshly-scraped changelog to get right.
"""

from __future__ import annotations

from pydantic import BaseModel

from mad_platform.state import firestore_client as fs
from mad_platform.tools import notify
from mad_platform.tools.adk_client import generate_structured
from mad_platform.tools.gemini_client import FLASH
from mad_platform.tools.rag import embed_and_store_corpus
from mad_platform.tools.wcag_version import fetch_current_wcag_version


class _VersionChangeClassification(BaseModel):
    change_type: str  # "minor" | "major"
    confidence: float
    reasoning: str


_CLASSIFY_PROMPT = """The WCAG accessibility standard's current version has
changed from {old_version} to {new_version}. Classify this change:

- "minor": the new version adds success criteria but keeps the same
  conformance model as the old version (e.g. WCAG 2.0 -> 2.1 -> 2.2 are all
  additive, backward-compatible revisions within the WCAG 2.x family --
  nothing that was previously compliant becomes non-compliant, and
  existing success-criterion numbers keep the same meaning).
- "major": the new version uses a fundamentally different conformance
  model (e.g. any jump to WCAG 3.0, which replaces binary pass/fail
  success criteria with a different scoring model entirely).

Give a confidence (0-1) and a one-sentence reason.
"""


async def classify_version_change(old_version: str, new_version: str) -> _VersionChangeClassification:
    prompt = _CLASSIFY_PROMPT.format(old_version=old_version, new_version=new_version)
    return await generate_structured(FLASH, prompt, _VersionChangeClassification)


async def run_wcag_freshness_check(simulate_current_version: str | None = None) -> dict:
    """The scheduled freshness-check tick.

    simulate_current_version overrides the real W3C fetch (see
    check_wcag_version.py --simulate), letting either branch of the
    decision be exercised on demand rather than waiting for a real WCAG
    version change, which is rare.
    """
    stored = fs.get_kb_version()
    stored_version = stored.get("version") if stored else None

    current_version = simulate_current_version or fetch_current_wcag_version()
    fs.touch_kb_check(current_version)

    if stored_version == current_version:
        return {"action": "no_change", "version": current_version}

    if stored_version is None:
        # First run ever -- nothing to compare against yet, just record it.
        # embed_and_store_corpus() is assumed to have already been run once
        # during setup to seed the embeddings themselves.
        fs.set_kb_version(current_version)
        return {"action": "initialized", "version": current_version}

    classification = await classify_version_change(stored_version, current_version)

    embed_and_store_corpus()
    fs.set_kb_version(current_version)

    if classification.change_type == "major":
        notify.alert(
            "WCAG knowledge base auto-refreshed after a MAJOR version change",
            [
                f"{stored_version} → {current_version} (confidence {classification.confidence:.2f})",
                classification.reasoning,
                "Re-embedded automatically like any other change, but a structural/"
                "conformance-model shift like this may mean the curated corpus content "
                "itself (mad_platform/data/wcag_corpus.py) needs a manual update, not "
                "just re-embedding -- worth a look.",
            ],
        )

    return {
        "action": "auto_refreshed",
        "old_version": stored_version,
        "new_version": current_version,
        "change_type": classification.change_type,
        "confidence": classification.confidence,
        "reasoning": classification.reasoning,
    }


def resolve_kb_escalation(escalation_id: str, disposition: str, reviewer: str = "sme") -> None:
    """SME disposition on a pending kb_version_change escalation. confirm ->
    re-embeds the existing curated corpus and advances the stored version
    pointer now; dismiss -> stays on the old version, consciously (an SME
    judged the corpus itself needs a real content update first -- e.g. a
    genuine WCAG 3.0 jump -- before it's safe to just re-embed and move on).
    """
    if disposition not in ("confirm", "dismiss"):
        raise ValueError(f"disposition must be 'confirm' or 'dismiss', got {disposition!r}")

    data = fs.resolve_escalation(escalation_id, disposition=disposition, reviewer=reviewer)

    if disposition == "confirm":
        embed_and_store_corpus()
        fs.set_kb_version(data["new_version"])
