"""
Clone or update a single GitHub wiki repo (.wiki.git) into the local cache.

Usage (standalone):
    python scripts/clone_wiki.py --org MY_ORG --repo my-service --cache-dir wiki_cache

Requires env var: GH_TOKEN
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

from utils import mask_token_in_url, setup_logging, slugify

# Error message substrings that indicate an empty (never-initialized) wiki
_EMPTY_WIKI_INDICATORS = (
    "repository not found",
    "not found",
    "remote: repository not found",
    "does not exist",
    "empty repository",
    "the requested url returned error: 404",
)


def clone_wiki(
    org: str,
    repo_name: str,
    token: str,
    cache_dir: Path,
    timeout: int = 120,
) -> Path | None:
    """
    Clone or update the wiki for *repo_name* into *cache_dir*.

    Returns the path to the cloned wiki directory, or None if the wiki
    is empty / unreachable (has_wiki=true but no pages were ever created).

    Authentication is embedded in the HTTPS URL. The token is never printed
    to stdout/stderr — all log output uses a masked URL.
    """
    logger = logging.getLogger("wiki-sync.clone")
    slug = slugify(repo_name)
    wiki_dir = cache_dir / f"{slug}.wiki"

    # Construct the authenticated clone URL
    clone_url = f"https://{token}@github.com/{org}/{repo_name}.wiki.git"
    safe_url = mask_token_in_url(clone_url, token)

    if wiki_dir.exists():
        return _update_wiki(wiki_dir, clone_url, safe_url, logger, timeout)
    else:
        return _clone_wiki(wiki_dir, clone_url, safe_url, logger, timeout)


def _run_git(args: list[str], cwd: Path | None, timeout: int) -> subprocess.CompletedProcess:
    """Run a git command and return the result. Never raises on non-zero exit."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _is_empty_wiki_error(stderr: str) -> bool:
    """Return True if the git error message indicates a non-existent/empty wiki."""
    stderr_lower = stderr.lower()
    return any(indicator in stderr_lower for indicator in _EMPTY_WIKI_INDICATORS)


def _clone_wiki(
    wiki_dir: Path,
    clone_url: str,
    safe_url: str,
    logger: logging.Logger,
    timeout: int,
) -> Path | None:
    """Fresh clone of a wiki repo."""
    logger.info("Cloning wiki: %s", safe_url)
    wiki_dir.parent.mkdir(parents=True, exist_ok=True)

    result = _run_git(
        ["clone", "--depth=1", clone_url, str(wiki_dir)],
        cwd=None,
        timeout=timeout,
    )

    if result.returncode == 0:
        logger.info("Cloned successfully → %s", wiki_dir)
        return wiki_dir

    stderr = result.stderr or result.stdout
    if _is_empty_wiki_error(stderr):
        logger.debug(
            "Wiki is empty or not initialized for %s — skipping.", safe_url
        )
        return None

    # Unexpected error — log and propagate as None so the sync continues
    # for other repos, but the caller will record this repo as failed.
    logger.error(
        "Failed to clone %s (exit %d):\n%s",
        safe_url,
        result.returncode,
        _mask_output(stderr, clone_url.split("@")[0].split("//")[-1] if "@" in clone_url else ""),
    )
    return None


def _update_wiki(
    wiki_dir: Path,
    clone_url: str,
    safe_url: str,
    logger: logging.Logger,
    timeout: int,
) -> Path | None:
    """
    Update an existing cached wiki clone via fetch + reset.

    This is a hard reset so deleted pages are also removed locally.
    """
    logger.info("Updating cached wiki: %s", safe_url)

    # Determine the remote branch (master or main)
    branch = _detect_default_branch(wiki_dir, clone_url, logger, timeout)
    if branch is None:
        # Could not determine branch — treat as empty/unreachable
        return None

    # Set the remote URL (token may have rotated between runs)
    _run_git(["remote", "set-url", "origin", clone_url], cwd=wiki_dir, timeout=30)

    # Fetch latest
    fetch = _run_git(["fetch", "--depth=1", "origin", branch], cwd=wiki_dir, timeout=timeout)
    if fetch.returncode != 0:
        stderr = fetch.stderr or fetch.stdout
        if _is_empty_wiki_error(stderr):
            logger.debug("Wiki became empty for %s — skipping.", safe_url)
            return None
        logger.error(
            "git fetch failed for %s (exit %d):\n%s",
            safe_url,
            fetch.returncode,
            stderr[:500],
        )
        return None

    # Hard reset to remote HEAD
    reset = _run_git(
        ["reset", "--hard", f"origin/{branch}"],
        cwd=wiki_dir,
        timeout=30,
    )
    if reset.returncode != 0:
        logger.error(
            "git reset failed for %s: %s", safe_url, reset.stderr[:500]
        )
        return None

    logger.info("Updated successfully → %s", wiki_dir)
    return wiki_dir


def _detect_default_branch(
    wiki_dir: Path,
    clone_url: str,
    logger: logging.Logger,
    timeout: int,
) -> str | None:
    """
    Detect whether the wiki uses 'master' or 'main' as its default branch.

    First checks the local refs (fast), then falls back to ls-remote (network).
    Returns None if the wiki appears to be gone/empty.
    """
    # Check existing local remote-tracking refs
    result = _run_git(
        ["branch", "-r", "--format=%(refname:short)"],
        cwd=wiki_dir,
        timeout=10,
    )
    if result.returncode == 0:
        remote_branches = result.stdout.strip().splitlines()
        for candidate in ("origin/master", "origin/main"):
            if candidate in remote_branches:
                return candidate.split("/", 1)[1]

    # Fallback: ls-remote to query the remote directly
    ls = _run_git(["ls-remote", "--heads", clone_url], cwd=None, timeout=30)
    if ls.returncode != 0:
        stderr = ls.stderr or ls.stdout
        if _is_empty_wiki_error(stderr):
            return None
        # Unknown error; default to master and let fetch fail if wrong
        logger.warning("ls-remote failed, defaulting to 'master': %s", stderr[:200])
        return "master"

    for line in ls.stdout.splitlines():
        if line.endswith("refs/heads/master"):
            return "master"
        if line.endswith("refs/heads/main"):
            return "main"

    # No heads found → empty wiki
    return None


def _mask_output(text: str, token_fragment: str) -> str:
    """Rudimentary masking for error output."""
    if token_fragment:
        return text.replace(token_fragment, "***")
    return text


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Clone a single GitHub wiki repo.")
    parser.add_argument("--org", required=True, help="GitHub organization name")
    parser.add_argument("--repo", required=True, help="Repository name")
    parser.add_argument(
        "--cache-dir", default="wiki_cache", help="Local cache directory"
    )
    parser.add_argument(
        "--timeout", type=int, default=120, help="Git operation timeout (seconds)"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    setup_logging(args.log_level)
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("ERROR: GH_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    result = clone_wiki(
        org=args.org,
        repo_name=args.repo,
        token=token,
        cache_dir=Path(args.cache_dir),
        timeout=args.timeout,
    )

    if result:
        print(f"Wiki cloned/updated at: {result}")
    else:
        print("Wiki is empty or unreachable — skipped.")
        sys.exit(2)


if __name__ == "__main__":
    main()
