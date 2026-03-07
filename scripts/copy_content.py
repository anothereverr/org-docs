"""
Copy and normalise the content of a cloned wiki into the central docs directory.

Responsibilities:
  - Namespace each wiki under docs/{slug}/
  - Rename Home.md → index.md
  - Copy and flatten images into docs/{slug}/images/
  - Apply link and image-path rewrites via rewrite_links.py
  - Parse (but not publish) _sidebar.md for nav generation
  - Return a structured result dict consumed by generate_nav.py
"""
from __future__ import annotations

import logging
import os
import shutil
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from rewrite_links import BrokenLink, build_known_pages, rewrite_content

# Represents an image filename collision within a single repo's wiki:
# two source images had the same basename → the second one overwrote the first.
ImageCollision = namedtuple("ImageCollision", ["filename"])

logger = logging.getLogger("wiki-sync.copy")

# File extensions treated as binary assets (images)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico"}

# Files we parse/process but never publish as standalone pages
SKIP_FILES = {"_sidebar.md", "_footer.md", "_header.md"}


class CopyResult(TypedDict):
    slug: str
    pages: list[str]               # relative paths of .md files written (e.g. "API.md")
    images: list[str]              # basenames of images copied
    has_sidebar: bool
    sidebar_structure: list        # parsed sidebar, consumed by generate_nav.py
    broken_links: list             # list[BrokenLink] — wiki links with missing targets
    image_collisions: list         # list[ImageCollision] — duplicate image filenames


def copy_wiki_content(
    source_dir: Path,
    docs_dir: Path,
    slug: str,
) -> CopyResult:
    """
    Copy all content from *source_dir* (a cloned wiki) to *docs_dir/{slug}/*.

    Returns a CopyResult with metadata used for nav generation.
    """
    dest = docs_dir / slug

    # Clean destination so deleted pages don't persist across syncs
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    images_dir = dest / "images"

    # Collect all files in the source directory (flat — GitHub wikis are flat)
    source_files = list(source_dir.iterdir())

    # Build known_pages set BEFORE rewriting so we can resolve wiki links
    known_pages = build_known_pages(source_dir)

    # Timestamp used in the footer of every generated page
    sync_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    pages: list[str] = []
    images: list[str] = []
    sidebar_structure: list = []
    has_sidebar = False
    broken_links: list = []
    image_collisions: list = []

    for src_file in source_files:
        if not src_file.is_file():
            continue

        name = src_file.name
        ext = src_file.suffix.lower()
        name_lower = name.lower()

        # ---- Handle _sidebar.md: parse, do not copy ----
        if name_lower == "_sidebar.md":
            has_sidebar = True
            sidebar_structure = _parse_sidebar(src_file, slug)
            logger.debug("[%s] Parsed _sidebar.md (%d entries)", slug, len(sidebar_structure))
            continue

        # ---- Skip other meta-files ----
        if name_lower in SKIP_FILES:
            logger.debug("[%s] Skipping meta-file: %s", slug, name)
            continue

        # ---- Images: copy to images/ subdirectory ----
        if ext in IMAGE_EXTENSIONS:
            images_dir.mkdir(exist_ok=True)
            dest_image = images_dir / name
            if dest_image.exists():
                logger.warning(
                    "[%s] Image filename collision: %s — overwriting previous copy",
                    slug,
                    name,
                )
                image_collisions.append(ImageCollision(filename=name))
            shutil.copy2(src_file, dest_image)
            images.append(name)
            logger.debug("[%s] Copied image: %s", slug, name)
            continue

        # ---- Markdown files ----
        if ext == ".md":
            page_broken = _process_markdown(src_file, dest, slug, known_pages, pages, sync_time)
            broken_links.extend(page_broken)
            continue

        # ---- Other files (PDFs, etc.): copy to assets/ ----
        assets_dir = dest / "assets"
        assets_dir.mkdir(exist_ok=True)
        shutil.copy2(src_file, assets_dir / name)
        logger.debug("[%s] Copied asset: %s", slug, name)

    logger.info(
        "[%s] Copied %d page(s), %d image(s), %d broken link(s), %d image collision(s). Sidebar: %s",
        slug,
        len(pages),
        len(images),
        len(broken_links),
        len(image_collisions),
        has_sidebar,
    )

    return CopyResult(
        slug=slug,
        pages=sorted(pages),
        images=images,
        has_sidebar=has_sidebar,
        sidebar_structure=sidebar_structure,
        broken_links=broken_links,
        image_collisions=image_collisions,
    )


