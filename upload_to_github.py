"""
upload_to_github.py
====================
Uploads monitor.py to your GitHub repo via the GitHub API.
No truncation. No paste. Exact file every time.

Usage:
  1. Put this file in the same folder as monitor.py
  2. Run: python3 upload_to_github.py
  3. Enter your GitHub Personal Access Token when prompted

To create a token:
  GitHub → Settings → Developer settings → Personal access tokens
  → Tokens (classic) → Generate new token → check 'repo' → copy it
"""

import base64
import json
import urllib.request
import urllib.error
import getpass
import os

REPO  = "liejofernandus9/portfolio-monitor"
FILES = [
    ("monitor.py",               "monitor.py"),
    (".github/workflows/monitor.yml", "monitor.yml"),
]

def upload_file(token: str, repo: str, repo_path: str, local_path: str):
    if not os.path.exists(local_path):
        print(f"  ⚠️  Skipping {local_path} — file not found")
        return

    with open(local_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")

    api_url = f"https://api.github.com/repos/{repo}/contents/{repo_path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept":        "application/vnd.github.v3+json",
        "Content-Type":  "application/json",
    }

    # Get current SHA
    req = urllib.request.Request(api_url, headers=headers)
    sha = None
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
            sha  = data.get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"  ❌ Could not fetch SHA: {e}")
            return

    # Upload
    payload = {
        "message": f"⬆️ Upload {os.path.basename(local_path)} via upload script",
        "content": encoded,
    }
    if sha:
        payload["sha"] = sha

    req = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            code = resp.status
        action = "Updated" if sha else "Created"
        print(f"  ✅ {action} {repo_path} (HTTP {code})")
    except urllib.error.HTTPError as e:
        print(f"  ❌ Upload failed: {e.code} {e.read().decode()[:200]}")


def main():
    print("=" * 50)
    print("Portfolio Monitor — GitHub Uploader")
    print("=" * 50)
    print()

    token = getpass.getpass("Paste your GitHub Personal Access Token: ").strip()
    if not token:
        print("No token provided — exiting.")
        return

    print()
    for repo_path, local_path in FILES:
        print(f"Uploading {local_path} → {repo_path}")
        upload_file(token, REPO, repo_path, local_path)

    print()
    print("Done! Go to GitHub Actions and trigger a manual run.")
    print(f"Repo: https://github.com/{REPO}/actions")


if __name__ == "__main__":
    main()
