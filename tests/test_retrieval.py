import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from traceforge.retrieval import MAX_CONTENT_CHARS, RepositoryIndex, is_denied_path, redact, safe_path


def write(root: Path, path: str, content: str) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def test_mixed_retrieval_has_real_citations_and_parent_context(tmp_path):
    content = "\n".join(f"# line {index}" for index in range(1, 21))
    content += "\ndef charge_customer(total, rebate):\n    return total - rebate\n# nearby explanation\n"
    target = write(tmp_path, "src/payment.py", content)
    write(tmp_path, "other.py", "def render_ui():\n    return 'hello'\n")
    index = RepositoryIndex(tmp_path)
    stats = index.build()
    assert stats["files"] == 2
    assert "TF-IDF" in stats["method"]
    results = index.search("charge_customer rebate")
    assert results[0]["path"] == "src/payment.py"
    evidence = results[0]
    source_lines = content.splitlines()
    assert evidence["content"] == "\n".join(source_lines[evidence["line_start"] - 1:evidence["line_end"]])
    assert "nearby explanation" in evidence["content"]
    assert evidence["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert evidence["id"] == index.search("charge_customer rebate")[0]["id"]
    assert evidence["score"] > 0


def test_cjk_retrieval(tmp_path):
    write(tmp_path, "orders.py", "# 订单重复扣款错误，退款处理需要幂等检查\ndef refund():\n    pass\n")
    write(tmp_path, "display.py", "# 页面字体颜色与导航布局\ndef draw():\n    pass\n")
    assert RepositoryIndex(tmp_path).search("重复扣款退款")[0]["path"] == "orders.py"


def test_incremental_build_and_search_follow_update_and_delete(tmp_path):
    target = write(tmp_path, "app.py", "original_unique_symbol = 1\n")
    index = RepositoryIndex(tmp_path)
    assert index.build()["added"] == 1
    first = index.read("app.py")
    assert index.build()["unchanged"] == 1
    target.write_text("replacement_unique_symbol = 2\n", encoding="utf-8")
    assert index.build()["updated"] == 1
    second = index.search("replacement_unique_symbol")[0]
    assert second["sha256"] != first["sha256"]
    assert second["id"] != first["id"]
    assert "original_unique_symbol" not in second["content"]
    target.unlink()
    assert index.search("replacement_unique_symbol") == []
    assert index.files() == []


def test_refresh_detects_content_even_with_unchanged_timestamp(tmp_path):
    target = write(tmp_path, "app.py", "alpha_token = 10")
    before = target.stat()
    index = RepositoryIndex(tmp_path)
    index.build()
    target.write_text("bravo_token = 20", encoding="utf-8")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert index.build()["updated"] == 1
    assert "bravo_token" in index.search("bravo_token")[0]["content"]


@pytest.mark.parametrize("path", ["../outside.txt", "a/../../outside.txt", "C:/Windows/win.ini",
                                  "C:relative.txt", "/etc/passwd", "\\\\host\\share\\file",
                                  ".git/config", ".env", ".env.example", "secrets/config.json",
                                  "src/client-secret.json", "private.pem", "node_modules/a.js",
                                  "build/output.js", ".npmrc", "a:stream", "CON", "nul.txt",
                                  ".env. ", ".traceforge/artifacts/run/patch.diff", ".config/gh/hosts.yml"])
def test_direct_read_cannot_bypass_boundary(tmp_path, path):
    index = RepositoryIndex(tmp_path)
    with pytest.raises((ValueError, OSError)):
        index.read(path)
    assert is_denied_path(path)


def test_scan_filters_private_generated_binary_and_oversize(tmp_path):
    for path in (".env", ".git/config", "secrets/password.txt", "node_modules/pkg/main.js", "dist/bundle.js"):
        write(tmp_path, path, "forbiddenneedle = true")
    write(tmp_path, "ok.py", "allowedneedle = 1")
    write(tmp_path, "big.log", "large" * 200)
    (tmp_path / "unknown.dat").write_bytes(b"binary\x00payload")
    (tmp_path / "bad.txt").write_bytes(b"\xff\xfe\xfd")
    index = RepositoryIndex(tmp_path, max_file_bytes=100)
    assert index.files() == ["ok.py"]
    assert all("forbiddenneedle" not in item["content"] for item in index.search("forbiddenneedle"))
    with pytest.raises(ValueError):
        index.read("unknown.dat")
    with pytest.raises(ValueError):
        index.read("big.log")


def test_symlinks_and_directory_links_are_never_followed(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = write(tmp_path, "outside/private.txt", "outside-secret-needle")
    try:
        (root / "link.txt").symlink_to(outside)
        (root / "linked_dir").symlink_to(outside.parent, target_is_directory=True)
    except OSError:
        pytest.skip("This host does not grant symbolic link creation")
    index = RepositoryIndex(root)
    assert index.files() == []
    for path in ("link.txt", "linked_dir/private.txt"):
        with pytest.raises(ValueError):
            index.read(path)
    assert index.search("outside-secret-needle") == []


def test_existing_record_replaced_by_symlink_is_evicted(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    target = write(root, "safe.py", "old_symbol = 1")
    outside = write(tmp_path, "outside.py", "outside_symbol = 2")
    index = RepositoryIndex(root)
    index.build()
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("This host does not grant symbolic link creation")
    assert index.search("old_symbol") == []
    assert index.files() == []


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell.exe"), reason="Windows junction test")
def test_windows_junction_is_never_followed(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = write(tmp_path, "outside/private.txt", "junction-outside-private")
    link = root / "junction"
    quote = lambda value: str(value).replace("'", "''")
    command = f"New-Item -ItemType Junction -Path '{quote(link)}' -Target '{quote(outside.parent)}' | Out-Null"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            check=False, capture_output=True, text=True, timeout=15)
    if result.returncode:
        pytest.skip("Junction creation is unavailable on this Windows host")
    index = RepositoryIndex(root)
    assert index.files() == []
    with pytest.raises(ValueError, match="reparse"):
        index.read("junction/private.txt")
    assert index.search("junction-outside-private") == []


def test_large_log_and_query_remain_bounded_and_redacted(tmp_path):
    write(tmp_path, "service.log", "ERROR invalid allocation\napi_key=sk-proj-abcdefghijklmnop\n" + "X" * 20000)
    index = RepositoryIndex(tmp_path)
    result = index.search("ERROR allocation" + " " * 1000000)[0]
    assert len(result["content"]) <= MAX_CONTENT_CHARS
    assert "sk-proj-abcdefghijklmnop" not in result["content"]
    assert "[REDACTED]" in result["content"]
    direct = index.read("service.log", 3, 3)
    assert len(direct["content"]) <= MAX_CONTENT_CHARS
    assert direct["line_start"] == direct["line_end"] == 3


def test_read_line_ranges_and_limits(tmp_path):
    write(tmp_path, "app.py", "one\ntwo\nthree\nfour\n")
    index = RepositoryIndex(tmp_path)
    value = index.read("app.py", 2, 3)
    assert value["content"] == "two\nthree"
    assert (value["line_start"], value["line_end"]) == (2, 3)
    for start, end in ((0, 2), (3, 2), (10, 11)):
        with pytest.raises(ValueError):
            index.read("app.py", start, end)
    assert index.search(" ") == []
    assert index.search("one", limit=0) == []


def test_redaction_common_forms():
    value = redact('password="hunter2" api_key=abcdef Authorization: Bearer privateabc '
                   'postgres://alice:password@localhost/db ghp_abcdefghijklmnop '
                   '-----BEGIN RSA PRIVATE KEY-----\nkey material\n-----END RSA PRIVATE KEY-----')
    for secret in ("hunter2", "abcdef", "privateabc", "alice", "ghp_abcdefghijklmnop", "key material"):
        assert secret not in value
    assert value.count("[REDACTED") >= 5


@pytest.mark.parametrize("name", ["AWS_SECRET_ACCESS_KEY", "CUSTOM_API_KEY", "AZURE_CLIENT_SECRET",
                                  "SERVICE_ACCESS_TOKEN", "DATABASE_PASSWORD", "AWS_ACCESS_KEY_ID",
                                  "SERVICE_SECRET_KEY", "custom-api-key"])
@pytest.mark.parametrize("value", ['synthetic-private-value', '"synthetic-private-value"',
                                   "'synthetic-private-value'"])
def test_redaction_prefixed_credential_assignments(name, value):
    original = f"{name}={value}\nNORMAL_SETTING=retained"
    sanitized = redact(original)
    assert "synthetic-private-value" not in sanitized
    assert name in sanitized and "NORMAL_SETTING=retained" in sanitized
    assert sanitized.count("\n") == original.count("\n")
    assert redact(sanitized) == sanitized


def test_json_redaction_preserves_quoted_strings_and_structure():
    original = {"AWS_SECRET_ACCESS_KEY": 'synthetic"escaped\\value', "CUSTOM_API_KEY": "another-private-value",
                "nested": {"SERVICE_ACCESS_TOKEN": 123456, "token_count": 3},
                "password": None, "token": {"enabled": True}, "enabled": True}
    sanitized = json.loads(redact(json.dumps(original)))
    assert sanitized["AWS_SECRET_ACCESS_KEY"] == "[REDACTED]"
    assert sanitized["CUSTOM_API_KEY"] == "[REDACTED]"
    assert sanitized["nested"] == {"SERVICE_ACCESS_TOKEN": "[REDACTED]", "token_count": 3}
    assert sanitized["password"] is None
    assert sanitized["token"] == {"enabled": True}
    assert sanitized["enabled"] is True


def test_redaction_leaves_noncredential_identifier_names_unchanged():
    code = ("AWS_SECRET_ACCESS_KEY_LENGTH = 40\nMAX_API_KEY_LENGTH = 32\n"
            "token_count = 10\npassword_required = True\nsecretary = 'normal'\n"
            "def api_key_length(value):\n    return len(value)\n")
    assert redact(code) == code


def test_search_and_read_redact_prefixed_secrets(tmp_path):
    original = ('AWS_SECRET_ACCESS_KEY = "synthetic-aws-private-value"\n'
                'CUSTOM_API_KEY = "synthetic-provider-private-value"\n')
    source = write(tmp_path, "settings.py", original)
    index = RepositoryIndex(tmp_path)
    evidence = [index.read("settings.py"), *index.search("AWS_SECRET_ACCESS_KEY CUSTOM_API_KEY")]
    assert len(evidence) >= 2
    for item in evidence:
        assert "synthetic-aws-private-value" not in item["content"]
        assert "synthetic-provider-private-value" not in item["content"]
        assert item["content"].count("[REDACTED]") == 2
        assert (item["line_start"], item["line_end"]) == (1, 2)
    assert source.read_text(encoding="utf-8") == original


def test_max_files_is_enforced(tmp_path):
    for number in range(6):
        write(tmp_path, f"{number}.py", "entry = 1")
    assert len(RepositoryIndex(tmp_path, max_files=2).files()) == 2


def test_safe_path_accepts_plain_repository_file(tmp_path):
    target = write(tmp_path, "src/app.py", "pass")
    assert safe_path(tmp_path, "src/app.py") == target.resolve()
