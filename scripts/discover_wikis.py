"""
Discover repositories in a GitHub organization or personal account that have wikis enabled.

Works with both GitHub organizations (/orgs/{name}/repos) and personal accounts
(/users/{name}/repos). The account type is auto-detected via the /users/{name} endpoint.

Usage (standalone):
    python scripts/discover_wikis.py --org MY_ORG_OR_USERNAME [--skip-archived]

Requires env var: GH_TOKEN
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import logging

import requests

from utils import get_github_headers, paginate_github, setup_logging, slugify

GITHUB_API = "https://api.github.com"


def _resolve_account_type(account: str, headers: dict) -> str:
    """
    Return 'org' or 'user' by querying the /users/{account} endpoint.
    Falls back to 'user' on any error so personal accounts always work.
    """
    logger = logging.getLogger("wiki-sync.discover")
    try:
        resp = requests.get(f"{GITHUB_API}/users/{account}", headers=headers, timeout=15)
        if resp.status_code == 200:
            account_type = resp.json().get("type", "User")
            if account_type == "Organization":
                logger.info("Account '%s' is an Organization.", account)
                return "org"
            else:
                logger.info("Account '%s' is a User (personal account).", account)
                return "user"
    except Exception as exc:
        logger.warning("Could not resolve account type for '%s': %s — defaulting to 'user'", account, exc)
    return "user"


def discover_wikis(
    org: str,
    token: str,
    skip_archived: bool = False,
) -> list[dict]:
    """
    Return a list of repos in *org* (organization or personal account) that have wikis enabled.

    Auto-detects whether *org* is a GitHub Organization or a personal User account
    and calls the appropriate API endpoint.

    Each entry:
        {
            "name":            str   repo name as-is (e.g. "my-service")
            "slug":            str   URL-safe slug   (e.g. "my-service")
            "wiki_clone_url":  str   HTTPS clone URL for the .wiki.git repo
            "default_branch":  str   default branch of the wiki ("master" | "main")
        }

    Note: has_wiki=true does NOT guarantee a wiki has any pages. The clone
    step is responsible for detecting and skipping empty wikis.
    """
    logger = logging.getLogger("wiki-sync.discover")
    headers = get_github_headers(token)

    account_type = _resolve_account_type(org, headers)

    if account_type == "org":
        # Organizations: type=sources excludes forks
        url = f"{GITHUB_API}/orgs/{org}/repos?type=sources&per_page=100"
    else:
        # Personal accounts: type=owner returns only repos owned by the user
        url = f"{GITHUB_API}/users/{org}/repos?type=owner&per_page=100"

    logger.info("Fetching repositories for %s '%s'", account_type, org)

    repos_with_wikis: list[dict] = []
    total = 0

    for repo in paginate_github(url, headers):
        total += 1

        if not repo.get("has_wiki", False):
            continue

        if skip_archived and repo.get("archived", False):
            logger.debug("Skipping archived repo: %s", repo["name"])
            continue

        name = repo["name"]
        slug = slugify(name)

        # GitHub wiki clone URL is always <repo_clone_url without .git>.wiki.git
        # Construct from HTTPS clone URL to avoid SSH key requirements in CI
        clone_url = repo.get("clone_url", "")  # https://github.com/OWNER/REPO.git
        if clone_url.endswith(".git"):
            wiki_clone_url = clone_url[:-4] + ".wiki.git"
        else:
            wiki_clone_url = f"https://github.com/{org}/{name}.wiki.git"

        repos_with_wikis.append(
            {
                "name": name,
                "slug": slug,
                "wiki_clone_url": wiki_clone_url,
                # Wiki repos default to 'master'; we try both at clone time
                "default_branch": "master",
            }
        )
        logger.debug("Found wiki-enabled repo: %s (slug: %s)", name, slug)

    logger.info(
        "Scanned %d repos, found %d with wikis enabled.",
        total,
        len(repos_with_wikis),
    )
    return repos_with_wikis


# ---------------------------------------------------------------------------
# CLI entry point for standalone use / debugging
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="List GitHub org repos with wikis enabled."
    )
    parser.add_argument("--org", required=True, help="GitHub organization name")
    parser.add_argument(
        "--skip-archived",
        action="store_true",
        default=False,
        help="Exclude archived repositories",
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

    results = discover_wikis(args.org, token, skip_archived=args.skip_archived)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
