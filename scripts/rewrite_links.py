"""
Rewrite markdown content from a GitHub wiki so it renders correctly in MkDocs.

Two transformations are applied:
  1. Image paths — strip absolute or relative prefixes, flatten to images/{filename}
  2. Wiki-style page links — convert bare page names to .md extensions

Both transformations are applied in-memory; this module does not touch the filesystem.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Collection

logger = logging.getLogger("wiki-sync.rewrite")

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Matches image references:  ![alt text](path/to/image.png)
# Group 1: alt text, Group 2: path (may include query string / anchor — unusual but possible)
_IMAGE_RE = re.compile(r"(!\[[^\]]*\])\(([^)]+)\)")

# Matches link references:   [link text](target)
# We intentionally exclude image refs (handled separately).
# Group 1: display text, Group 2: target (may include #anchor)
_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")

# File extensions we recognise as images
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp", ".ico"}

# Protocols that indicate an external link
_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "ftp://", "//")

# Regex to detect GitHub wiki URLs pointing to the same repo's wiki
# e.g. https://github.com/ORG/REPO/wiki/PageName
_GITHUB_WIKI_RE = re.compile(
    r"https?://github\.com/[^/]+/[^/]+/wiki/([^)\s#]+)(#[^)\s]*)?"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rewrite_content(
    content: str,
    known_pages: Collection[str],
    slug: str,
) -> str:
    """
    Apply all rewrites to *content* and return the transformed string.

    Parameters
    ----------
    content:
        Raw markdown text read from a wiki page.
    known_pages:
        Set of page stems (filenames without .md) that exist in this wiki,
        all lowercased. Used for wiki-link resolution.
    slug:
        Repository slug — used only for warning log messages.
    """
    content = _rewrite_images(content)
    content = _rewrite_wiki_links(content, known_pages, slug)
    return content


# ---------------------------------------------------------------------------
# Image path rewriting
# ---------------------------------------------------------------------------

def _rewrite_images(content: str) -> str:
    """
    Normalise all image references to `images/{basename}`.

    Rules:
      - External URLs (http/https) are left unchanged.
      - Everything else: strip any leading /, ./, ../ sequences, take the
        basename, and replace the path with `images/{basename}`.
      - If the reference has no image extension it is left unchanged
        (it may be a link to a page, not an image).
    """
    def _replace(match: re.Match) -> str:
        prefix = match.group(1)   # e.g. "![alt text]"
        path = match.group(2).strip()

        # Leave external images alone
        if any(path.startswith(p) for p in _EXTERNAL_PREFIXES):
            return match.group(0)

        # Strip query strings or anchors that sometimes appear in image paths
        clean_path = path.split("?")[0].split("#")[0]
        _ext = os.path.splitext(clean_path)[1].lower()

        if _ext not in _IMAGE_EXTENSIONS:
            # Not a recognised image extension; leave as-is
            return match.group(0)

        basename = os.path.basename(clean_path.lstrip("/").lstrip("./"))
        if not basename:
            return match.group(0)

        new_path = f"images/{basename}"
        # Preserve any trailing query/anchor in the original path (rare but possible)
        suffix = path[len(clean_path):]
        return f"{prefix}({new_path}{suffix})"

    return _IMAGE_RE.sub(_replace, content)


# ---------------------------------------------------------------------------
# Wiki-style link rewriting
# ---------------------------------------------------------------------------

def _rewrite_wiki_links(
    content: str,
    known_pages: Collection[str],
    slug: str,
) -> str:
    """
    Convert GitHub wiki-style links to MkDocs-compatible .md links.

    GitHub wiki renders [API](API) as a link to the "API" page.
    MkDocs requires [API](API.md) — the extension is mandatory for internal links.

    Rules:
      - External URLs → unchanged, UNLESS they are GitHub wiki URLs for the
        same repo (https://github.com/ORG/REPO/wiki/Page) → rewritten to Page.md
      - Anchor-only links (#section) → unchanged
      - target == "" or "Home" → index.md
      - target already ends with a non-.md extension (.pdf, .zip, etc.) → unchanged
      - target already ends with .md → normalise Home.md → index.md, else unchanged
      - ALL other bare targets (with or without a match in known_pages) → {target}.md
        Known targets: rewritten silently
        Unknown targets: rewritten with a WARNING (link may 404, but stays in portal)
    """
    known_lower = {p.lower() for p in known_pages}

    # First pass: rewrite absolute GitHub wiki URLs
    content = _rewrite_github_wiki_urls(content, slug)

    def _replace(match: re.Match) -> str:
        text = match.group(1)
        target = match.group(2).strip()

        # External links: leave alone (GitHub wiki URLs already handled above)
        if any(target.startswith(p) for p in _EXTERNAL_PREFIXES):
            return match.group(0)

        # Anchor-only links: leave alone
        if target.startswith("#"):
            return match.group(0)

        # Split target from anchor
        anchor = ""
        if "#" in target:
            target, anchor = target.split("#", 1)
            anchor = "#" + anchor
        target = target.strip()

        # Strip leading / or ./
        target = target.lstrip("/")
        if target.startswith("./"):
            target = target[2:]

        # Already has .md extension
        if target.lower().endswith(".md"):
            page_stem = target[:-3]
            if page_stem.lower() == "home":
                return f"[{text}](index.md{anchor})"
            return match.group(0)  # keep as-is if already .md

        # Empty target → home
        if not target:
            return f"[{text}](index.md{anchor})"

        # "Home" is always index.md
        if target.lower() == "home":
            return f"[{text}](index.md{anchor})"

        # Skip targets that already have a non-markdown file extension
        # (.pdf, .zip, .png, etc.) — these are not wiki pages
        _ext = os.path.splitext(target)[1].lower()
        if _ext and _ext != ".md" and len(_ext) <= 5:
            return match.group(0)

        # Known page: rewrite cleanly
        if target.lower() in known_lower:
            return f"[{text}]({target}.md{anchor})"

        # Unknown target: rewrite anyway so the link stays within the portal.
        # A 404 from MkDocs is better than silently redirecting to GitHub wiki.
        logger.warning(
            "[%s] Wiki link target not found as a local page: [%s](%s) "
            "— rewriting to %s.md anyway (may result in 404)",
            slug,
            text,
            target,
            target,
        )
        return f"[{text}]({target}.md{anchor})"

    return _LINK_RE.sub(_replace, content)


def _rewrite_github_wiki_urls(content: str, slug: str) -> str:
    """
    Rewrite absolute GitHub wiki URLs to relative .md links.

    Example:
      https://github.com/ORG/REPO/wiki/PageName  →  PageName.md
      https://github.com/ORG/REPO/wiki/Home       →  index.md

    This handles cases where wiki authors copy-paste the full browser URL
    instead of using the relative wiki-style link syntax.
    """
    def _replace_wiki_url(match: re.Match) -> str:
        full_match = match.group(0)
        page_name = match.group(1).strip("/")
        anchor = match.group(2) or ""

        if not page_name or page_name.lower() == "home":
            return f"index.md{anchor}"

        logger.debug(
            "[%s] Rewriting GitHub wiki URL to local link: %s → %s.md",
            slug,
            full_match,
            page_name,
        )
        return f"{page_name}.md{anchor}"

    # Only rewrite URLs that appear as link targets: [text](URL)
    # We do this by replacing inside _LINK_RE matches
    def _replace_link(match: re.Match) -> str:
        text = match.group(1)
        target = match.group(2).strip()
        wiki_match = _GITHUB_WIKI_RE.fullmatch(target)
        if wiki_match:
            new_target = _replace_wiki_url(wiki_match)
            logger.warning(
                "[%s] Absolute GitHub wiki URL rewritten: [%s](%s) → [%s](%s)",
                slug, text, target, text, new_target,
            )
            return f"[{text}]({new_target})"
        return match.group(0)

    return _LINK_RE.sub(_replace_link, content)


# ---------------------------------------------------------------------------
# Helper for copy_content.py: build known_pages from a source directory
# ---------------------------------------------------------------------------

def build_known_pages(source_dir) -> set[str]:
    """
    Return a set of lowercased page stems from all .md files in *source_dir*.

    This is used by copy_content.py to pass to rewrite_content().
    """
    from pathlib import Path
    source_path = Path(source_dir)
    stems = set()
    for md_file in source_path.glob("*.md"):
        stem = md_file.stem
        # "Home" is renamed to "index" — both forms should resolve
        stems.add(stem.lower())
        if stem.lower() == "home":
            stems.add("index")
    return stems
