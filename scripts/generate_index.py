#!/usr/bin/env python3
"""
SteamVault metadata generator.

Reads SteamForbiddenDB/SteamVault_DB through the GitHub REST API and writes
SteamVault_Index/metadata/index.json.

Important:
- No package binaries are downloaded.
- Unchanged branches are reused from the previous index by comparing commit SHA.
- For changed branches, Git Trees is used to calculate file count and total blob size.
- Optional steamvault.json in each package branch supplies friendly metadata.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_VERSION = "2026-03-10"
DEFAULT_OWNER = "SteamForbiddenDB"
DEFAULT_REPO = "SteamVault_DB"


class GitHubError(RuntimeError):
    pass


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


class GitHub:
    def __init__(self, token: str, owner: str, repo: str):
        if not token:
            raise GitHubError("STEAMVAULT_DB_READ_TOKEN secret is missing.")
        self.token = token
        self.owner = owner
        self.repo = repo
        self.base = f"https://api.github.com/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"

    def request(self, path: str):
        url = path if path.startswith("https://") else self.base + path
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "SteamVault-Index-Generator",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            remaining = e.headers.get("X-RateLimit-Remaining")
            reset = e.headers.get("X-RateLimit-Reset")
            extra = f" rate_remaining={remaining} reset={reset}" if remaining is not None else ""
            raise GitHubError(f"GitHub API {e.code} for {url}: {body[:700]}{extra}") from e

    def list_branches(self):
        branches = []
        page = 1
        while True:
            batch = self.request(f"/branches?per_page=100&page={page}")
            if not batch:
                break
            branches.extend(
                {"name": item["name"], "sha": item["commit"]["sha"]}
                for item in batch
            )
            if len(batch) < 100:
                break
            page += 1
        return branches

    def get_commit(self, sha: str):
        return self.request(f"/git/commits/{urllib.parse.quote(sha, safe='')}")

    def get_tree(self, tree_sha: str, recursive: bool = True):
        suffix = "?recursive=1" if recursive else ""
        return self.request(f"/git/trees/{urllib.parse.quote(tree_sha, safe='')}{suffix}")

    def get_blob_text(self, blob_sha: str) -> str:
        blob = self.request(f"/git/blobs/{urllib.parse.quote(blob_sha, safe='')}")
        if blob.get("encoding") != "base64":
            raise GitHubError(f"Unsupported blob encoding for {blob_sha}: {blob.get('encoding')}")
        return base64.b64decode(blob.get("content", "")).decode("utf-8")


def walk_tree(github: GitHub, root_tree_sha: str):
    """Return a complete flat tree. Falls back to subtree walking if recursive response is truncated."""
    recursive = github.get_tree(root_tree_sha, recursive=True)
    if not recursive.get("truncated"):
        return recursive.get("tree", [])

    print("::warning::Recursive Git tree was truncated; walking subtrees individually.", file=sys.stderr)
    out = []
    stack = [("", root_tree_sha)]
    while stack:
        prefix, tree_sha = stack.pop()
        current = github.get_tree(tree_sha, recursive=False)
        for item in current.get("tree", []):
            path = f"{prefix}/{item['path']}" if prefix else item["path"]
            copied = dict(item)
            copied["path"] = path
            if item.get("type") == "tree":
                stack.append((path, item["sha"]))
            else:
                out.append(copied)
    return out


def branch_is_excluded(name: str, cfg: dict) -> bool:
    if name in set(cfg.get("exclude_branches", [])):
        return True
    return any(name.startswith(prefix) for prefix in cfg.get("exclude_prefixes", []))


def safe_descriptor(raw: dict) -> dict:
    # Only explicitly supported public fields are copied.
    return {
        "name": str(raw.get("name", "")).strip(),
        "version": str(raw.get("version", "")).strip(),
        "description": str(raw.get("description", "")).strip(),
        "visible": bool(raw.get("visible", True)),
        "publish": bool(raw.get("publish", True)),
        "sort_order": int(raw.get("sort_order", 0) or 0),
    }


def build_package(github: GitHub, branch: str, sha: str, cfg: dict) -> dict | None:
    commit = github.get_commit(sha)
    tree_sha = commit["tree"]["sha"]
    entries = walk_tree(github, tree_sha)

    descriptor_name = cfg.get("descriptor_file", "steamvault.json")
    descriptor = {}
    descriptor_entry = next(
        (x for x in entries if x.get("type") == "blob" and x.get("path") == descriptor_name),
        None,
    )
    if descriptor_entry:
        try:
            descriptor = safe_descriptor(json.loads(github.get_blob_text(descriptor_entry["sha"])))
        except Exception as exc:
            raise GitHubError(f"{branch}/{descriptor_name} is invalid: {exc}") from exc
    else:
        descriptor = safe_descriptor({})

    if not descriptor["publish"]:
        return None

    blobs = [x for x in entries if x.get("type") == "blob"]
    size_bytes = sum(int(x.get("size") or 0) for x in blobs)
    updated_at = (
        commit.get("committer", {}).get("date")
        or commit.get("author", {}).get("date")
        or iso_now()
    )

    package = {
        "id": branch,
        "name": descriptor["name"] or branch,
        "version": descriptor["version"] or "latest",
        "size_bytes": size_bytes,
        "file_count": len(blobs),
        "description": descriptor["description"],
        "visible": descriptor["visible"],
        "updated_at": updated_at,
        "commit_sha": sha,
        "sort_order": descriptor["sort_order"],
    }
    return package


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="metadata/index.json")
    parser.add_argument("--config", default="metadata/config.json")
    parser.add_argument("--owner", default=os.environ.get("STEAMVAULT_DB_OWNER", DEFAULT_OWNER))
    parser.add_argument("--repo", default=os.environ.get("STEAMVAULT_DB_REPO", DEFAULT_REPO))
    args = parser.parse_args()

    output = Path(args.output)
    config_path = Path(args.config)
    cfg = load_json(
        config_path,
        {
            "exclude_branches": ["main", "master", "gh-pages"],
            "exclude_prefixes": ["_", "dependabot/"],
            "descriptor_file": "steamvault.json",
        },
    )

    token = os.environ.get("STEAMVAULT_DB_READ_TOKEN", "")
    github = GitHub(token, args.owner, args.repo)

    previous = load_json(output, {"packages": []})
    previous_by_id = {
        str(item.get("id")): item
        for item in previous.get("packages", [])
        if item.get("id")
    }

    branches = [
        b for b in github.list_branches()
        if not branch_is_excluded(b["name"], cfg)
    ]
    current_names = {b["name"] for b in branches}

    print(f"Found {len(branches)} package branch(es).")
    packages = []
    changed = 0
    reused = 0
    unpublished = 0

    for i, branch_info in enumerate(branches, start=1):
        branch = branch_info["name"]
        sha = branch_info["sha"]
        old = previous_by_id.get(branch)

        if old and old.get("commit_sha") == sha:
            packages.append(old)
            reused += 1
            print(f"[{i}/{len(branches)}] reuse  {branch}")
            continue

        print(f"[{i}/{len(branches)}] scan   {branch}")
        pkg = build_package(github, branch, sha, cfg)
        changed += 1
        if pkg is None:
            unpublished += 1
            print(f"                 unpublished")
            continue
        packages.append(pkg)

    # Deleted branches automatically disappear because only current branch names are emitted.
    packages.sort(key=lambda x: (int(x.get("sort_order", 0)), str(x.get("name", "")).casefold(), str(x.get("id", "")).casefold()))

    result = {
        "schema_version": 2,
        "generated_at": iso_now(),
        "source": f"{args.owner}/{args.repo}",
        "package_count": len(packages),
        "packages": packages,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(output)

    removed = len(set(previous_by_id) - current_names)
    print(
        f"Done: {len(packages)} published, {changed} rescanned, "
        f"{reused} reused, {removed} deleted, {unpublished} unpublished."
    )


if __name__ == "__main__":
    main()
