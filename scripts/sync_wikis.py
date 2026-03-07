"""
Main orchestrator: discover → clone → copy → rewrite → generate nav.

Usage:
    python scripts/sync_wikis.py \\
        --org MY_ORG \\
        --docs-dir docs \\
        --cache-dir wiki_cache \\
        [--parallelism 4] \\
        [--dry-run] \\
        [--repo-filter my-service] \\
        [--skip-archived] \\
        [--log-level DEBUG]

Required env vars:
    GH_TOKEN   GitHub Personal Access Token (repo or public_repo scope)
    GH_ORG     GitHub organization name (can also be set via --org)

Optional env vars:
    MKDOCS_SITE_NAME   Override the portal site_name in mkdocs.yml
    MKDOCS_SITE_URL    Override the site_url in mkdocs.yml
    MKDOCS_REPO_URL    Override the repo_url in mkdocs.yml
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Scripts directory is in sys.path when run from project root; adjust if needed.
_SCRIPTS_DIR = Path(__file__).parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from clone_wiki import clone_wiki
from copy_content import copy_wiki_content, CopyResult
from discover_wikis import discover_wikis
from generate_nav import generate_mkdocs_yml
from utils import setup_logging


def run_sync(
    org: str,
    token: str,
    docs_dir: Path,
    cache_dir: Path,
    parallelism: int = 4,
    dry_run: bool = False,
    repo_filter: str = "",
    skip_archived: bool = False,
    clone_timeout: int = 120,
    summary_file: str | Path | None = None,
) -> bool:
    """
    Execute the full sync pipeline.

    Returns True if all repos succeeded (or were intentionally skipped),
    False if one or more repos encountered an unexpected error.
    """
    logger = logging.getLogger("wiki-sync")

    # ------------------------------------------------------------------
    # 1. Discover repos with wikis
    # ------------------------------------------------------------------
    logger.info("=== Step 1: Discovering wikis in org '%s' ===", org)
    all_repos = discover_wikis(org, token, skip_archived=skip_archived)

    if not all_repos:
        logger.warning("No wiki-enabled repositories found. Nothing to sync.")
        return True

    # Optional filter for a single repo (useful for debugging)
    if repo_filter:
        all_repos = [r for r in all_repos if r["slug"] == repo_filter or r["name"] == repo_filter]
        if not all_repos:
            logger.error("Repo filter '%s' matched no repositories.", repo_filter)
            return False
        logger.info("Filtered to %d repo(s): %s", len(all_repos), [r["name"] for r in all_repos])

    logger.info("Processing %d wiki-enabled repo(s).", len(all_repos))

    if dry_run:
        logger.info("[DRY RUN] Would process: %s", [r["name"] for r in all_repos])
        return True

    # ------------------------------------------------------------------
    # 2. Clone + copy each wiki (parallel)
    # ------------------------------------------------------------------
    logger.info("=== Step 2-4: Cloning, copying, and rewriting wikis ===")
    docs_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    successful_results: list[CopyResult] = []
    failed_repos: list[str] = []
    skipped_repos: list[str] = []

    def _process_repo(repo_meta: dict) -> tuple[str, CopyResult | None, str]:
        """
        Clone and copy a single repo. Returns (name, result_or_None, status).
        status is one of: "ok", "skipped", "error"
        """
        name = repo_meta["name"]
        slug = repo_meta["slug"]
        repo_logger = logging.getLogger(f"wiki-sync.{slug}")

        # Clone / update
        try:
            wiki_path = clone_wiki(
                org=org,
                repo_name=name,
                token=token,
                cache_dir=cache_dir,
                timeout=clone_timeout,
            )
        except Exception as exc:
            repo_logger.error("Unexpected error cloning %s: %s", name, exc)
            return name, None, "error"

        if wiki_path is None:
            repo_logger.info("Wiki is empty or unreachable — skipping %s", name)
            return name, None, "skipped"

        # Copy content
        try:
            result = copy_wiki_content(
                source_dir=wiki_path,
                docs_dir=docs_dir,
                slug=slug,
            )
        except Exception as exc:
            repo_logger.error("Unexpected error copying content for %s: %s", name, exc)
            return name, None, "error"

        return name, result, "ok"

    # Use ThreadPoolExecutor for parallel network-bound git clones
    effective_parallelism = max(1, min(parallelism, len(all_repos)))

    with ThreadPoolExecutor(max_workers=effective_parallelism) as pool:
        future_to_repo = {
            pool.submit(_process_repo, repo): repo
            for repo in all_repos
        }

        for future in as_completed(future_to_repo):
            repo_meta = future_to_repo[future]
            try:
                name, result, status = future.result()
            except Exception as exc:
                logger.error("Unhandled exception for %s: %s", repo_meta["name"], exc)
                failed_repos.append(repo_meta["name"])
                continue

            if status == "ok" and result is not None:
                successful_results.append(result)
            elif status == "skipped":
                skipped_repos.append(name)
            else:
                failed_repos.append(name)

    logger.info(
        "Sync complete: %d ok, %d skipped, %d failed.",
        len(successful_results),
        len(skipped_repos),
        len(failed_repos),
    )

    if failed_repos:
        logger.error("Failed repos: %s", failed_repos)

    # ------------------------------------------------------------------
    # 5. Generate mkdocs.yml with full nav
    # ------------------------------------------------------------------
    logger.info("=== Step 5: Generating mkdocs.yml ===")

    mkdocs_path = docs_dir.parent / "mkdocs.yml"

    generate_mkdocs_yml(
        results=successful_results,
        output_path=mkdocs_path,
        site_name=os.environ.get("MKDOCS_SITE_NAME", ""),
        site_url=os.environ.get("MKDOCS_SITE_URL", ""),
        repo_url=os.environ.get("MKDOCS_REPO_URL", ""),
    )

    # ------------------------------------------------------------------
    # 6. Write sync_summary.json (consumed by the Actions job summary step)
    # ------------------------------------------------------------------
    if summary_file is not None:
        all_broken = [
            {"repo": r["slug"], "source_file": bl.source_file, "target": bl.target}
            for r in successful_results
            for bl in r.get("broken_links", [])
        ]
        all_collisions = [
            {"repo": r["slug"], "filename": ic.filename}
            for r in successful_results
            for ic in r.get("image_collisions", [])
        ]
        summary = {
            "repos_ok":         len(successful_results),
            "repos_skipped":    len(skipped_repos),
            "repos_failed":     len(failed_repos),
            "broken_links":     all_broken,
            "image_collisions": all_collisions,
            "failed":           failed_repos,
            "skipped":          skipped_repos,
        }
        Path(summary_file).write_text(json.dumps(summary, indent=2), encoding="utf-8")
        logger.info("Summary written to %s", summary_file)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    logger.info("=== Sync finished ===")
    logger.info("  Repos synced:  %d", len(successful_results))
    logger.info("  Repos skipped: %d (empty or unreachable wikis)", len(skipped_repos))
    logger.info("  Repos failed:  %d", len(failed_repos))
    logger.info("  mkdocs.yml:    %s", mkdocs_path)

    # Return False if any repo had an unexpected error
    return len(failed_repos) == 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync all GitHub org wikis into a central MkDocs documentation portal.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--org",
        default=os.environ.get("GH_ORG", ""),
        help="GitHub organization name (or set GH_ORG env var)",
    )
    parser.add_argument(
        "--docs-dir",
        default="docs",
        help="Path to the docs/ directory (default: docs)",
    )
    parser.add_argument(
        "--cache-dir",
        default="wiki_cache",
        help="Path to the wiki cache directory (default: wiki_cache)",
    )
    parser.add_argument(
        "--parallelism",
        type=int,
        default=4,
        help="Number of concurrent clone operations (default: 4)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"),
        help="Discover wikis but do not clone or write anything",
    )
    parser.add_argument(
        "--repo-filter",
        default=os.environ.get("REPO_FILTER", ""),
        help="Only sync this specific repo (by name or slug)",
    )
    parser.add_argument(
        "--skip-archived",
        action="store_true",
        default=False,
        help="Skip archived repositories",
    )
    parser.add_argument(
        "--clone-timeout",
        type=int,
        default=120,
        help="Timeout in seconds for each git clone/fetch operation (default: 120)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--summary-file",
        default="",
        help="Write sync summary JSON to this path (default: no file written). "
             "The JSON is consumed by the GitHub Actions job summary step.",
    )
    args = parser.parse_args()

    setup_logging(args.log_level)
    logger = logging.getLogger("wiki-sync")

    token = os.environ.get("GH_TOKEN", "")
    if not token:
        logger.error("GH_TOKEN environment variable is not set.")
        sys.exit(1)

    if not args.org:
        logger.error("GitHub organization not specified. Use --org or set GH_ORG.")
        sys.exit(1)

    success = run_sync(
        org=args.org,
        token=token,
        docs_dir=Path(args.docs_dir),
        cache_dir=Path(args.cache_dir),
        parallelism=args.parallelism,
        dry_run=args.dry_run,
        repo_filter=args.repo_filter,
        skip_archived=args.skip_archived,
        clone_timeout=args.clone_timeout,
        summary_file=args.summary_file or None,
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
