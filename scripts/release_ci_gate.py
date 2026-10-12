"""release_ci_gate.py <commit sha>: a release starts only from a commit whose CI run finished green

asks the GitHub API for the runs of the CI workflow on exactly this commit and passes only when the latest of them
completed with success. anything else refuses, an API that cannot be reached included: a new release waits for
GitHub, while the commands that keep a running stack alive (dc up, dc exec, a restore) never go through here
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

REPO = os.environ.get("RELEASE_CI_REPO", "mailorq/asian-restaurant")
API = os.environ.get("RELEASE_CI_API", "https://api.github.com").rstrip("/")
WORKFLOW = "CI"


def refuse(reason: str) -> None:
    print(f"release refused: {reason}", file=sys.stderr)
    sys.exit(1)


def main(argv: list[str]) -> None:
    if len(argv) != 2 or not re.fullmatch(r"[0-9a-f]{40}", argv[1]):
        refuse("pass the full 40 character sha of the commit being released")
    sha = argv[1]
    request = urllib.request.Request(
        f"{API}/repos/{REPO}/actions/runs?head_sha={sha}&per_page=100",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "release-ci-gate"},
    )
    if os.environ.get("GITHUB_TOKEN"):
        request.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            runs = json.load(response).get("workflow_runs") or []
    except (urllib.error.URLError, TimeoutError, ValueError, AttributeError) as exc:
        refuse(f"the CI state of {sha[:12]} could not be read ({type(exc).__name__})")
    # the same commit pushed twice runs twice; the latest run, at its latest attempt, is the one that counts
    runs = [run for run in runs if run.get("name") == WORKFLOW and run.get("head_sha") == sha]
    if not runs:
        refuse(f"no {WORKFLOW} run for {sha[:12]}")
    latest = max(runs, key=lambda run: (run.get("run_number") or 0, run.get("run_attempt") or 0))
    if latest.get("status") != "completed":
        refuse(f"{WORKFLOW} run {latest.get('run_number')} for {sha[:12]} is {latest.get('status')}")
    if latest.get("conclusion") != "success":
        refuse(f"{WORKFLOW} run {latest.get('run_number')} for {sha[:12]} ended {latest.get('conclusion')}")
    print(f"{WORKFLOW} run {latest.get('run_number')} for {sha[:12]} succeeded: {latest.get('html_url')}")


if __name__ == "__main__":
    main(sys.argv)
