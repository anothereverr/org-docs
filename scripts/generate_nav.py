"""
Generate the complete mkdocs.yml from a template merged with an auto-built nav.

The nav block is constructed from the CopyResult list produced by copy_content.py:
  - If a wiki had _sidebar.md → use its structure, append unlisted pages at the end
  - Otherwise → alphabetical flat nav with index.md first

Usage (standalone):
    python scripts/generate_nav.py --help
"""
from __future__ import annotations

import argparse
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml  # bundled with mkdocs; always available

from copy_content import CopyResult

logger = logging.getLogger("wiki-sync.nav")

# ---------------------------------------------------------------------------
# mkdocs.yml static template
# The nav: key is populated programmatically and merged in at write time.
# ---------------------------------------------------------------------------

MKDOCS_TEMPLATE: dict[str, Any] = {
    "site_name": "Organization Documentation",
    "site_url": "",          # filled from env var GH_PAGES_URL or left blank
    "repo_url": "",          # filled from env var GH_REPO_URL or left blank
    "docs_dir": "docs",
    "theme": {
        "name": "material",
        "features": [
            "navigation.tabs",
            "navigation.sections",
            "navigation.top",
            "navigation.indexes",
            "search.suggest",
            "search.highlight",
            "content.code.copy",
        ],
        "palette": [
            {
                "scheme": "default",
                "primary": "indigo",
                "accent": "indigo",
                "toggle": {
                    "icon": "material/brightness-7",
                    "name": "Switch to dark mode",
                },
            },
            {
                "scheme": "slate",
                "primary": "indigo",
                "accent": "indigo",
                "toggle": {
                    "icon": "material/brightness-4",
                    "name": "Switch to light mode",
                },
            },
        ],
    },
    "plugins": [
        "search",
    ],
    "markdown_extensions": [
        "pymdownx.highlight",
        "pymdownx.superfences",
        "pymdownx.inlinehilite",
        "pymdownx.tabbed",
        "admonition",
        {"toc": {"permalink": True}},
    ],
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_mkdocs_yml(
    results: list[CopyResult],
    output_path: Path,
    site_name: str = "",
    site_url: str = "",
    repo_url: str = "",
) -> None:
    """
    Write a complete mkdocs.yml to *output_path*.

    Parameters
    ----------
    results:
        List of CopyResult dicts from copy_content.py.
    output_path:
        Destination file (typically the project root mkdocs.yml).
    site_name, site_url, repo_url:
        Optional overrides from environment variables.
    """
    config = dict(MKDOCS_TEMPLATE)

    if site_name:
        config["site_name"] = site_name
    if site_url:
        config["site_url"] = site_url
    if repo_url:
        config["repo_url"] = repo_url

    config["nav"] = _build_nav(results)

    # yaml.dump uses block style for readability
    yml_text = yaml.dump(
        config,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )

    output_path.write_text(yml_text, encoding="utf-8")
    logger.info("Wrote mkdocs.yml → %s", output_path)


# ---------------------------------------------------------------------------
# Nav construction
# ---------------------------------------------------------------------------

def _build_nav(results: list[CopyResult]) -> list:
    """
    Build the top-level nav list.

    Structure:
        - Home: index.md
        - Repositories:
          - repo-a:
            - Home: repo-a/index.md
            - API Reference: repo-a/API.md
          - repo-b:
            ...
    """
    nav: list = [{"Home": "index.md"}]

    if not results:
        return nav

    # Sort repos alphabetically by slug for consistent ordering
    sorted_results = sorted(results, key=lambda r: r["slug"])

    repos_nav: list = []
    for result in sorted_results:
        repo_section = _build_repo_nav(result)
        if repo_section:
            # Display name: title-case the slug for readability
            display_name = _slug_to_title(result["slug"])
            repos_nav.append({display_name: repo_section})

    if repos_nav:
        nav.append({"Repositories": repos_nav})

    return nav


def _build_repo_nav(result: CopyResult) -> list:
    """
    Build the nav section for a single repo.

    Returns an empty list if the repo has no pages (will be omitted from nav).
    """
    slug = result["slug"]
    pages = result["pages"]

    if not pages:
        logger.warning("[%s] No pages found — omitting from nav", slug)
        return []

    if result["has_sidebar"] and result["sidebar_structure"]:
        return _nav_from_sidebar(result)
    else:
        return _nav_flat(slug, pages)


def _nav_flat(slug: str, pages: list[str]) -> list:
    """
    Generate a flat, alphabetical nav list.
    index.md (Home) is always placed first.
    """
    nav_items: list = []

    # index.md first
    if "index.md" in pages:
        nav_items.append({"Home": f"{slug}/index.md"})

    for page in sorted(pages):
        if page == "index.md":
            continue
        title = _page_title(page)
        nav_items.append({title: f"{slug}/{page}"})

    return nav_items


def _nav_from_sidebar(result: CopyResult) -> list:
    """
    Build a structured nav from a parsed _sidebar.md.

    Pages present in docs/ but not referenced by the sidebar are appended
    under an "Other" section so nothing is silently hidden.
    """
    slug = result["slug"]
    pages_set = set(result["pages"])
    referenced: set[str] = set()
    nav_items: list = []

    for section in result["sidebar_structure"]:
        section_title = section.get("title")
        section_pages = section.get("pages", [])

        section_nav: list = []
        for entry in section_pages:
            target = entry.get("target", "").strip()
            title = entry.get("title", target)

            # Resolve target to a .md filename in the docs directory
            dest_name = _resolve_sidebar_target(target, pages_set, slug)
            if dest_name:
                section_nav.append({title: f"{slug}/{dest_name}"})
                referenced.add(dest_name)
            else:
                logger.warning(
                    "[%s] Sidebar references unknown page '%s' — skipping",
                    slug,
                    target,
                )

        if not section_nav:
            continue

        if section_title:
            nav_items.append({section_title: section_nav})
        else:
            # No section header — add items directly to root level
            nav_items.extend(section_nav)

    # Append unreferenced pages under "Other"
    unreferenced = sorted(pages_set - referenced)
    if unreferenced:
        other_items = [
            {_page_title(p): f"{slug}/{p}"} for p in unreferenced
        ]
        nav_items.append({"Other": other_items})

    return nav_items


def _resolve_sidebar_target(target: str, pages_set: set[str], slug: str) -> str | None:
    """
    Resolve a sidebar link target to an actual filename in pages_set.

    Handles:
      - "Home" → "index.md"
      - "API" → "API.md" (if it exists)
      - "API.md" → "API.md" (already has extension)
      - Case-insensitive matching as fallback
    """
    if not target:
        return None

    # Home always maps to index.md
    if target.lower() == "home":
        return "index.md" if "index.md" in pages_set else None

    # Already has .md extension
    if target.lower().endswith(".md"):
        if target in pages_set:
            return target
        # Try case-insensitive
        for p in pages_set:
            if p.lower() == target.lower():
                return p
        return None

    # Try adding .md extension
    candidate = f"{target}.md"
    if candidate in pages_set:
        return candidate

    # Case-insensitive fallback
    candidate_lower = candidate.lower()
    for p in pages_set:
        if p.lower() == candidate_lower:
            return p

    return None


# ---------------------------------------------------------------------------
# Display name helpers
# ---------------------------------------------------------------------------

def _page_title(filename: str) -> str:
    """
    Convert a markdown filename to a human-readable title.

    "API-Reference.md" → "API Reference"
    "getting-started.md" → "Getting Started"
    "index.md" → "Home"
    """
    stem = filename.removesuffix(".md") if filename.endswith(".md") else filename
    if stem.lower() == "index":
        return "Home"
    # Replace hyphens and underscores with spaces, title-case
    title = re.sub(r"[-_]+", " ", stem)
    # Title-case but preserve all-caps acronyms (e.g. "API", "CLI")
    words = []
    for word in title.split():
        if word.isupper() and len(word) > 1:
            words.append(word)
        else:
            words.append(word.capitalize())
    return " ".join(words)


def _slug_to_title(slug: str) -> str:
    """Convert a repo slug to a display name. "my-service" → "My Service"."""
    return " ".join(word.capitalize() for word in slug.replace("-", " ").replace("_", " ").split())


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import json
    import sys

    parser = argparse.ArgumentParser(description="Generate mkdocs.yml from sync results.")
    parser.add_argument(
        "--results-file",
        required=True,
        help="Path to JSON file containing list of CopyResult dicts",
    )
    parser.add_argument(
        "--output", default="mkdocs.yml", help="Output path for mkdocs.yml"
    )
    parser.add_argument("--site-name", default="", help="Override site_name")
    parser.add_argument("--site-url", default="", help="Override site_url")
    parser.add_argument("--repo-url", default="", help="Override repo_url")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    from utils import setup_logging
    setup_logging(args.log_level)

    with open(args.results_file, encoding="utf-8") as f:
        results: list[CopyResult] = json.load(f)

    generate_mkdocs_yml(
        results=results,
        output_path=Path(args.output),
        site_name=args.site_name or os.environ.get("MKDOCS_SITE_NAME", ""),
        site_url=args.site_url or os.environ.get("MKDOCS_SITE_URL", ""),
        repo_url=args.repo_url or os.environ.get("MKDOCS_REPO_URL", ""),
    )


if __name__ == "__main__":
    main()
