import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from traceforge import connectors
from traceforge.connectors import collect_git_evidence, collect_github_evidence
from traceforge.retrieval import MAX_CONTENT_CHARS


def git(root: Path, *arguments: str):
    return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, check=True)


def test_no_git_repository_degrades_to_empty(tmp_path):
    assert collect_git_evidence(tmp_path) == []


def test_missing_git_executable_degrades(monkeypatch, tmp_path):
    monkeypatch.setattr(connectors, "_run", lambda *args, **kwargs: (127, "", "not available"))
    assert collect_git_evidence(tmp_path) == []


@pytest.mark.skipif(not shutil.which("git"), reason="Git is unavailable")
def test_real_git_history_status_diff_and_private_path_filter(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "TraceForge Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "app.py").write_text("result = total + discount\n", encoding="utf-8")
    (tmp_path / ".env").write_text("DO_NOT_READ=private-initial\n", encoding="utf-8")
    git(tmp_path, "add", "app.py", ".env")
    git(tmp_path, "commit", "-m", "Introduce discount calculation")
    (tmp_path / "app.py").write_text("result = total - discount\napi_key=abcdefghi\n", encoding="utf-8")
    (tmp_path / ".env").write_text("DO_NOT_READ=private-updated\n", encoding="utf-8")
    evidence = collect_git_evidence(tmp_path, "discount")
    assert {item["kind"] for item in evidence} == {"history", "status", "diff"}
    content = "\n".join(item["content"] for item in evidence)
    assert "Introduce discount" in content
    assert "result = total - discount" in content
    assert "private-initial" not in content and "private-updated" not in content
    assert "abcdefghi" not in content
    assert ".env" not in content
    assert all(len(item["content"]) <= MAX_CONTENT_CHARS for item in evidence)
    assert (tmp_path / "app.py").read_text(encoding="utf-8").endswith("api_key=abcdefghi\n")


@pytest.mark.skipif(not shutil.which("git"), reason="Git is unavailable")
def test_nested_project_does_not_read_parent_git_history(tmp_path):
    git(tmp_path, "init")
    nested = tmp_path / "nested"
    nested.mkdir()
    assert collect_git_evidence(nested) == []


@pytest.mark.skipif(not shutil.which("git"), reason="Git is unavailable")
@pytest.mark.parametrize("layout", ["gitfile", "commondir", "alternates"])
def test_external_git_metadata_rejected_before_git_execution(tmp_path, monkeypatch, layout):
    outside = tmp_path / "outside"
    outside.mkdir()
    git(outside, "init")
    git(outside, "config", "user.name", "Synthetic Audit")
    git(outside, "config", "user.email", "audit@example.invalid")
    (outside / "app.py").write_text("value = 1\n", encoding="utf-8")
    git(outside, "add", "app.py")
    git(outside, "commit", "-m", "OUTSIDE_HISTORY_CANARY_983f")
    commit = git(outside, "rev-parse", "HEAD").stdout.strip()
    registered = tmp_path / "registered"
    registered.mkdir()
    if layout == "gitfile":
        (registered / ".git").write_text(f"gitdir: {(outside / '.git').as_posix()}\n", encoding="utf-8")
    else:
        git(registered, "init")
        (registered / ".git" / "HEAD").write_text(commit + "\n", encoding="utf-8")
        if layout == "commondir":
            (registered / ".git" / "commondir").write_text((outside / ".git").as_posix() + "\n",
                                                           encoding="utf-8")
        else:
            objects = registered / ".git" / "objects"
            pointer = os.path.relpath(outside / ".git" / "objects", objects).replace("\\", "/")
            (objects / "info" / "alternates").write_text(pointer + "\n", encoding="utf-8", newline="\n")
    (registered / "app.py").write_text("value = 1\n", encoding="utf-8")
    # The old --show-toplevel check accepts this root despite reading outside history.
    assert Path(git(registered, "rev-parse", "--show-toplevel").stdout.strip()).resolve() == registered.resolve()
    assert "OUTSIDE_HISTORY_CANARY_983f" in git(registered, "log", "-1", "--format=%s").stdout
    calls = []
    monkeypatch.setattr(connectors, "_git", lambda *args, **kwargs: calls.append(args))
    assert collect_git_evidence(registered) == []
    assert calls == []


@pytest.mark.parametrize("pointer", ["commondir", "objects/info/alternates", "objects/info/http-alternates"])
def test_metadata_pointer_layouts_fail_closed_even_when_empty(tmp_path, monkeypatch, pointer):
    metadata = tmp_path / ".git"
    target = metadata / pointer
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    monkeypatch.setattr(connectors, "_git", lambda *args, **kwargs: pytest.fail("Git must not follow metadata pointers"))
    assert collect_git_evidence(tmp_path) == []


def test_git_metadata_scan_is_bounded(tmp_path, monkeypatch):
    metadata = tmp_path / ".git"
    metadata.mkdir()
    for name in ("HEAD", "config", "index"):
        (metadata / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(connectors, "MAX_GIT_METADATA_ENTRIES", 2)
    monkeypatch.setattr(connectors, "_git", lambda *args, **kwargs: pytest.fail("Git metadata scan must fail closed"))
    assert collect_git_evidence(tmp_path) == []


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell.exe"), reason="Windows junction test")
def test_git_metadata_objects_junction_is_rejected(tmp_path, monkeypatch):
    metadata = tmp_path / "registered" / ".git"
    metadata.mkdir(parents=True)
    outside = tmp_path / "outside-objects"
    outside.mkdir()
    link = metadata / "objects"
    quote = lambda value: str(value).replace("'", "''")
    command = f"New-Item -ItemType Junction -Path '{quote(link)}' -Target '{quote(outside)}' | Out-Null"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=15, check=False)
    if result.returncode:
        pytest.skip("Junction creation is unavailable on this host")
    monkeypatch.setattr(connectors, "_git", lambda *args, **kwargs: pytest.fail("Git must not follow a metadata junction"))
    assert collect_git_evidence(metadata.parent) == []


def test_github_validates_json_and_reserves_workflow_evidence(monkeypatch):
    calls = []
    issues = [{"number": 7, "title": "Checkout failure", "state": "OPEN",
               "url": "https://github.com/example/project/issues/7", "body": "api_key=privateabcdef",
               "updatedAt": "2026-11-06T00:00:00Z"},
              {"number": 8, "title": "Other issue", "state": "CLOSED",
               "url": "https://github.com/example/project/issues/8", "body": "detail"}]
    runs = [{"databaseId": 12, "displayTitle": "Broken tests", "workflowName": "CI", "conclusion": "failure",
             "url": "https://github.com/example/project/actions/runs/12", "createdAt": "2026-11-06T00:00:00Z",
             "headSha": "abc123"}]

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return 0, json.dumps(issues if argv[1] == "issue" else runs), ""

    monkeypatch.setattr(connectors, "_run", fake_run)
    evidence = collect_github_evidence("example/project", limit=2)
    assert [item["kind"] for item in evidence] == ["issue", "workflow"]
    assert "privateabcdef" not in evidence[0]["content"]
    assert len(calls) == 2
    assert all("--repo" in argv and "example/project" in argv for argv in calls)
    assert calls[1][calls[1].index("--status") + 1] == "failure"


@pytest.mark.parametrize("repo", ["", "--help", "owner/repo;whoami", "../owner/repo", "https://github.com/a/b", "a/.."])
def test_invalid_github_repository_rejected(repo):
    with pytest.raises(ValueError):
        collect_github_evidence(repo)


@pytest.mark.parametrize("output", ["not json", "{}", "[null, 3, true]", '[{"url":"https://evil.invalid/1"}]'])
def test_invalid_github_json_is_ignored(monkeypatch, output):
    monkeypatch.setattr(connectors, "_run", lambda *args, **kwargs: (0, output, ""))
    assert collect_github_evidence("example/project") == []


def test_gh_unavailable_or_unauthenticated_returns_empty(monkeypatch):
    monkeypatch.setattr(connectors, "_run", lambda *args, **kwargs: (1, "", "authentication required"))
    assert collect_github_evidence("example/project") == []


def test_subprocess_large_output_is_bounded():
    code, stdout, stderr = connectors._run(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 500000); sys.stderr.write('y' * 500000)"],
        output_limit=2000,
    )
    assert code == 0
    assert len(stdout) <= 2000 and len(stderr) <= 2000
    assert "truncated" in stdout and "truncated" in stderr


def test_subprocess_timeout_is_actionable_and_bounded():
    code, _stdout, stderr = connectors._run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)
    assert code == 124
    assert "timed out" in stderr


def test_tool_json_is_parsed_before_redaction_and_stays_valid():
    data = [{"body": "api_key=privateabcdef", "title": "A valid issue"}]
    command = "import json; print(" + repr(json.dumps(data)) + ")"
    code, output, _ = connectors._run([sys.executable, "-c", command])
    assert code == 0
    rows = connectors._json_rows(output)
    assert rows[0]["body"] == "api_key=[REDACTED]"
    assert rows[0]["title"] == "A valid issue"
    assert "privateabcdef" not in output


def test_subprocess_never_uses_a_shell(monkeypatch):
    seen = {}
    original = subprocess.Popen

    def observe(*args, **kwargs):
        seen.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(connectors.subprocess, "Popen", observe)
    assert connectors._run([sys.executable, "-c", "print('safe')"])[0] == 0
    assert seen["shell"] is False
    assert seen["stdin"] == subprocess.DEVNULL
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
