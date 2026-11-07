"""Exercise the real command under a legacy Windows output encoding."""
import json
import os
import subprocess
import sys

from traceforge.config import PACKAGE_ROOT


def test_evaluate_emits_utf8_json_under_cp1252(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("TRACEFORGE_")}
    env.update(PYTHONIOENCODING="cp1252", PYTHONUTF8="0", TRACEFORGE_DATA_DIR=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-m", "traceforge", "evaluate"],
        cwd=PACKAGE_ROOT,
        env=env,
        capture_output=True,
        check=False,
        timeout=90,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    stdout = result.stdout.decode("utf-8")
    assert "数量计算修复与原仓库不变" in stdout
    report = json.loads(stdout)
    assert report["kind"] == "offline-harness"
    assert report["passed_cases"] == report["total_cases"] == 10
    assert report == json.loads((tmp_path / "evaluation.json").read_text(encoding="utf-8"))
