#!/usr/bin/env python3
"""Voyage Log article generator.

Converts approved Markdown snapshots in content/articles/approved/ into static
article pages under onepiece-catalog/articles/<slug>/index.html.

Build-time only. Generated HTML has no runtime dependency on this script or on
markdown-it-py; GitHub Pages serves plain static files.

Safety rules are enforced here rather than left to reviewer discipline:
  * snapshots are hash-verified against the approved VoyageSEO source
  * a sidecar that is not status="approved" is never rendered
  * reviewer-only regions are stripped before conversion
  * every INTERNAL href in the finished page must resolve to a verified hash
    route, a generated page, or an in-page id that exists
  * preview output is quarantined under .article-preview/ and can never
    overwrite a published page or the production sitemap
  * nothing is written until every page has rendered and validated in memory

External links are only syntax-checked. Whether a remote page is actually live
is a browser/QA question, not something this build can prove.

Usage:
    python scripts/build_articles.py --check --draft-preview   # full preview validation, writes nothing
    python scripts/build_articles.py --draft-preview           # preview build (noindex, no sitemap)
    python scripts/build_articles.py --check                   # production validation, writes nothing
    python scripts/build_articles.py                           # production build
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import struct
import sys
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import unquote

from markdown_it import MarkdownIt

REPO = Path(__file__).resolve().parent.parent
APPROVED = REPO / "content" / "articles" / "approved"
TEMPLATES = REPO / "scripts" / "templates"
OUT_ROOT = REPO / "onepiece-catalog"                       # production web root
OUT_ARTICLES = OUT_ROOT / "articles"                       # production pages
PREVIEW_ROOT = REPO / ".article-preview"                   # local-only, git-ignored
PREVIEW_ARTICLES = PREVIEW_ROOT / "onepiece-catalog" / "articles"
OG_DIR = OUT_ROOT / "assets" / "articles" / "og"
CHARACTERS_JSON = OUT_ROOT / "data" / "one-piece" / "characters.json"

SITE_BASE = "https://fourthlps.github.io/tcgcardcatalog/onepiece-catalog"
SITE_NAME = "Voyage Log"

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

EXCLUDED_HEADINGS = ("สรุปการตรวจสอบข้อเท็จจริง", "หน้าที่ควรสร้างเพิ่ม")
FAQ_HEADING = "คำถามที่พบบ่อย"
FURTHER_READING_HEADING = "อ่านเพิ่มเติมจาก"

EMOJI_TEXT = {"✅": "มี", "❌": "ไม่มี", "⚠️": "ควรตรวจสอบ"}


class BuildError(Exception):
    """Raised when output would be wrong. Always fails the build loudly."""


md = MarkdownIt("default", {"html": False, "linkify": False, "typographer": False})


# ── mode-specific paths ───────────────────────────────────────────────────────
def paths_for(preview: bool, page_kind: str) -> dict[str, str]:
    """Mode- and page-specific relative paths.

    Preview pages live two directories deeper than production pages, so every
    path that escapes the article directory differs. Production output is
    unaffected by the existence of preview mode.
    """
    if page_kind == "article":
        spa_home = "../../../../onepiece-catalog/" if preview else "../../"
        article_index = "../"
    else:  # article index page
        spa_home = "../../../onepiece-catalog/" if preview else "../"
        article_index = ""

    return {
        "spa_home": spa_home,
        "css": f"{spa_home}assets/articles/article.css",
        "article_index": article_index,
        "route_game": f"{spa_home}#/game/one-piece",
        "route_chase": f"{spa_home}#/chase",
        "route_character": f"{spa_home}#/character/one-piece/",
    }


def retarget(href: str, paths: dict[str, str], preview: bool) -> str:
    """Rewrite production SPA-relative hrefs for the preview tree.

    Metadata sidecars always store production-form hrefs ("../../#/..."), so they
    never encode a preview path. No-op in production.
    """
    if not preview or not href.startswith("../../#/"):
        return href
    return paths["spa_home"] + href[len("../../") :]


# ── verified data ─────────────────────────────────────────────────────────────
def load_character_names() -> set[str]:
    """Character names that actually exist in the One Piece dataset."""
    if not CHARACTERS_JSON.exists():
        raise BuildError(f"characters dataset not found: {CHARACTERS_JSON}")
    data = json.loads(CHARACTERS_JSON.read_text(encoding="utf-8"))
    names: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("name"), str):
                names.add(node["name"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    if not names:
        raise BuildError("no character names found in characters.json")
    return names


# ── snapshot + metadata validation ────────────────────────────────────────────
def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_metas() -> list[dict]:
    metas: list[dict] = []
    seen: set[str] = set()

    for meta_path in sorted(APPROVED.glob("*.meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        slug = meta.get("slug", "")

        if not SLUG_RE.match(slug):
            raise BuildError(f"{meta_path.name}: invalid slug {slug!r}")
        if slug in seen:
            raise BuildError(f"duplicate slug: {slug!r}")
        seen.add(slug)

        if meta_path.name != f"{slug}.meta.json":
            raise BuildError(
                f"{meta_path.name}: filename does not match slug "
                f"(expected {slug}.meta.json)"
            )
        if meta.get("status") != "approved":
            raise BuildError(f"{slug}: status is {meta.get('status')!r}, not 'approved'")

        snapshot = APPROVED / f"{slug}.md"
        if not snapshot.exists():
            raise BuildError(f"{slug}: snapshot {slug}.md is missing")

        expected = meta.get("source_sha256")
        if not expected:
            raise BuildError(f"{slug}: source_sha256 is missing from metadata")
        actual = sha256_of(snapshot)
        if actual != expected:
            raise BuildError(
                f"{slug}: snapshot hash mismatch — approved content has been altered.\n"
                f"  expected {expected}\n  actual   {actual}"
            )

        metas.append(meta)

    if not metas:
        raise BuildError("no approved articles found")
    return metas


def check_iso_date(slug: str, field: str, value: str) -> None:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        raise BuildError(
            f"{slug}: {field} must be ISO YYYY-MM-DD, got {value!r}"
        ) from None


def enforce_release_blockers(metas: list[dict]) -> None:
    """Production-only gates. Preview builds deliberately skip these."""
    for meta in metas:
        slug = meta["slug"]

        if not meta.get("date_published"):
            raise BuildError(
                f"{slug}: date_published is not set. Production output is blocked "
                "until the real first-deployment date is supplied "
                "(use --draft-preview for local review)."
            )
        check_iso_date(slug, "date_published", meta["date_published"])
        if meta.get("date_modified"):
            check_iso_date(slug, "date_modified", meta["date_modified"])

        og_image = meta.get("og_image")
        if not og_image:
            raise BuildError(
                f"{slug}: og_image is not set. Production output is blocked until an "
                "approved Voyage Log OG image exists."
            )
        og_path = OG_DIR / Path(og_image).name
        if not og_path.exists():
            raise BuildError(
                f"{slug}: og_image {og_path.name!r} not found in "
                f"{OG_DIR.relative_to(REPO)}"
            )


# ── markdown → html ───────────────────────────────────────────────────────────
def strip_reviewer_regions(source: str) -> str:
    """Drop the status/front-matter block and every reviewer-only section."""
    lines = source.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("# ")), None)
    if start is None:
        raise BuildError("no H1 found — cannot locate the start of reader-facing content")

    kept: list[str] = []
    skipping_level = 0

    for line in lines[start:]:
        match = re.match(r"^(#{2,6})\s+(.*)$", line)
        if match:
            level, text = len(match.group(1)), match.group(2)
            if skipping_level and level <= skipping_level:
                skipping_level = 0
            if not skipping_level and any(text.startswith(h) for h in EXCLUDED_HEADINGS):
                skipping_level = level
                continue
        if not skipping_level:
            kept.append(line)

    while kept and kept[-1].strip() in ("", "---"):
        kept.pop()
    return "\n".join(kept) + "\n"


def split_h1(body_md: str) -> tuple[str, str]:
    lines = body_md.splitlines()
    return lines[0][2:].strip(), "\n".join(lines[1:]).lstrip("\n")


def heading_id(text: str) -> str:
    """Deterministic, content-derived id.

    An unchanged heading keeps its id across builds, so deep links survive the
    insertion or removal of other headings. Thai does not slugify safely, so the
    id is a short hash of the NFC-normalized heading text.
    """
    norm = unicodedata.normalize("NFC", re.sub(r"\s+", " ", text)).strip()
    return "h-" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:8]


def add_heading_ids(rendered: str) -> tuple[str, list[dict]]:
    headings: list[dict] = []
    used: set[str] = set()

    def repl(match: re.Match) -> str:
        level, inner = match.group(1), match.group(2)
        text = re.sub(r"<[^>]+>", "", inner).strip()
        hid = heading_id(text)
        if hid in used:
            raise BuildError(f"duplicate heading id for heading text: {text!r}")
        used.add(hid)
        headings.append({"id": hid, "level": int(level), "text": text})
        return f'<h{level} id="{hid}">{inner}</h{level}>'

    return re.sub(r"<h([23])>(.*?)</h\1>", repl, rendered, flags=re.S), headings


def section_span(rendered: str, headings: list[dict], prefix: str) -> tuple[int, int]:
    """Character span of a section, ending at the next heading of level <= its own.

    An H2 section therefore runs through all its nested H3s and ends at the next
    H2; an H3 section ends at the next H3 or H2.
    """
    idx = next((i for i, h in enumerate(headings) if h["text"].startswith(prefix)), None)
    if idx is None:
        raise BuildError(f"section not found: heading starting {prefix!r}")

    target = headings[idx]
    start = rendered.index(f'<h{target["level"]} id="{target["id"]}"')

    for nxt in headings[idx + 1 :]:
        if nxt["level"] <= target["level"]:
            return start, rendered.index(f'<h{nxt["level"]} id="{nxt["id"]}"')
    return start, len(rendered)


def apply_anchor_fixes(rendered: str, fixes: list[dict], headings: list[dict]) -> str:
    """Resolve the source's placeholder (#) anchor to a real in-page heading.

    Removes a dead link; never adds one. Unresolvable → build failure.
    """
    for fix in fixes:
        prefix = fix["to_heading_prefix"]
        target = next((h for h in headings if h["text"].startswith(prefix)), None)
        if target is None:
            raise BuildError(f"anchor fix target not found: heading starting {prefix!r}")
        placeholder = f'<a href="{fix["from_href"]}">'
        if placeholder not in rendered:
            raise BuildError(f"anchor fix source not found: href={fix['from_href']!r}")
        rendered = rendered.replace(placeholder, f'<a href="#{target["id"]}">', 1)
    return rendered


def apply_body_links(rendered: str, links: list[dict], headings: list[dict],
                     paths: dict[str, str], preview: bool) -> str:
    """Link only what the metadata authorises, only where it is scoped.

    Wording is never rewritten — matched text is wrapped, not replaced. A rule
    that does not match its expected count fails the build, so a link cannot
    silently vanish or attach to the wrong occurrence.
    """
    for rule in links:
        match_text = rule["match"]
        href = retarget(rule["href"], paths, preview)
        expect = rule.get("expect", 1)
        scope = rule.get("scope")

        low, high = (
            section_span(rendered, headings, FURTHER_READING_HEADING)
            if scope == "further-reading"
            else (0, len(rendered))
        )

        region = rendered[low:high]
        pattern = re.compile(re.escape(match_text) + r"(?![^<]*</a>)")
        found = len(pattern.findall(region))
        if found != expect:
            raise BuildError(
                f"body_link {match_text!r}: expected {expect} match(es) in "
                f"{scope or 'document'}, found {found}"
            )
        region = pattern.sub(f'<a href="{href}">{match_text}</a>', region, count=expect)
        rendered = rendered[:low] + region + rendered[high:]

    return rendered


def mark_external_links(rendered: str) -> str:
    """External links stay in the same tab. No target="_blank"."""
    return re.sub(
        r'<a href="(https?://[^"]+)">(.*?)</a>',
        lambda m: f'<a href="{m.group(1)}" class="ext" rel="external">{m.group(2)}</a>',
        rendered,
        flags=re.S,
    )


def accessible_tables(rendered: str, headings: list[dict]) -> str:
    """Header cells get scope="col"; each table becomes a keyboard-scrollable
    region labelled in Thai by its nearest preceding heading; meaning-bearing
    emoji gain a visually-hidden text equivalent so they are not the only cue."""
    out: list[str] = []
    cursor = 0
    current_heading = ""

    for match in re.finditer(
        r"<h[23] id=\"([^\"]+)\"|<table>.*?</table>", rendered, flags=re.S
    ):
        out.append(rendered[cursor : match.start()])
        cursor = match.end()
        chunk = match.group(0)

        if chunk.startswith("<h"):
            hid = match.group(1)
            current_heading = next((h["text"] for h in headings if h["id"] == hid), "")
            out.append(chunk)
            continue

        table = chunk.replace("<th>", '<th scope="col">')
        for emoji, word in EMOJI_TEXT.items():
            table = table.replace(emoji, f'{emoji}<span class="sr-only">{word}</span>')

        label = f"ตาราง: {current_heading}" if current_heading else "ตารางข้อมูล"
        out.append(
            f'<div class="table-scroll" tabindex="0" role="region" '
            f'aria-label="{html.escape(label)}">{table}</div>'
        )

    out.append(rendered[cursor:])
    return "".join(out)


# ── in-body figures ───────────────────────────────────────────────────────────
# The Visual Designer's manifest is the ONLY source of in-body figures. Approved
# Markdown never carries image syntax, so no visual decision can alter the
# article snapshot or its verified sha256.

FIGURES_KEY = "in_body_figures"
ASSET_ROOT = "onepiece-catalog/assets/articles/"
# Prefix only: variants add modifier classes (e.g. "art-figure card-rail"), and
# this guard must still match them.
FIGURE_MARKER = '<figure class="art-figure'


def load_figures(slug: str) -> list[dict]:
    path = APPROVED / f"{slug}.visual-assets.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BuildError(f"{slug}: visual-assets manifest is not valid JSON: {exc}")
    figures = data.get(FIGURES_KEY, [])
    if not isinstance(figures, list):
        raise BuildError(f"{slug}: manifest {FIGURES_KEY!r} must be a list")
    return figures


ASSET_SUFFIXES = (".svg", ".png", ".webp")


def svg_intrinsic_size(path: Path) -> tuple[int, int]:
    """Intrinsic size from the viewBox, so the <img> reserves space and the
    figure cannot shift layout while it loads."""
    head = path.read_text(encoding="utf-8", errors="ignore")[:2000]
    match = re.search(
        r'viewBox="\s*[-\d.]+\s+[-\d.]+\s+([\d.]+)\s+([\d.]+)', head
    )
    if not match:
        raise BuildError(f"{path.name}: no usable viewBox; cannot reserve layout space")
    return round(float(match.group(1))), round(float(match.group(2)))


def raster_size(path: Path) -> tuple[int, int]:
    """Pixel size straight from the file header. No image library needed, so
    generated artwork adds no dependency to the build (AGENTS.md)."""
    head = path.read_bytes()[:64]

    if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        return struct.unpack(">II", head[16:24])

    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        codec = head[12:16]
        if codec == b"VP8 ":                       # lossy
            if head[23:26] != b"\x9d\x01\x2a":
                raise BuildError(f"{path.name}: corrupt VP8 keyframe header")
            width = struct.unpack("<H", head[26:28])[0] & 0x3FFF
            height = struct.unpack("<H", head[28:30])[0] & 0x3FFF
            return width, height
        if codec == b"VP8L":                       # lossless
            if head[20] != 0x2F:
                raise BuildError(f"{path.name}: corrupt VP8L header")
            bits = struct.unpack("<I", head[21:25])[0]
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if codec == b"VP8X":                       # extended / animated
            return (
                int.from_bytes(head[24:27], "little") + 1,
                int.from_bytes(head[27:30], "little") + 1,
            )
        raise BuildError(f"{path.name}: unrecognised WebP codec {codec!r}")

    raise BuildError(
        f"{path.name}: not a readable PNG or WebP. Supported figure assets are "
        f"{', '.join(ASSET_SUFFIXES)}; for anything else give the manifest entry "
        "explicit integer 'width' and 'height'."
    )


def asset_intrinsic_size(path: Path, entry: dict, label: str) -> tuple[int, int]:
    """Width/height for the <img>, so a figure can never shift layout.

    An explicit width/height in the manifest wins, which is the escape hatch for
    any format this build cannot measure itself. Otherwise the size is read from
    the file, so it cannot drift from the artwork actually shipped.
    """
    declared_w, declared_h = entry.get("width"), entry.get("height")
    if declared_w is not None or declared_h is not None:
        if not all(isinstance(v, int) and v > 0 for v in (declared_w, declared_h)):
            raise BuildError(
                f"{label}: manifest 'width' and 'height' must both be positive integers"
            )
        return declared_w, declared_h

    suffix = path.suffix.lower()
    if suffix == ".svg":
        return svg_intrinsic_size(path)
    if suffix in (".png", ".webp"):
        return raster_size(path)
    raise BuildError(
        f"{label}: unsupported asset type {suffix!r}. Supported: "
        f"{', '.join(ASSET_SUFFIXES)}, or declare explicit 'width' and 'height'."
    )


def inject_figures(rendered: str, headings: list[dict], figures: list[dict],
                   paths: dict[str, str], slug: str) -> str:
    """Place manifest-declared figures at explicit heading anchors.

    Fails closed on every ambiguity: an unknown anchor, a missing asset, an
    asset outside the approved asset root, a missing alt text, or a duplicate
    entry stops the build rather than emitting a broken or unlabelled figure.
    """
    if not figures:
        return rendered

    if FIGURE_MARKER in rendered:
        raise BuildError(
            f"{slug}: article body already contains an injected figure before "
            "injection ran. Refusing to inject twice."
        )

    insertions: list[tuple[int, str]] = []
    seen_ids: set[str] = set()
    seen_assets: set[str] = set()

    for entry in figures:
        vid = entry.get("v_id")
        if not vid:
            raise BuildError(f"{slug}: a {FIGURES_KEY} entry has no 'v_id'")
        if vid in seen_ids:
            raise BuildError(f"{slug}: duplicate figure v_id {vid!r}")
        seen_ids.add(vid)

        if entry.get("injected") is not True:
            raise BuildError(
                f"{slug}/{vid}: only entries with \"injected\": true belong in "
                f"{FIGURES_KEY}"
            )

        variant = entry.get("variant", "image")
        if variant not in ("image", "card-rail"):
            raise BuildError(
                f"{slug}/{vid}: variant must be 'image' or 'card-rail'; got {variant!r}"
            )

        def claim_asset(path_value: str, what: str) -> Path:
            """Every referenced asset must be local, inside the approved asset
            root, present on disk, and used once."""
            if not isinstance(path_value, str) or not path_value.startswith(ASSET_ROOT) \
                    or ".." in path_value:
                raise BuildError(
                    f"{slug}/{vid}: {what} must live under {ASSET_ROOT!r}; "
                    f"got {path_value!r}"
                )
            if path_value in seen_assets:
                raise BuildError(
                    f"{slug}/{vid}: asset injected more than once: {path_value!r}"
                )
            seen_assets.add(path_value)
            resolved = REPO / path_value
            if not resolved.is_file():
                raise BuildError(f"{slug}/{vid}: {what} not found: {path_value}")
            return resolved

        if variant == "image":
            asset = entry.get("asset", "")
            asset_path = claim_asset(asset, "asset")
            alt = entry.get("alt")
            if not isinstance(alt, str) or not alt.strip():
                raise BuildError(
                    f"{slug}/{vid}: an informative figure requires non-empty alt text"
                )
        else:  # card-rail
            items = entry.get("items")
            if not isinstance(items, list) or not items:
                raise BuildError(f"{slug}/{vid}: card-rail requires a non-empty 'items' list")
            for pos, item in enumerate(items):
                for field in ("card_type", "label", "fact"):
                    value = item.get(field)
                    if not isinstance(value, str) or not value.strip():
                        raise BuildError(
                            f"{slug}/{vid}: item {pos} is missing a non-empty {field!r}"
                        )
                item["_icon_path"] = claim_asset(item.get("icon", ""), f"item {pos} icon")

        prefix = (entry.get("anchor") or {}).get("heading_prefix")
        if not prefix:
            raise BuildError(f"{slug}/{vid}: anchor.heading_prefix is required")

        start, end = section_span(rendered, headings, prefix)  # raises if unknown

        position = entry.get("position", "after-heading")
        if position == "after-heading":
            at = rendered.index(">", rendered.index("</h", start)) + 1
        elif position == "end-of-section":
            at = end
        else:
            raise BuildError(
                f"{slug}/{vid}: position must be 'after-heading' or 'end-of-section'"
            )

        caption = entry.get("caption")

        def asset_src(path_value: str) -> str:
            return paths["spa_home"] + path_value[len("onepiece-catalog/"):]

        if variant == "image":
            width, height = asset_intrinsic_size(asset_path, entry, f"{slug}/{vid}")
            figure = (
                f'\n<figure class="art-figure">'
                f'<img src="{asset_src(asset)}" alt="{html.escape(alt)}" '
                f'width="{width}" height="{height}" loading="lazy" decoding="async">'
            )
        else:
            # A rail of Voyage Log original card faces. The tiles are live DOM,
            # not a flattened graphic, so they inherit the site's tokens, respond
            # to the reader's text size, and stay legible at any width.
            label = entry.get("aria_label") or "การ์ดแต่ละประเภท"
            tiles = []
            for item in entry["items"]:
                width, height = asset_intrinsic_size(
                    item["_icon_path"], item, f"{slug}/{vid} item icon"
                )
                # The icon is decorative: the type name sits beside it as text,
                # so alt="" avoids a screen reader announcing it twice.
                tiles.append(
                    '<li class="cr-item"><div class="cr-tile">'
                    f'<span class="cr-art"><img src="{asset_src(item["icon"])}" alt="" '
                    f'width="{width}" height="{height}" loading="lazy" decoding="async"></span>'
                    '<span class="cr-body">'
                    f'<span class="cr-type">{html.escape(item["label"])}</span>'
                    + (
                        f'<span class="cr-en">{html.escape(item["card_type"])}</span>'
                    )
                    + f'<span class="cr-fact">{html.escape(item["fact"])}</span>'
                    "</span></div></li>"
                )
            figure = (
                f'\n<figure class="art-figure card-rail">'
                f'<div class="cr-track" tabindex="0" role="region" '
                f'aria-label="{html.escape(label)}">'
                f'<ul class="cr-list">{"".join(tiles)}</ul>'
                "</div>"
            )

        if caption:
            figure += f"<figcaption>{html.escape(caption)}</figcaption>"
        figure += "</figure>\n"

        insertions.append((at, figure))

    # Insert from the end backwards so earlier offsets stay valid.
    for at, figure in sorted(insertions, key=lambda pair: pair[0], reverse=True):
        rendered = rendered[:at] + figure + rendered[at:]

    return rendered


def hero_block(meta: dict, slug: str, paths: dict[str, str]) -> tuple[str, str]:
    """The article's hero illustration, rendered ABOVE the title.

    Optional. The image is named in the article's metadata, never in the
    stylesheet, so no article-specific path is ever hardcoded in CSS. Articles
    without a hero keep the shared chart artwork and are untouched.

    Fails closed on the same terms as inject_figures(): the file must live in
    this article's own graphics directory, must exist, must be a format the
    build can measure, and must carry alt text.
    """
    name = meta.get("hero_image")
    if not name:
        return "", ""

    if "/" in name or "\\" in name or ".." in name:
        raise BuildError(
            f"{slug}: hero_image must be a bare filename inside the article's "
            f"graphics directory; got {name!r}"
        )

    rel = f"{ASSET_ROOT}graphics/{slug}/{name}"
    path = REPO / rel
    if not path.is_file():
        raise BuildError(f"{slug}: hero_image not found: {rel}")

    alt = meta.get("hero_alt")
    if not isinstance(alt, str) or not alt.strip():
        raise BuildError(
            f"{slug}: hero_alt is required whenever hero_image is set — the hero "
            "is an informative image, not decoration"
        )

    width, height = asset_intrinsic_size(
        path,
        {"width": meta.get("hero_width"), "height": meta.get("hero_height")},
        f"{slug} hero",
    )
    src = paths["spa_home"] + rel[len("onepiece-catalog/"):]

    # The hero is the LCP element: eager, high priority — the opposite of the
    # in-body figures, which are lazy.
    img = (
        f'          <img class="art-hero" src="{src}" alt="{html.escape(alt)}" '
        f'width="{width}" height="{height}" fetchpriority="high" decoding="async">'
    )
    return img, " art-hdr--hero"


def extract_faq(rendered: str, headings: list[dict]) -> list[dict]:
    """FAQPage data, built ONLY from the visible FAQ section. Invents nothing.

    Each FAQ entry in the source is a paragraph whose leading <strong> is the
    question and whose remaining text is the answer. Anything not matching that
    shape is skipped; an empty result means no FAQPage block is emitted at all.
    """
    try:
        low, high = section_span(rendered, headings, FAQ_HEADING)
    except BuildError:
        return []

    faqs: list[dict] = []
    for para in re.findall(r"<p>(.*?)</p>", rendered[low:high], flags=re.S):
        match = re.match(r"\s*<strong>(.*?)</strong>(.*)", para, flags=re.S)
        if not match:
            continue
        question = html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()
        answer = html.unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
        answer = re.sub(r"\s+", " ", answer)
        if question and answer:
            faqs.append({"q": question, "a": answer})
    return faqs


# ── validation of the FINAL assembled page ────────────────────────────────────
def assert_unique_ids(page_html: str, label: str) -> set[str]:
    """Every id in the FINAL page must be unique — headings, related module,
    template landmarks, TOC and accessibility components alike. Duplicate ids
    break fragment links and screen-reader labelling, so they fail the build."""
    ids = re.findall(r'\sid="([^"]+)"', page_html)
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise BuildError(f"{label}: duplicate id(s) in final page: {duplicates}")
    return set(ids)


def validate_links(page_html: str, label: str, all_slugs: set[str],
                   characters: set[str], page_kind: str,
                   paths: dict[str, str]) -> None:
    """Every internal href in the finished page must resolve.

    Runs after template assembly, so it covers the article body, the table of
    contents, breadcrumbs, the related-article module, header/footer chrome and
    the index cards — not just the converted Markdown.

    External http(s) URLs are syntax-checked only. This build cannot prove a
    remote page is live; that is a browser/QA check.
    """
    ids = assert_unique_ids(page_html, label)  # duplicates fail BEFORE fragments

    sibling_re = (
        re.compile(r"\.\./([a-z0-9-]+)/")
        if page_kind == "article"
        else re.compile(r"([a-z0-9-]+)/")
    )
    exact_ok = {paths["route_game"], paths["route_chase"], paths["spa_home"]}
    if page_kind == "article":
        exact_ok.add(paths["article_index"])

    for href in re.findall(r'<a[^>]+href="([^"]*)"', page_html):
        if href in ("", "#"):
            raise BuildError(f"{label}: placeholder or empty link: {href!r}")

        if href.startswith(("https://", "http://")):
            continue  # syntax only; reachability is a QA concern

        if href.startswith("#"):
            if href[1:] not in ids:
                raise BuildError(
                    f"{label}: in-page anchor {href!r} has no matching id in the page"
                )
            continue

        if href in exact_ok:
            continue

        if href.startswith(paths["route_character"]):
            name = unquote(href[len(paths["route_character"]) :])
            if name not in characters:
                raise BuildError(
                    f"{label}: character route {name!r} is not in characters.json"
                )
            continue

        sibling = sibling_re.fullmatch(href)
        if sibling and sibling.group(1) in all_slugs:
            continue

        raise BuildError(f"{label}: link does not resolve to a verified target: {href!r}")


# ── structured data ───────────────────────────────────────────────────────────
def blogposting_ld(meta: dict, h1: str, url: str, preview: bool) -> dict:
    blog: dict = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": h1,
        "description": meta["description"],
        "inLanguage": meta["lang"],
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "author": {"@type": "Organization", "name": meta["author"]},
        "publisher": {"@type": "Organization", "name": SITE_NAME},
    }
    # Dates and image are never faked from a draft date or an invented path.
    if not preview:
        blog["datePublished"] = meta["date_published"]
        blog["dateModified"] = meta["date_modified"] or meta["date_published"]
        if meta.get("og_image"):
            blog["image"] = f"{SITE_BASE}/assets/articles/og/{Path(meta['og_image']).name}"
    return blog


def breadcrumb_ld(h1: str, url: str) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "หน้าแรก",
             "item": f"{SITE_BASE}/"},
            {"@type": "ListItem", "position": 2, "name": "บทความ",
             "item": f"{SITE_BASE}/articles/"},
            {"@type": "ListItem", "position": 3, "name": h1, "item": url},
        ],
    }


def faq_ld(faqs: list[dict]) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": f["q"],
                "acceptedAnswer": {"@type": "Answer", "text": f["a"]},
            }
            for f in faqs
        ],
    }


def _script_safe(payload: dict) -> str:
    """Serialize JSON-LD so approved article text can never terminate the script
    element. Visible article wording is untouched — this affects only the
    machine-readable block."""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return text.replace("</", "<\\/")  # </script> → <\/script>


def json_ld_block(meta: dict, h1: str, url: str, preview: bool,
                  faqs: list[dict]) -> str:
    blocks = [blogposting_ld(meta, h1, url, preview), breadcrumb_ld(h1, url)]
    if faqs:  # omitted entirely when no visible FAQ section was extracted
        blocks.append(faq_ld(faqs))
    return "\n".join(
        f'<script type="application/ld+json">{_script_safe(b)}</script>' for b in blocks
    )


# ── page assembly ─────────────────────────────────────────────────────────────
def build_toc(headings: list[dict]) -> str:
    return "\n".join(
        f'        <li><a href="#{h["id"]}">{html.escape(h["text"])}</a></li>'
        for h in headings
        if h["level"] == 2
    )


def related_module(meta: dict, index: dict[str, dict]) -> str:
    """Site chrome, not editorial content. A missing sibling fails the build
    rather than being silently omitted."""
    slugs = meta.get("related", [])
    for slug in slugs:
        if slug not in index:
            raise BuildError(f"{meta['slug']}: related slug {slug!r} has no generated page")
    if not slugs:
        return ""

    items = "\n".join(
        f'          <li><a href="../{s}/">'
        f'<span class="rel-kicker">{html.escape(index[s]["category_label"])}</span>'
        f'<span class="rel-title">{html.escape(index[s]["title"])}</span></a></li>'
        for s in slugs
    )
    return (
        '      <aside class="related" aria-labelledby="related-h">\n'
        '        <h2 id="related-h">บทความที่เกี่ยวข้อง</h2>\n'
        f"        <ul>\n{items}\n        </ul>\n"
        "      </aside>"
    )


def render_article(meta: dict, index: dict[str, dict], preview: bool,
                   characters: set[str]) -> str:
    slug = meta["slug"]
    paths = paths_for(preview, "article")
    source = (APPROVED / f"{slug}.md").read_text(encoding="utf-8")

    body_md = strip_reviewer_regions(source)
    h1, rest_md = split_h1(body_md)

    # H1/title integrity. display_title does not disable validation; it only
    # changes what the H1 must equal.
    expected_title = meta.get("display_title") or meta["title"]
    if h1 != expected_title:
        raise BuildError(
            f"{slug}: visible H1 does not match the expected title.\n"
            f"  H1:       {h1!r}\n"
            f"  expected: {expected_title!r}  "
            f"(from {'display_title' if meta.get('display_title') else 'title'})\n"
            "Fix the metadata, or add an approved 'display_title' field if the "
            "difference is intentional."
        )

    rendered = md.render(rest_md)
    rendered, headings = add_heading_ids(rendered)
    rendered = apply_anchor_fixes(rendered, meta.get("anchor_fixes", []), headings)
    rendered = apply_body_links(
        rendered, meta.get("body_links", []), headings, paths, preview
    )
    faqs = extract_faq(rendered, headings)  # from the visible section only
    rendered = mark_external_links(rendered)
    rendered = accessible_tables(rendered, headings)
    # Figures are placed last: they must not be scanned as tables, and their
    # alt text is not FAQ or link content.
    rendered = inject_figures(
        rendered, headings, load_figures(slug), paths, slug
    )

    url = f"{SITE_BASE}/articles/{slug}/"  # canonical is always the production URL

    og_image_tags = ""
    if not preview and meta.get("og_image"):
        img = f"{SITE_BASE}/assets/articles/og/{Path(meta['og_image']).name}"
        og_image_tags = (
            f'<meta property="og:image" content="{img}">\n'
            f'<meta name="twitter:image" content="{img}">\n'
            '<meta name="twitter:card" content="summary_large_image">'
        )

    hero_img, hero_class = hero_block(meta, slug, paths)

    page = (TEMPLATES / "article.html").read_text(encoding="utf-8").format(
        hero=hero_img,
        hero_class=hero_class,
        lang=meta["lang"],
        seo_title=html.escape(meta["seo_title"]),
        description=html.escape(meta["description"]),
        canonical=url,
        og_title=html.escape(meta["title"]),
        og_image_tags=og_image_tags,
        robots='<meta name="robots" content="noindex,nofollow">' if preview else "",
        preview_banner=(
            '<p class="preview-banner" role="status">PREVIEW — NOT FOR RELEASE · '
            "ยังไม่ได้กำหนดวันเผยแพร่จริง และยังไม่มีภาพ OG</p>"
            if preview
            else ""
        ),
        json_ld=json_ld_block(meta, h1, url, preview, faqs),
        css=paths["css"],
        spa_home=paths["spa_home"],
        article_index=paths["article_index"],
        site_name=SITE_NAME,
        category_label=html.escape(meta["category_label"]),
        h1=html.escape(h1),
        author=html.escape(meta["author"]),
        date_line=(
            "ตัวอย่างภายใน — ยังไม่กำหนดวันเผยแพร่"
            if preview
            else f'<time datetime="{meta["date_published"]}">{meta["date_published"]}</time>'
        ),
        toc_items=build_toc(headings),
        body=rendered,
        related=related_module(meta, index),
    )

    validate_links(page, slug, set(index), characters, "article", paths)
    return page


def render_index(metas: list[dict], preview: bool, characters: set[str]) -> str:
    paths = paths_for(preview, "index")

    cards = "\n".join(
        f'        <li class="art-card">\n'
        f'          <a class="art-link" href="{m["slug"]}/">\n'
        f'            <span class="art-kicker">{html.escape(m["category_label"])}</span>\n'
        f'            <h2 class="art-title">{html.escape(m["title"])}</h2>\n'
        f'            <span class="art-desc">{html.escape(m["description"])}</span>\n'
        f'            <span class="art-date">'
        f'{html.escape("ยังไม่กำหนดวันเผยแพร่" if preview else m["date_published"])}'
        f"</span>\n"
        f"          </a>\n"
        f"        </li>"
        for m in metas
    )

    page = (TEMPLATES / "article-index.html").read_text(encoding="utf-8").format(
        canonical=f"{SITE_BASE}/articles/",  # production URL, unchanged
        css=paths["css"],
        spa_home=paths["spa_home"],
        site_name=SITE_NAME,
        robots='<meta name="robots" content="noindex,nofollow">' if preview else "",
        preview_banner=(
            '<p class="preview-banner" role="status">PREVIEW — NOT FOR RELEASE</p>'
            if preview
            else ""
        ),
        cards=cards,
    )

    validate_links(
        page, "articles/index", {m["slug"] for m in metas}, characters, "index", paths
    )
    return page


def render_sitemap(metas: list[dict]) -> str:
    """Per-URL lastmod. The SPA homepage carries none: we have no verified
    homepage-modified date, and inventing one would misinform crawlers."""

    def lastmod(meta: dict) -> str:
        return meta.get("date_modified") or meta["date_published"]

    newest = max(lastmod(m) for m in metas)

    entries = [f"  <url><loc>{SITE_BASE}/</loc></url>"]
    entries.append(
        f"  <url><loc>{SITE_BASE}/articles/</loc><lastmod>{newest}</lastmod></url>"
    )
    entries += [
        f"  <url><loc>{SITE_BASE}/articles/{m['slug']}/</loc>"
        f"<lastmod>{lastmod(m)}</lastmod></url>"
        for m in metas
    ]

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )


# ── atomic output ─────────────────────────────────────────────────────────────
def write_atomic(path: Path, content: str) -> None:
    """Write via a temp file in the destination directory, then os.replace().

    Replacement is atomic on the same filesystem, so a reader never sees a
    half-written page.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


