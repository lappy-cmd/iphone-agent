"""The agent's access to its own repo: read files, commit a change on a branch, watch the tests, merge.

Everything goes through the GitHub REST API (no git on the server). A change only reaches `main` (and
so Render) after the GitHub Actions tests pass on its branch.
"""
from __future__ import annotations

import base64
import posixpath

# Paths the agent may never write: CI (and its own guard rails), deploy config, secrets.
FORBIDDEN_PREFIXES = (".github/", ".git/")
FORBIDDEN_NAMES = {"render.yaml", ".env", "token.json", "credentials.json", ".gitattributes"}
SOURCE_EXTS = {".py", ".md", ".txt", ".yaml", ".yml", ".toml", ".sh", ".json", ".example", ".plist"}


class SelfRepoError(Exception):
    pass


def safe_path(path: str) -> str | None:
    """Normalized repo path if the agent may write it, else None."""
    path = str(path or "").replace("\\", "/").lstrip("/")
    norm = posixpath.normpath(path)
    if not path or norm.startswith("..") or norm != path or norm == ".":
        return None
    base = posixpath.basename(norm)
    if norm.startswith(FORBIDDEN_PREFIXES) or base in FORBIDDEN_NAMES or base.startswith(".env"):
        return None
    return norm


def is_source(path: str) -> bool:
    if "/.venv/" in f"/{path}" or "__pycache__" in path:
        return False
    return posixpath.splitext(path)[1] in SOURCE_EXTS or posixpath.basename(path) == ".gitignore"


class SelfRepo:
    API = "https://api.github.com"

    def __init__(self, token: str, repo: str, branch: str = "main", http=None):
        import httpx

        if not token or not repo:
            raise SelfRepoError("SELF_GITHUB_TOKEN / SELF_REPO not configured")
        self.repo = repo
        self.branch = branch
        self._http = http or httpx.Client(
            timeout=30,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def _req(self, method: str, path: str, ok=(200,), **kw):
        try:
            r = self._http.request(method, f"{self.API}/repos/{self.repo}{path}", **kw)
        except Exception as exc:
            raise SelfRepoError(f"GitHub unreachable: {exc}") from exc
        if r.status_code not in ok:
            raise SelfRepoError(f"GitHub {method} {path.split('?')[0]} -> {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    def head(self) -> str:
        return self._req("GET", f"/git/ref/heads/{self.branch}")["object"]["sha"]

    def list_files(self, sha: str) -> list[str]:
        tree = self._req("GET", f"/git/trees/{sha}?recursive=1")
        return sorted(t["path"] for t in tree.get("tree", []) if t.get("type") == "blob" and is_source(t["path"]))

    def read(self, path: str, sha: str) -> str:
        d = self._req("GET", f"/contents/{path}?ref={sha}")
        return base64.b64decode(d.get("content", "")).decode("utf-8", errors="replace")

    def commit_branch(self, base_sha: str, files: list[dict], message: str, branch: str) -> str:
        """Commit `files` ([{path, content}]) on top of base_sha as a new branch. Returns the commit sha."""
        base_tree = self._req("GET", f"/git/commits/{base_sha}")["tree"]["sha"]
        entries = []
        for f in files:
            path = safe_path(f["path"])
            if path is None:
                raise SelfRepoError(f"not allowed to change {f['path']}")
            entries.append({"path": path, "mode": "100644", "type": "blob", "content": f["content"]})
        tree = self._req("POST", "/git/trees", ok=(201,), json={"base_tree": base_tree, "tree": entries})
        commit = self._req("POST", "/git/commits", ok=(201,),
                           json={"message": message, "tree": tree["sha"], "parents": [base_sha]})
        self._req("POST", "/git/refs", ok=(201,), json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        return commit["sha"]

    def tests(self, sha: str) -> tuple[str, str]:
        """('pending'|'success'|'failure', url) for the GitHub Actions runs on that commit."""
        runs = self._req("GET", f"/actions/runs?head_sha={sha}").get("workflow_runs", [])
        if not runs:
            return "pending", ""
        if any(r.get("status") != "completed" for r in runs):
            return "pending", runs[0].get("html_url", "")
        bad = [r for r in runs if r.get("conclusion") != "success"]
        return ("failure", bad[0].get("html_url", "")) if bad else ("success", runs[0].get("html_url", ""))

    def merge(self, branch: str, message: str) -> None:
        self._req("POST", "/merges", ok=(201, 204),
                  json={"base": self.branch, "head": branch, "commit_message": message})

    def delete_branch(self, branch: str) -> None:
        try:
            self._req("DELETE", f"/git/refs/heads/{branch}", ok=(204,))
        except SelfRepoError:
            pass