def _process_markdown(
    src_file: Path,
    dest_dir: Path,
    slug: str,
    known_pages: set[str],
    pages: list[str],
    sync_time: str = "",
) -> list:
    """
    Read, rewrite, and write a single markdown file.

    Returns a (possibly empty) list of BrokenLink namedtuples for every wiki
    link in this page whose target was not found in the local wiki.
    """
    # Determine destination filename
    src_stem = src_file.stem.lower()
    if src_stem == "home":
        dest_name = "index.md"
    else:
        dest_name = src_file.name  # preserve original casing

    dest_file = dest_dir / dest_name

    try:
        content = src_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.error("[%s] Cannot read %s: %s", slug, src_file.name, exc)
        return []

    content, broken = rewrite_content(content, known_pages, slug, source_file=src_file.name)

    # Append a subtle footer indicating when the page was last synced.
    # The horizontal rule provides visual separation from the page content.
    if sync_time:
        content = content.rstrip("\n") + f"\n\n---\n\n*Last synced from GitHub wiki · {sync_time}*\n"

    try:
        dest_file.write_text(content, encoding="utf-8")
    except OSError as exc:
        logger.error("[%s] Cannot write %s: %s", slug, dest_name, exc)
        return broken  # still report broken links even if write failed

    pages.append(dest_name)
    logger.debug("[%s] Wrote page: %s", slug, dest_name)
    return broken


# ---------------------------------------------------------------------------
# _sidebar.md parser
# ---------------------------------------------------------------------------

def _parse_sidebar(sidebar_path: Path, slug: str) -> list:
    """
    Parse a GitHub wiki _sidebar.md into a structured list for nav generation.

    Expected sidebar format:
        ## Section Name
        * [Page Title](PageName)
        * [Other](Other-Page)

        ## Another Section
        * [Foo](Foo)

    Returns a list of section dicts:
        [
            {
                "title": "Section Name",
                "pages": [
                    {"title": "Page Title", "target": "PageName"},
                    ...
                ]
            },
            ...
        ]

    A flat list of links (no section headers) is returned as a single
    section with title None.
    """
    try:
        raw = sidebar_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("[%s] Could not read _sidebar.md: %s", slug, exc)
        return []

    sections: list[dict] = []
    current_section: dict | None = None

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # Section header: ## Title
        if stripped.startswith("##"):
            title = stripped.lstrip("#").strip()
            current_section = {"title": title, "pages": []}
            sections.append(current_section)
            continue

        # Top-level header (#): treat as implicit section with no title
        if stripped.startswith("#") and not stripped.startswith("##"):
            # Usually the wiki name — skip
            continue

        # List item: * [Title](Target) or - [Title](Target)
        if stripped.startswith(("* ", "- ", "*\t", "-\t")):
            link_part = stripped[2:].strip()
            entry = _parse_sidebar_link(link_part)
            if entry:
                if current_section is None:
                    current_section = {"title": None, "pages": []}
                    sections.append(current_section)
                current_section["pages"].append(entry)

    return sections


def _parse_sidebar_link(text: str) -> dict | None:
    """
    Extract title and target from a sidebar link like `[Page Title](PageName)`.

    Returns {"title": str, "target": str} or None if not parseable.
    """
    import re
    match = re.match(r"\[([^\]]+)\]\(([^)]*)\)", text.strip())
    if not match:
        return None
    return {
        "title": match.group(1).strip(),
        "target": match.group(2).strip(),
    }