# ── driver ────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="Build Voyage Log article pages.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate only; write nothing. Combine with --draft-preview for full "
             "preview validation without the production release blockers.",
    )
    parser.add_argument(
        "--draft-preview",
        action="store_true",
        help="preview mode: writes only under .article-preview/, noindex, no dates, "
             "no OG image, no sitemap. Never touches published pages.",
    )
    args = parser.parse_args()
    preview = args.draft_preview

    # Phase 1: render and validate EVERYTHING in memory. No file is written until
    # every page, its metadata, structured data and links have passed.
    try:
        characters = load_character_names()
        metas = load_metas()
        index = {m["slug"]: m for m in metas}

        if not preview:
            enforce_release_blockers(metas)

        pages = {m["slug"]: render_article(m, index, preview, characters) for m in metas}
        index_page = render_index(metas, preview, characters)
        sitemap = None if preview else render_sitemap(metas)

    except BuildError as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1  # nothing was written — no partial output can exist

    mode = "preview" if preview else "production"

    if args.check:
        print(f"OK — {len(pages)} article(s) validated in {mode} mode. "
              "Nothing written (--check).")
        for slug in pages:
            print(f"  · {slug}")
        print("note: external link reachability is NOT checked here — that is a "
              "browser/QA step.")
        return 0

    # Phase 2: everything validated; now write. Preview output is quarantined and
    # can never overwrite a published page or the production sitemap.
    out_articles = PREVIEW_ARTICLES if preview else OUT_ARTICLES

    for slug, page in pages.items():
        target = out_articles / slug / "index.html"
        write_atomic(target, page)
        print(f"wrote {target.relative_to(REPO)}")

    write_atomic(out_articles / "index.html", index_page)
    print(f"wrote {(out_articles / 'index.html').relative_to(REPO)}")

    if preview:
        base = "http://localhost:8000/.article-preview/onepiece-catalog/articles"
        print("\npreview build complete — production pages and sitemap untouched.")
        print("\nserve from the REPOSITORY ROOT:")
        print("    python -m http.server 8000")
        print("\nthen open:")
        print(f"    index    {base}/")
        for slug in pages:
            print(f"    article  {base}/{slug}/")
    else:
        write_atomic(OUT_ROOT / "sitemap.xml", sitemap)
        print("wrote onepiece-catalog/sitemap.xml")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
