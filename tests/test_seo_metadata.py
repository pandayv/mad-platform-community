"""T1: the SEO/metadata surface, which shipped with zero test coverage.

Grepping `tests/` for robots, sitemap, canonical, jsonld, og:, favicon or
manifest returned nothing at all before this file. That is the newest and
least-exercised code in the repo, and it already carried two defects a
first test would have caught: the FAQ structured data published a
66-character colon-terminated fragment for one of twelve questions (F2),
and the security-header middleware's comment asserted /static/* sets its
own long-lived Cache-Control when it set none (F3, covered in
tests/test_web_routes.py alongside the test whose name already promised
it).

The highest-stakes assertion here is the one nothing checked: that the
review queue and the verification funnel render `noindex, nofollow`. The
review queue "should never appear in a search result in the first place"
(app.py's own words) and nothing enforced it.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from mad_platform.state import firestore_client as fs
from mad_platform.web import app as app_module

ORIGIN = app_module._CANONICAL_ORIGIN


@pytest.fixture
def client() -> TestClient:
    return TestClient(app_module.app)


def _head(body: str) -> str:
    return body.split("</head>", 1)[0]


# --- robots directives ----------------------------------------------------


@pytest.mark.parametrize("path", app_module._PUBLIC_PAGES)
def test_every_public_page_is_indexable(client, path):
    head = _head(client.get(path).text)
    assert '<meta name="robots" content="index, follow">' in head


@pytest.mark.parametrize(
    "render",
    [
        lambda: app_module._render_email_step("https://example.com/"),
        lambda: app_module._render_code_step("https://example.com/", "a@b.com"),
        lambda: app_module._render_review_login(),
        lambda: app_module._render_review_list([]),
    ],
)
def test_every_gated_or_ephemeral_page_is_noindex(render):
    """The review queue lists EVERY pending escalation across every scan
    and every user; the verification steps are per-visitor and
    transactional. Neither has anything for a crawler, and the first
    should never be findable at all.
    """
    head = _head(render())
    assert '<meta name="robots" content="noindex, nofollow">' in head
    assert "index, follow" not in head


def test_the_status_page_is_noindex(client, monkeypatch):
    monkeypatch.setattr(fs, "get_job", lambda _j: {"url": "https://example.com/"})
    head = _head(client.get("/status/3f1c2b8a-7d44-4e9b-9c10-2a5e6f0b1d77").text)
    assert '<meta name="robots" content="noindex, nofollow">' in head


def test_no_page_carries_two_conflicting_robots_directives(client):
    for path in app_module._PUBLIC_PAGES:
        head = _head(client.get(path).text)
        assert head.count('name="robots"') == 1, path


# --- canonical links ------------------------------------------------------


@pytest.mark.parametrize("path", app_module._PUBLIC_PAGES)
def test_every_public_page_has_exactly_one_canonical_at_the_production_origin(client, path):
    head = _head(client.get(path).text)
    links = re.findall(r'<link rel="canonical" href="([^"]+)">', head)
    assert links == [f"{ORIGIN}{path}"]


def test_the_canonical_origin_is_absolute_and_unslashed():
    """A trailing slash here produces "https://host.com//faq"."""
    assert ORIGIN.startswith("https://")
    assert not ORIGIN.endswith("/")


def test_the_canonical_path_is_escaped(client, monkeypatch):
    """F9: `path` was interpolated raw into href attributes while `title`
    and `description` next to it were escaped. Not currently reachable --
    status_page 404s first -- but the safety was a property of two other
    functions rather than of this one.
    """
    head = app_module._head_meta("t", "d", '/status/"><script>alert(1)</script>')
    assert "<script>" not in head
    assert "&quot;" in head or "&#x27;" in head


# --- Open Graph / Twitter / icons ----------------------------------------


@pytest.mark.parametrize("path", app_module._PUBLIC_PAGES)
def test_every_public_page_carries_og_and_twitter_cards(client, path):
    head = _head(client.get(path).text)
    for tag in (
        '<meta property="og:type"',
        '<meta property="og:site_name"',
        '<meta property="og:title"',
        '<meta property="og:url"',
        '<meta property="og:image"',
        '<meta name="twitter:card"',
        '<meta name="twitter:title"',
        '<meta name="twitter:image"',
    ):
        assert tag in head, f"{path} is missing {tag}"


def test_the_og_image_is_an_absolute_url():
    """Relative OG image URLs are ignored by most scrapers."""
    assert app_module._DEFAULT_OG_IMAGE.startswith("https://")


@pytest.mark.parametrize("path", app_module._PUBLIC_PAGES)
def test_every_public_page_links_its_icons_and_manifest(client, path):
    head = _head(client.get(path).text)
    assert 'rel="icon" href="/static/favicon.svg"' in head
    assert 'rel="apple-touch-icon"' in head
    assert 'rel="manifest"' in head


@pytest.mark.parametrize(
    "asset",
    ["favicon.svg", "favicon-32.png", "apple-touch-icon.png", "site.webmanifest", "og-image.png"],
)
def test_every_referenced_static_asset_actually_exists(client, asset):
    """A 404 on a favicon is invisible until someone looks at a crawler
    log; a 404 on the OG image means every shared link renders blank.
    """
    assert client.get(f"/static/{asset}").status_code == 200


def test_no_static_icon_is_an_unreferenced_orphan(client):
    """F12: favicon-16.png was referenced by nothing -- not _head_meta, not
    site.webmanifest, not the README -- which is the kind of orphan that
    leaves the next person guessing which icons are actually wired up.
    It is now the 16x16 tab icon.
    """
    import pathlib

    static = pathlib.Path(app_module.__file__).parent / "static"
    head = _head(client.get("/").text)
    manifest = (static / "site.webmanifest").read_text()
    for icon in sorted(static.glob("*.png")) + sorted(static.glob("*.svg")):
        name = icon.name
        if name in ("hero-dashboard.png", "how-step1.png"):
            continue  # page content, referenced from the body not the head
        assert f"/static/{name}" in head or f"/static/{name}" in manifest, name


def test_every_icon_the_head_references_is_served(client):
    """The general form of the test above: nothing may be referenced that
    is not there.
    """
    head = _head(client.get("/").text)
    for href in re.findall(r'<link[^>]+href="(/static/[^"]+)"', head):
        assert client.get(href).status_code == 200, href


# --- /robots.txt ----------------------------------------------------------


def test_robots_txt_is_served_as_plain_text(client):
    resp = client.get("/robots.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")


@pytest.mark.parametrize(
    "prefix", ["/review", "/scan/", "/status/", "/report/", "/api/"]
)
def test_robots_txt_disallows_every_non_indexable_prefix(client, prefix):
    assert f"Disallow: {prefix}" in client.get("/robots.txt").text


def test_robots_txt_points_at_the_sitemap(client):
    assert f"Sitemap: {ORIGIN}/sitemap.xml" in client.get("/robots.txt").text


def test_robots_txt_does_not_disallow_a_public_page(client):
    """A Disallow prefix that happens to cover a public page would remove
    it from search entirely, silently.
    """
    disallowed = re.findall(r"Disallow: (\S+)", client.get("/robots.txt").text)
    for path in app_module._PUBLIC_PAGES:
        for prefix in disallowed:
            assert not path.startswith(prefix), f"{path} is blocked by {prefix}"


# --- /sitemap.xml ---------------------------------------------------------


def test_the_sitemap_is_well_formed_xml(client):
    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    assert "xml" in resp.headers["content-type"]
    ET.fromstring(resp.text)


def test_the_sitemap_lists_exactly_the_public_pages_and_nothing_more(client):
    root = ET.fromstring(client.get("/sitemap.xml").text)
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    locs = [el.text for el in root.iter(f"{ns}loc")]
    assert locs == [f"{ORIGIN}{path}" for path in app_module._PUBLIC_PAGES]


def test_the_sitemap_never_lists_a_noindex_page(client):
    """The two lists are maintained separately, so this is the assertion
    that keeps them honest.
    """
    body = client.get("/sitemap.xml").text
    for path in ("/review", "/scan/email", "/scan/verify", "/status/"):
        assert path not in body


# --- /llms.txt ------------------------------------------------------------


def test_llms_txt_is_served_as_plain_text(client):
    resp = client.get("/llms.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")


def test_llms_txt_links_every_public_page_at_the_canonical_origin(client):
    body = client.get("/llms.txt").text
    for path in app_module._PUBLIC_PAGES:
        assert f"{ORIGIN}{path}" in body, path


def test_llms_txt_license_claim_has_a_license_file_behind_it():
    """S2: /llms.txt and /terms both state AGPL-3.0. A public repository
    with no LICENSE grants no rights under GitHub's default terms, so the
    claim was one anyone forking on the strength of it could not rely on.
    """
    import pathlib

    license_file = pathlib.Path(app_module.__file__).parents[2] / "LICENSE"
    assert license_file.is_file(), "the site claims AGPL-3.0; the repository must carry it"
    text = license_file.read_text()
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 19 November 2007" in text


def test_the_terms_page_and_llms_txt_agree_on_the_license(client):
    assert "AGPL-3.0" in client.get("/llms.txt").text
    assert "AGPL-3.0" in client.get("/terms").text


# --- JSON-LD --------------------------------------------------------------


def _jsonld_blocks(body: str) -> list[dict]:
    raw = re.findall(
        r'<script type="application/ld\+json">\s*(.*?)\s*</script>', body, re.DOTALL
    )
    return [json.loads(block) for block in raw]


def test_the_homepage_publishes_parseable_organization_and_website_data(client):
    blocks = _jsonld_blocks(client.get("/").text)
    assert len(blocks) == 1
    graph = blocks[0]["@graph"]
    assert [node["@type"] for node in graph] == ["Organization", "WebSite"]
    assert graph[0]["url"] == f"{ORIGIN}/"
    assert graph[0]["logo"].startswith(ORIGIN)


def test_the_faq_publishes_one_parseable_faqpage_block(client):
    blocks = _jsonld_blocks(client.get("/faq").text)
    assert len(blocks) == 1
    assert blocks[0]["@type"] == "FAQPage"


def test_every_visible_faq_question_reaches_the_structured_data(client):
    """The mechanism exists so the visible FAQ and the JSON-LD "cannot
    drift". The regex it used matched `<li>` and `<h3>` only in their bare,
    attribute-less form, so a future `<li class="...">` would have dropped
    that item silently -- no error, no log line.
    """
    body = client.get("/faq").text
    visible = re.findall(r"<h3>(.*?)</h3>", body, re.DOTALL)
    entities = _jsonld_blocks(body)[0]["mainEntity"]
    assert len(entities) == len(visible) == 12
    for heading, entity in zip(visible, entities):
        assert entity["name"] == " ".join(re.sub(r"<[^>]+>", "", heading).split())


def test_no_faq_answer_is_a_truncated_fragment(client):
    """F2, the defect itself: the twelfth item puts its lead-in in a <p>
    and the actual answer in a following <ul>, and the non-greedy `</p>`
    stopped at the lead-in. It published "You can support the project and
    the community in one or more ways:" -- 66 characters, ending in a
    colon, answering nothing. Google treats an acceptedAnswer.text that
    does not answer its question as a structured-data quality problem; at
    worst the FAQ rich result is suppressed for the whole page.
    """
    entities = _jsonld_blocks(client.get("/faq").text)[0]["mainEntity"]
    for entity in entities:
        answer = entity["acceptedAnswer"]["text"]
        assert len(answer) >= app_module._MIN_FAQ_ANSWER_CHARS, entity["name"]
        assert not answer.rstrip().endswith(":"), entity["name"]


def test_the_answer_that_lives_in_a_list_is_published_whole(client):
    """The specific item, named, so a future refactor that reintroduces
    first-block-only extraction fails here with an obvious message.
    """
    entities = _jsonld_blocks(client.get("/faq").text)[0]["mainEntity"]
    support = next(e for e in entities if "support this project" in e["name"].lower())
    answer = support["acceptedAnswer"]["text"]
    assert "Spread the word" in answer
    assert "Donate" in answer


def test_a_script_terminator_in_the_data_cannot_break_out_of_the_block():
    """json.dumps escapes quotes and backslashes, but not `<` or `/`, and
    the HTML parser looks for the literal "</script" before any JSON
    parsing happens -- so an answer containing it would end the block
    early and spill the rest into the page as text.
    """
    out = app_module._jsonld_script({"x": "</script><img src=x onerror=alert(1)>"})
    assert "</script>" == out[-len("</script>") :]
    assert out.count("</script>") == 1
    payload = out.split(">\n", 1)[1].rsplit("\n</script>", 1)[0]
    assert json.loads(payload)["x"] == "</script><img src=x onerror=alert(1)>"


def test_faq_items_with_attributes_on_the_list_element_are_still_found():
    """The silent-drop failure mode, tested directly: the old regex
    required `<li>` and `<h3>` bare.
    """
    html_fragment = (
        '<ol class="trust-list">'
        '<li class="something" data-x="1"><h3 id="q">A question?</h3>'
        "<p>" + "An answer that is comfortably long enough to pass the minimum. " * 3 + "</p>"
        "</li></ol>"
    )
    entities = json.loads(
        app_module._faqpage_jsonld(html_fragment)
        .split(">\n", 1)[1]
        .rsplit("\n</script>", 1)[0]
    )["mainEntity"]
    assert len(entities) == 1
    assert entities[0]["name"] == "A question?"
