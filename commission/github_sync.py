"""Optional write-through to GitHub for the Settings JSON files.

Streamlit Community Cloud's free tier has an ephemeral disk: any edit made
to `data/*.json` from the running app is lost the next time the container
restarts (every code push, ~weekly idle timeout, or random infrastructure
rebalance). To make Settings edits permanent, we round-trip them back to
the GitHub repo via the Contents API as soon as the user clicks Save.

Configuration (read from Streamlit secrets):
  github_pat  -- a fine-grained Personal Access Token with
                 "Contents: Read and write" on this repo only.
  github_repo -- e.g. "anusha626/lb-sales-commission".
  github_branch -- defaults to "main" if unset.

When the secrets aren't set (e.g. local dev) sync is skipped silently.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

import requests


GITHUB_API = "https://api.github.com"


@dataclass
class SyncResult:
    ok: bool
    message: str


@dataclass
class GitHubConfig:
    pat: str
    repo: str  # "owner/repo"
    branch: str = "main"

    @property
    def configured(self) -> bool:
        return bool(self.pat and self.repo)


def push_file(
    cfg: GitHubConfig,
    repo_relative_path: str,
    content: str,
    commit_message: str,
    *,
    timeout: int = 20,
) -> SyncResult:
    """Create or update `repo_relative_path` on GitHub with `content`.

    Uses the Contents API (PUT /repos/{owner}/{repo}/contents/{path}). When
    the file already exists we must include its current SHA; on creation
    we omit it. Returns a SyncResult so the UI can render success/failure
    without blowing up.
    """
    if not cfg.configured:
        return SyncResult(False, "GitHub sync not configured")

    api_url = f"{GITHUB_API}/repos/{cfg.repo}/contents/{repo_relative_path}"
    headers = {
        "Authorization": f"Bearer {cfg.pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Step 1: discover the current SHA (if any).
    sha: str | None = None
    try:
        r = requests.get(api_url, headers=headers, params={"ref": cfg.branch}, timeout=timeout)
    except requests.RequestException as e:
        return SyncResult(False, f"GitHub GET network error: {e}")
    if r.status_code == 200:
        sha = r.json().get("sha")
    elif r.status_code != 404:
        return SyncResult(
            False,
            f"GitHub GET failed ({r.status_code}): {_short(r.text)}",
        )

    # Step 2: PUT the new content.
    body = {
        "message": commit_message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": cfg.branch,
    }
    if sha:
        body["sha"] = sha

    try:
        r = requests.put(api_url, headers=headers, json=body, timeout=timeout)
    except requests.RequestException as e:
        return SyncResult(False, f"GitHub PUT network error: {e}")

    if r.status_code in (200, 201):
        commit_url = (r.json().get("commit") or {}).get("html_url", "")
        return SyncResult(True, f"Synced. {commit_url}".strip())
    return SyncResult(
        False, f"GitHub PUT failed ({r.status_code}): {_short(r.text)}"
    )


def push_local_path(
    cfg: GitHubConfig,
    local_path: Path,
    project_root: Path,
    commit_message: str,
) -> SyncResult:
    """Convenience wrapper: read `local_path` and push it under its repo-
    relative path."""
    rel = local_path.relative_to(project_root).as_posix()
    return push_file(cfg, rel, local_path.read_text(encoding="utf-8"), commit_message)


def _short(text: str, n: int = 200) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:n] + ("…" if len(text) > n else "")
