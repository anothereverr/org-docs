"""
Shared utilities: slugify, GitHub API pagination, logging setup, token masking.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Iterator

import requests


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(level: str = "INFO") -> logging.Logger:
    """Return a module-level logger with a timestamped format."""
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        level=getattr(logging, level.upper(), logging.INFO),
    )
    return logging.getLogger("wiki-sync")


# ---------------------------------------------------------------------------
# String helpers
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """
    Convert a repo name to a URL-safe, filesystem-safe slug.

    Examples:
        "My Service"  -> "my-service"
        "my_service"  -> "my-service"
        "My.Service!" -> "my-service"
        "répo-nàme"   -> "r-po-n-me"  (ASCII fallback)
    """
    # Lowercase first
    slug = name.lower()
    # Replace underscores and spaces with hyphens
    slug = re.sub(r"[\s_]+", "-", slug)
    # Remove any character that is not alphanumeric or hyphen
    slug = re.sub(r"[^a-z0-9\-]", "-", slug)
    # Collapse multiple consecutive hyphens
    slug = re.sub(r"-{2,}", "-", slug)
    # Strip leading/trailing hyphens
    slug = slug.strip("-")
    # Truncate to 50 characters to stay well under Windows MAX_PATH limits
    slug = slug[:50].rstrip("-")
    return slug or "repo"


def safe_filename(name: str) -> str:
    """
    Strip path-traversal characters from a bare filename.
    Returns only the basename portion after removing directory separators.
    """
    # Take only the basename — prevents ../../../etc/passwd style attacks
    name = re.sub(r"[/\\]", "_", name)
    # Remove null bytes
    name = name.replace("\x00", "")
    return name


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def get_github_headers(token: str) -> dict:
    """Return standard GitHub REST API v3 request headers."""
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def paginate_github(url: str, headers: dict) -> Iterator[dict]:
    """
    Yield every item from a paginated GitHub API list endpoint.

    Follows the Link header (rel="next") until exhausted.
    Handles 429 / secondary rate limits via Retry-After.
    """
    logger = logging.getLogger("wiki-sync.paginate")
    current_url: str | None = url

    while current_url:
        response = requests.get(current_url, headers=headers, timeout=30)

        # Handle rate limiting
        if response.status_code == 429 or (
            response.status_code == 403
            and "rate limit" in response.text.lower()
        ):
            retry_after = int(response.headers.get("Retry-After", "60"))
            logger.warning("Rate limited. Waiting %d seconds.", retry_after)
            time.sleep(retry_after)
            continue

        response.raise_for_status()
        data = response.json()

        if isinstance(data, list):
            yield from data
        else:
            # Some endpoints wrap results (e.g., search)
            items = data.get("items", [data])
            yield from items

        # Follow pagination
        link_header = response.headers.get("Link", "")
        next_url = _parse_next_link(link_header)
        current_url = next_url


def _parse_next_link(link_header: str) -> str | None:
    """Extract the 'next' URL from a GitHub Link header, or None."""
    if not link_header:
        return None
    # Format: <https://api.github.com/...>; rel="next", <...>; rel="last"
    for part in link_header.split(","):
        part = part.strip()
        if 'rel="next"' in part:
            match = re.search(r"<([^>]+)>", part)
            if match:
                return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

def mask_token_in_url(url: str, token: str) -> str:
    """Replace a PAT embedded in a URL with '***' for safe log output."""
    if not token:
        return url
    return url.replace(token, "***")
