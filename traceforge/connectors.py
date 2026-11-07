"""Read-only, bounded local Git and explicitly selected GitHub evidence collectors."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import threading
from pathlib import Path

from .retrieval import MAX_CONTENT_CHARS, is_denied_path, make_evidence, redact, safe_path

MAX_TOOL_BYTES = 48000
TOOL_TIMEOUT_SECONDS = 8.0
MAX_GIT_METADATA_ENTRIES = 20000
_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}\Z")


def _redact_output(value: str) -> str:
    """Preserve JSON syntax while scrubbing tool output before it leaves the runner."""
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        return redact(value)

    def clean(item, depth: int = 0):
        if depth > 30:
            return "[nested value omitted]"
        if isinstance(item, str):
            return redact(item)
        if isinstance(item, list):
            return [clean(child, depth + 1) for child in item]
        if isinstance(item, dict):
            return {key: clean(child, depth + 1) for key, child in item.items()}
        return item

    return json.dumps(clean(parsed), ensure_ascii=False)


def _run(argv: list[str], *, cwd: Path | None = None, timeout: float = TOOL_TIMEOUT_SECONDS,
         output_limit: int = MAX_TOOL_BYTES) -> tuple[int, str, str]:
    """Drain pipes continuously while retaining at most output_limit bytes per pipe."""
    environment = os.environ.copy()
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
                        "GIT_PAGER": "cat", "PAGER": "cat", "GH_PROMPT_DISABLED": "1",
                        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    # Explicit cwd/-C determines the selected repository; inherited overrides must not redirect Git.
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_EXTERNAL_DIFF", "GIT_CONFIG_COUNT"):
        environment.pop(key, None)
    try:
        process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, shell=False, env=environment,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, ValueError) as exc:
        return 127, "", redact(f"{type(exc).__name__}: executable unavailable")
    buffers = [bytearray(), bytearray()]
    truncated = [False, False]

    def drain(pipe, position: int) -> None:
        try:
            while chunk := pipe.read(4096):
                remaining = output_limit - len(buffers[position])
                if remaining > 0:
                    buffers[position].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated[position] = True
        except (OSError, ValueError):
            pass
        finally:
            pipe.close()

    readers = [threading.Thread(target=drain, args=(process.stdout, 0), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, 1), daemon=True)]
    for reader in readers:
        reader.start()
    try:
        process.wait(timeout=timeout)
        returncode = process.returncode
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
        returncode = 124
    for reader in readers:
        reader.join(timeout=1)
    outputs = [_redact_output(bytes(buffer).decode("utf-8", errors="replace")) for buffer in buffers]
    for index, was_truncated in enumerate(truncated):
        if was_truncated or len(outputs[index]) > output_limit:
            outputs[index] = outputs[index][:max(0, output_limit - 40)] + "\n[output truncated]"
    if returncode == 124:
        outputs[1] = (outputs[1] + "\n[command timed out]")[:output_limit]
    return returncode, outputs[0], outputs[1]


def _git(root: Path, args: list[str], **kwargs) -> tuple[int, str, str]:
    return _run(["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=",
                 "-c", "diff.external=", "-C", str(root), *args], cwd=root, **kwargs)


def _standalone_git_metadata(root: Path) -> bool:
    """Accept only an ordinary, self-contained .git directory before invoking Git.

    Gitfiles (including linked worktrees/submodules), commondir and alternates are
    deliberately unsupported: Git's worktree root does not constrain their object
    or history sources. Even internal pointer layouts fail closed rather than
    following a chain which may eventually leave the selected repository.
    """
    try:
        root_info = root.lstat()
        metadata = root / ".git"
        info = metadata.lstat()
        if (stat.S_ISLNK(root_info.st_mode) or getattr(root_info, "st_file_attributes", 0) & 0x400
                or not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400):
            return False
        forbidden = {"commondir", "objects/info/alternates", "objects/info/http-alternates"}
        pending = [metadata]
        inspected = 0
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    inspected += 1
                    if inspected > MAX_GIT_METADATA_ENTRIES:
                        return False
                    path = Path(entry.path)
                    if path.relative_to(metadata).as_posix().casefold() in forbidden:
                        return False
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        return False
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)
                    elif not stat.S_ISREG(info.st_mode):
                        return False
        return True
    except (OSError, ValueError):
        return False


def _safe_git_name(root: Path, path: str) -> bool:
    if is_denied_path(path):
        return False
    try:
        safe_path(root, path)
        return True
    except FileNotFoundError:
        # Deleted files can appear in status, but never inspect their historical contents here.
        return False
    except (OSError, ValueError):
        return False


def collect_git_evidence(root: Path, query: str = "", limit: int = 8) -> list[dict]:
    """Collect local history and safe current changes without fetching or modifying Git."""
    candidate = Path(root).absolute()
    if not _standalone_git_metadata(candidate):
        return []
    root = candidate.resolve()
    limit = max(0, min(int(limit), 20))
    if not limit or not root.is_dir():
        return []
    code, output, _ = _git(root, ["rev-parse", "--show-toplevel"])
    if code or Path(output.strip()).resolve() != root:
        return []
    evidence = []
    code, output, _ = _git(root, ["log", "-n", str(min(limit, 8)), "--format=%h%x09%aI%x09%s",
                                "--no-decorate", "--no-show-signature"], output_limit=16000)
    if not code and output.strip():
        evidence.append(make_evidence("git", "git:log", output.strip(), kind="history"))
    code, output, _ = _git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=no"], output_limit=16000)
    paths: list[str] = []
    status_lines: list[str] = []
    if not code:
        entries = iter(output.split("\0"))
        for entry in entries:
            if len(entry) < 4:
                continue
            state, path = entry[:2], entry[3:]
            previous = next(entries, "") if "R" in state or "C" in state else None
            if _safe_git_name(root, path) and (previous is None or _safe_git_name(root, previous)):
                status_lines.append(f"{state} {path}")
                paths.append(path)
        if status_lines:
            evidence.append(make_evidence("git", "git:status", "\n".join(status_lines), kind="status"))
    # Limit work; query only affects the order, never command syntax or shell interpretation.
    query_words = set(re.findall(r"[\w]+", str(query).casefold()[:4000]))
    paths = sorted(set(paths), key=lambda path: (-sum(word in path.casefold() for word in query_words), path))
    for path in paths[:max(0, limit - len(evidence))]:
        code, output, _ = _git(root, ["diff", "HEAD", "--no-ext-diff", "--no-textconv", "--no-color",
                                    "--unified=3", "--", path], output_limit=MAX_CONTENT_CHARS)
        if not code and output.strip():
            evidence.append(make_evidence("git", path, output, kind="diff"))
    return evidence[:limit]


def _json_rows(output: str) -> list[dict]:
    try:
        result = json.loads(output)
    except (json.JSONDecodeError, TypeError, RecursionError):
        return []
    if not isinstance(result, list) or len(result) > 100:
        return []
    return [item for item in result if isinstance(item, dict)]


def _github_url(value: object, repo: str, kind: str) -> str | None:
    if not isinstance(value, str):
        return None
    pattern = rf"https://github\.com/{re.escape(repo)}/{kind}/[0-9]+(?:[/?#].*)?\Z"
    return value if re.fullmatch(pattern, value, re.IGNORECASE) else None


def collect_github_evidence(repo: str, limit: int = 5) -> list[dict]:
    """Read issues and failed workflow metadata for one explicitly opted-in repository."""
    if not isinstance(repo, str) or not _REPOSITORY.fullmatch(repo) or repo.split("/")[1] in {".", ".."}:
        raise ValueError("GitHub repository must have the form owner/repo")
    limit = max(0, min(int(limit), 20))
    if not limit:
        return []
    evidence = []
    code, output, _ = _run(["gh", "issue", "list", "--repo", repo, "--state", "all", "--limit", str(limit),
                            "--json", "number,title,state,url,body,updatedAt"])
    if not code:
        for item in _json_rows(output):
            url = _github_url(item.get("url"), repo, "issues")
            if not url or not isinstance(item.get("number"), int) or not isinstance(item.get("title"), str):
                continue
            if not all(isinstance(item.get(field, ""), str) for field in ("state", "body", "updatedAt")):
                continue
            text = f"Issue #{item['number']}: {item['title']}\nState: {item.get('state', '')}\n"
            text += f"Updated: {item.get('updatedAt', '')}\n{item.get('body', '')}"
            evidence.append(make_evidence("github", url, text, kind="issue"))
    code, output, _ = _run(["gh", "run", "list", "--repo", repo, "--status", "failure", "--limit", str(limit),
                            "--json", "databaseId,displayTitle,workflowName,conclusion,url,createdAt,headSha"])
    if not code:
        for item in _json_rows(output):
            url = _github_url(item.get("url"), repo, "actions/runs")
            if not url or not isinstance(item.get("databaseId"), int) or item.get("conclusion") != "failure":
                continue
            fields = ("displayTitle", "workflowName", "createdAt", "headSha")
            if not all(isinstance(item.get(field, ""), str) for field in fields):
                continue
            text = f"Failed workflow: {item.get('workflowName', '')}\nTitle: {item.get('displayTitle', '')}\n"
            text += f"Created: {item.get('createdAt', '')}\nCommit: {item.get('headSha', '')}\nConclusion: failure"
            evidence.append(make_evidence("github", url, text, kind="workflow"))
    # Reserve space for each source when both are available.
    issues = [item for item in evidence if item["kind"] == "issue"]
    workflows = [item for item in evidence if item["kind"] == "workflow"]
    combined = []
    while issues or workflows:
        if issues:
            combined.append(issues.pop(0))
        if workflows:
            combined.append(workflows.pop(0))
    return combined[:limit]
