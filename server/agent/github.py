"""Push generated code into a GitHub repo via the REST contents API (no git needed)."""
from __future__ import annotations

import base64


class GitHubError(Exception):
    pass


class GitHub:
    API = "https://api.github.com"

    def __init__(self, token: str, repo: str, http=None):
        import httpx

        if not token or not repo:
            raise GitHubError("GITHUB_TOKEN / GITHUB_REPO not configured")
        self._repo = repo
        self._http = http or httpx.Client(
            timeout=20,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def push_project(self, folder: str, files: list[dict], message: str) -> str:
        """Create files under `folder/`. Returns the folder's web URL."""
        for f in files:
            path = f"{folder}/{f['path']}"
            body = {
                "message": f"{message} ({f['path']})",
                "content": base64.b64encode(f["content"].encode("utf-8")).decode(),
            }
            try:
                r = self._http.put(f"{self.API}/repos/{self._repo}/contents/{path}", json=body)
            except Exception as exc:
                raise GitHubError(str(exc)) from exc
            if r.status_code not in (200, 201):
                raise GitHubError(f"{r.status_code} on {path}: {r.text[:200]}")
        return f"https://github.com/{self._repo}/tree/HEAD/{folder}"
