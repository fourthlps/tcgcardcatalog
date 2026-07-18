#!/usr/bin/env python3
"""Rasterise Voyage Log OG source SVGs to the PNGs the article build requires.

Open Graph consumers (Facebook, LINE, X, Discord) do not render SVG. The
Designer produces an OG *source* SVG; this script produces the PNG that
`scripts/build_articles.py` demands before it will emit a production page.

Rasteriser: the Chrome/Edge already installed on the machine, driven headless.
That is a deliberate choice over cairosvg/resvg — it adds NO dependency (see
AGENTS.md: no new production dependency without explicit approval), and it
renders Thai text using the system's Thai fonts, which was the blocker that
deferred the PNG in the first place.

The trade-off is honest: output depends on the fonts installed on the machine
that runs this. It is therefore a build-time authoring step whose result is
committed and reviewed, not a step that silently re-renders in CI.

Usage:
    python scripts/rasterize_og.py                    # every slug with an og.svg
    python scripts/rasterize_og.py <slug> [<slug>...] # named slugs
    python scripts/rasterize_og.py --check            # verify PNGs, write nothing
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GRAPHICS = REPO / "onepiece-catalog" / "assets" / "articles" / "graphics"
OG_DIR = REPO / "onepiece-catalog" / "assets" / "articles" / "og"

OG_WIDTH = 1200
OG_HEIGHT = 630

CHROME_CANDIDATES = [
    os.environ.get("VOYAGE_CHROME", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "google-chrome",
    "chromium",
    "chromium-browser",
]


class RasterError(RuntimeError):
    pass


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if not candidate:
            continue
        if Path(candidate).is_file():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    raise RasterError(
        "no Chrome/Edge binary found. Set VOYAGE_CHROME to the executable path."
    )


def png_size(path: Path) -> tuple[int, int]:
    """Read width/height from the PNG IHDR chunk. No image library needed."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise RasterError(f"{path.name}: not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def rasterize(slug: str, chrome: str) -> Path:
    svg = GRAPHICS / slug / "og.svg"
    if not svg.is_file():
        raise RasterError(f"{slug}: no OG source at {svg.relative_to(REPO)}")

    OG_DIR.mkdir(parents=True, exist_ok=True)
    out = OG_DIR / f"{slug}.png"

    # Render into a temp file first: a failed run must never leave a corrupt
    # PNG where the build expects an approved one.
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "og.png"
        result = subprocess.run(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                f"--window-size={OG_WIDTH},{OG_HEIGHT}",
                "--virtual-time-budget=5000",
                f"--screenshot={staged}",
                svg.resolve().as_uri(),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if not staged.is_file():
            raise RasterError(
                f"{slug}: Chrome produced no PNG.\n{result.stderr.strip()[:400]}"
            )

        width, height = png_size(staged)
        if (width, height) != (OG_WIDTH, OG_HEIGHT):
            raise RasterError(
                f"{slug}: expected {OG_WIDTH}x{OG_HEIGHT}, got {width}x{height}"
            )
        shutil.copyfile(staged, out)

    return out


def check(slugs: list[str]) -> int:
    failures = 0
    for slug in slugs:
        png = OG_DIR / f"{slug}.png"
        if not png.is_file():
            print(f"MISSING  {slug}: {png.relative_to(REPO)}")
            failures += 1
            continue
        try:
            width, height = png_size(png)
        except RasterError as exc:
            print(f"INVALID  {exc}")
            failures += 1
            continue
        status = "OK" if (width, height) == (OG_WIDTH, OG_HEIGHT) else "WRONG SIZE"
        print(f"{status:8} {slug}: {width}x{height}, {png.stat().st_size:,} bytes")
        if status != "OK":
            failures += 1
    return failures


def discover() -> list[str]:
    return sorted(p.parent.name for p in GRAPHICS.glob("*/og.svg"))


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--check"]
    check_only = "--check" in sys.argv[1:]
    slugs = args or discover()

    if not slugs:
        print("no OG sources found under", GRAPHICS.relative_to(REPO))
        return 1

    if check_only:
        return 1 if check(slugs) else 0

    chrome = find_chrome()
    print(f"rasteriser: {chrome}")
    for slug in slugs:
        out = rasterize(slug, chrome)
        width, height = png_size(out)
        print(f"wrote {out.relative_to(REPO)} ({width}x{height}, {out.stat().st_size:,} bytes)")

    print("\nOG PNGs are approved assets: review them, then set `og_image` in the")
    print("article's .meta.json to the PNG filename.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RasterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
