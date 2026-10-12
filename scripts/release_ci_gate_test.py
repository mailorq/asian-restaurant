"""release_ci_gate_test.py: the release gate against a local stand-in of the GitHub API, one commit per case"""

import json
import os
import pathlib
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

GATE = pathlib.Path(__file__).with_name("release_ci_gate.py")


def sha(n: int) -> str:
    return f"{n:040x}"


def run(number, status="completed", conclusion="success", name="CI", head=None, attempt=1):
    return {"name": name, "head_sha": head, "run_number": number, "run_attempt": attempt, "status": status,
            "conclusion": conclusion, "html_url": f"https://example.invalid/runs/{number}"}


# commit -> (expected exit, runs the api lists for it); head None means the commit itself
CASES = {
    "green": (0, [run(10)]),
    "green after a failed earlier run": (0, [run(10, conclusion="failure"), run(11)]),
    "green on a rerun of the same run": (0, [run(10, attempt=2)]),
    "failed": (1, [run(10, conclusion="failure")]),
    "an older green run, the latest failed": (1, [run(10), run(11, conclusion="failure")]),
    "still running": (1, [run(10, status="in_progress", conclusion=None)]),
    "cancelled": (1, [run(10, conclusion="cancelled")]),
    "no run": (1, []),
    "only another workflow": (1, [run(10, name="Management")]),
    "a run of another commit": (1, [run(10, head="f" * 40)]),
}
COMMITS = {sha(i + 1): label for i, label in enumerate(CASES)}


class FakeGitHub(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        query = parse_qs(urlparse(self.path).query)
        commit = query.get("head_sha", [""])[0]
        if commit == sha(500):
            self.send_response(500)
            self.end_headers()
            return
        runs = [{**r, "head_sha": r["head_sha"] or commit} for r in CASES.get(COMMITS.get(commit), (0, []))[1]]
        body = json.dumps({"total_count": len(runs), "workflow_runs": runs}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def gate(commit: str, api: str) -> int:
    env = {**os.environ, "RELEASE_CI_API": api, "RELEASE_CI_REPO": "owner/repo"}
    env.pop("GITHUB_TOKEN", None)
    return subprocess.run([sys.executable, str(GATE), commit], env=env, capture_output=True, timeout=60).returncode


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{server.server_address[1]}"
    failed = 0
    checks = [(label, gate(commit, api), CASES[label][0]) for commit, label in COMMITS.items()]
    checks.append(("the api answers 500", gate(sha(500), api), 1))
    server.shutdown()
    checks.append(("the api cannot be reached", gate(sha(1), api), 1))
    checks.append(("an abbreviated sha", gate("abc1234", api), 1))
    for label, got, expected in checks:
        ok = got == expected
        failed += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"exit {got}, expected {expected}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
