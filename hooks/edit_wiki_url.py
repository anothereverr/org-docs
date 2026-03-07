"""
MkDocs hook: rewrite the edit URL for wiki pages to point to the GitHub wiki source.

For a page at docs/{slug}/{Page}.md the edit URL becomes:
    https://github.com/{org}/{slug}/wiki/{Page}/_edit

This replaces the default MkDocs behaviour (which would construct a URL
pointing at the org-docs repository itself, not the wiki it was synced from).

The GitHub organization name is read from ``extra.gh_org`` in mkdocs.yml,
injected by generate_nav.py when the org is known.

Top-level pages (docs/index.md) and internal directories (docs/_assets/)
get their edit URL cleared so no edit button is shown for them.
"""
from __future__ import annotations

from pathlib import Path


def on_page_context(context, *, page, config, nav, **kwargs):
    """
    Set ``page.edit_url`` to the GitHub wiki edit URL for every wiki page.

    Called by MkDocs after page context is built, before rendering.
    Returning *context* unchanged is required by the hook API.
    """
    src = page.file.src_path  # relative path inside docs/, e.g. "my-service/API.md"
    parts = Path(src).parts

    # Pages without a repo-slug prefix (e.g. "index.md") have no wiki source
    if len(parts) < 2:
        page.edit_url = None
        return context

    slug = parts[0]

    # Internal asset directories are not wiki pages
    if slug.startswith("_"):
        page.edit_url = None
        return context

    page_stem = Path(parts[-1]).stem  # "API" from "API.md"

    # GitHub wiki stores the landing page as "Home", which we rename to "index"
    page_name = "Home" if page_stem == "index" else page_stem

    org = (config.get("extra") or {}).get("gh_org", "")
    if not org:
        # org not configured — leave edit_url as-is (may be None or repo default)
        return context

    # GitHub is case-insensitive for repo names, so using the slug directly works
    # even when the original repo name used different casing or underscores.
    page.edit_url = f"https://github.com/{org}/{slug}/wiki/{page_name}/_edit"
    return context
