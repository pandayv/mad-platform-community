"""Editor: independently verifies every finding Analyst produced.

Dismisses false positives with a required written reason, assigns a
validated confidence rating to what survives. Uses the higher-capability
model tier -- this is exactly the kind of low-volume, high-consequence
call worth spending that on.

Grounded against retrieved WCAG text, not just the model's own parametric
knowledge. Retrieval provides candidates, not a forced answer -- semantic
similarity isn't guaranteed to surface the single best match (e.g.
"aria-hidden on a focusable button" can retrieve focus-themed criteria
over the actually-correct Name/Role/Value one), so Editor reasons over
what's retrieved rather than blindly adopting it.

The written rationale on every dismissal is required for exactly this
reason: mad_platform/agents/pattern_miner.py mines accumulated dismissal
history for recurring, SME-confirmed patterns, which come back here as
grounding on every future call (see _format_learned_patterns) -- the
persistent-memory loop the rest of this docstring used to describe as
future work.
"""

from __future__ import annotations

from pydantic import BaseModel

from mad_platform.agents.analyst import RawFinding
from mad_platform.state import firestore_client as fs
from mad_platform.tools.adk_client import generate_structured
from mad_platform.tools.crawler import PageSnapshot
from mad_platform.tools.gemini_client import FLASH
from mad_platform.tools.rag import retrieve_batch as rag_retrieve_batch


class VerifiedFinding(BaseModel):
    finding_index: int  # which raw finding this corresponds to, by list position
    confirmed: bool
    wcag_criterion: str  # Editor may correct Analyst's citation
    rationale: str  # required either way -- why confirmed, or why dismissed
    confidence: float  # 0.0-1.0, Editor's own validated rating; only meaningful if confirmed


class _VerificationResponse(BaseModel):
    verifications: list[VerifiedFinding]


_EDITOR_PROMPT = """You are an accessibility Editor. Analyst has flagged the
findings below on a webpage. Your job is to independently verify EACH one
against the actual page evidence (the HTML excerpt and the screenshot),
not to trust Analyst's flag at face value.

Analyst is deliberately tuned toward high recall -- it over-flags on
purpose, so a meaningful fraction of these will be false positives you
should dismiss. A missed real violation is the actual risk; a correctly
dismissed false positive is Analyst and Editor working as designed, not a
failure.

For EVERY finding, whether you confirm or dismiss it, give a specific
rationale grounded in the actual evidence -- not a generic restatement of
the finding. If you dismiss one, say concretely why it doesn't hold up
(e.g. "the img has role=presentation, missing alt is correct here" or
"this text's actual rendered color has sufficient contrast, the flagged
value appears to be a hover state not visible by default").

Dismiss outright only when you're genuinely confident it's not a real
issue -- a verifiable fact you can point to, the kind of example above.
Don't dismiss a real judgment call just because you lean toward "probably
fine": a low-confidence system escalates automatically for a human to
make the actual call, but a dismissal is final and invisible -- nobody
ever sees it again. So when reasonable practitioners could genuinely
disagree (e.g. a muted, looping background video with no dialogue --
does WCAG 1.2.2's captions requirement meaningfully apply to content
with no informational audio, or not -- both readings are defensible),
confirm it instead, at a low confidence score, rather than dismissing.
That's not a failure to decide -- it's routing a genuine gray area to
the one place equipped to close it, instead of silently erasing it or
asserting a certainty you don't actually have.

If you confirm a finding, also give your own confidence rating (0.0-1.0)
reflecting how certain you are this is a real, actionable violation --
low for the genuine judgment calls described above, high when you're
confident it's a real, clear-cut violation.

Check whether the flagged element (or an ancestor) carries
data-mad-hidden="true" in the HTML excerpt -- this is set from the page's
real computed style (display:none, visibility:hidden, or zero rendered
size), not guessed, so it's ground truth, not a hint. A finding about
something not currently rendered is describing a problem that doesn't
exist in the page's current state, however plausible it reads from the
markup alone -- dismiss it, or if the underlying concern would become real
the moment the element is shown (e.g. an aria-hidden modal whose focus
management might not update correctly when it opens), say so explicitly
and confirm at reduced confidence rather than treating it as an active
violation. This is a real, confirmed failure mode: a closed lightbox
modal (display:none, aria-hidden="true", focusable buttons inside) was
previously confirmed at 88/100 as an active keyboard trap, when tabbing
through the live page never actually reached it -- display:none already
removes focusable descendants from the tab order on its own, regardless
of aria-hidden.

For each finding, retrieved WCAG reference candidates are provided below
it -- these are the CLOSEST semantic matches found, not a guaranteed
correct answer. Use them to ground your citation when they genuinely fit;
if none of the candidates match the actual issue, use your own knowledge
instead rather than forcing a bad fit.
{learned_patterns}
Findings to verify (index: source, check, Analyst's WCAG guess, description, selector, Analyst's own confidence, retrieved reference candidates):
{findings_list}

Page title: {title}
HTML excerpt:
{html_excerpt}
"""


def _format_learned_patterns(patterns: list[dict]) -> str:
    if not patterns:
        return ""
    lines = "\n".join(
        f"- WCAG {p['wcag_criterion']}: {p['pattern_description']}" for p in patterns
    )
    return (
        "\nKnown dismissal patterns, confirmed by a human reviewer from this "
        "system's own history -- weigh a matching finding accordingly, but "
        "still verify against the actual evidence rather than dismissing on "
        "pattern match alone:\n" + lines + "\n"
    )


def _format_findings(findings: list[RawFinding]) -> str:
    all_candidates = rag_retrieve_batch([f.description for f in findings], top_k=3)
    lines = []
    for i, (f, candidates) in enumerate(zip(findings, all_candidates)):
        candidates_text = "; ".join(f"{c.number} {c.title} ({c.level})" for c in candidates) or "none found"
        lines.append(
            f"{i}: [{f.source}/{f.check}] Analyst guessed WCAG {f.wcag_criterion} -- {f.description} "
            f"(selector: {f.selector}, Analyst confidence: {f.analyst_confidence:.2f})\n"
            f"    retrieved candidates: {candidates_text}"
        )
    return "\n".join(lines)


async def verify_findings(snapshot: PageSnapshot, findings: list[RawFinding]) -> list[VerifiedFinding]:
    if not findings:
        return []

    prompt = _EDITOR_PROMPT.format(
        learned_patterns=_format_learned_patterns(fs.list_learned_patterns()),
        findings_list=_format_findings(findings),
        title=snapshot.title,
        html_excerpt=snapshot.html[:8000],
    )
    result = await generate_structured(
        FLASH, prompt, _VerificationResponse, image_bytes=snapshot.screenshot_png
    )
    return result.verifications
