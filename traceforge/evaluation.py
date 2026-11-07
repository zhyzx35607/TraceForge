"""Offline verification harness. These scores are NOT LLM repair accuracy."""
from __future__ import annotations

import json
import platform
import shutil
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .config import DEMO_ROOT, Settings
from .store import ConflictError, Store, patch_digest


def evaluate(settings: Settings | None = None) -> dict:
    from .retrieval import RepositoryIndex
    from .runner import run_verification
    settings = settings or Settings.from_env()
    cases = []

    def case(name, category, operation):
        start = time.monotonic()
        try:
            detail = operation()
            passed = True
        except Exception as exc:  # noqa: BLE001 - each case records failures without hiding the denominator
            detail = f"{type(exc).__name__}: {exc}"
            passed = False
        cases.append({"name": name, "category": category, "passed": passed,
                      "detail": detail, "duration_ms": round((time.monotonic() - start) * 1000)})

    with tempfile.TemporaryDirectory(prefix="traceforge-eval-") as raw:
        root = Path(raw)
        config = Settings(data_dir=root / "artifacts", allowed_roots=[root], test_timeout=15)
        original = 'subtotal = sum(Decimal(str(item["price"])) for item in items)'
        fixed = 'subtotal = sum(Decimal(str(item["price"])) * item["quantity"] for item in items)'

        def fixture(name):
            path = root / name
            shutil.copytree(DEMO_ROOT, path, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            return {"id": name, "name": name, "root": str(path), "runner": "local", "trusted": True,
                    "test_command": [sys.executable, "-m", "unittest", "discover", "-v"]}

        def patch(after=fixed, path="pricing.py", before=original):
            return {"path": path, "before": before, "after": after, "reason": "Offline oracle candidate for verifier evaluation",
                    "evidence_ids": ["oracle-evaluation-only"]}

        def verify_fix():
            project = fixture("quantity")
            prior = (Path(project["root"]) / "pricing.py").read_bytes()
            result = run_verification(project, [patch()], config, uuid.uuid4().hex)
            assert result["baseline"]["exit_code"] != 0, "baseline must fail"
            assert result["patched"]["exit_code"] == 0, "patched suite must pass"
            assert result["regression_fixed"] is True
            assert (Path(project["root"]) / "pricing.py").read_bytes() == prior, "original repo modified"
            return "原失败被消除，全部控制测试通过，原始源文件逐字节保持一致"
        case("数量计算修复与原仓库不变", "verification", verify_fix)

        def reject_overfit():
            project = fixture("overfit")
            result = run_verification(project, [patch(original + " * 3")], config, uuid.uuid4().hex)
            assert result["patched"]["exit_code"] != 0
            assert result["regression_fixed"] is False
            return "针对单条报错乘3的过拟合补丁被其他原有回归用例否决"
        case("过拟合补丁被回归测试拒绝", "verification", reject_overfit)

        def no_regression():
            project = fixture("already-good")
            path = Path(project["root"]) / "pricing.py"
            path.write_text(path.read_text().replace(original, fixed), encoding="utf-8")
            result = run_verification(project, [patch(fixed + "  # unchanged behavior", before=fixed)], config, uuid.uuid4().hex)
            assert result["baseline"]["exit_code"] == 0
            assert result["patched"]["exit_code"] == 0
            assert result["regression_fixed"] is False
            return "基线本来成功，前后均通过不计为修复成功"
        case("不可复现故障不冒充修复", "verification", no_regression)

        def forbidden_patch(path, before="irrelevant"):
            project = fixture("deny-" + uuid.uuid4().hex[:8])
            try:
                run_verification(project, [patch("changed", path=path, before=before)], config, uuid.uuid4().hex)
            except (ValueError, PermissionError, RuntimeError):
                return "执行层实际拒绝此补丁目标"
            raise AssertionError("forbidden patch was accepted")
        case("禁止改写测试来作弊", "policy", lambda: forbidden_patch("test_pricing.py", "import unittest"))
        case("阻断补丁路径穿越", "policy", lambda: forbidden_patch("../outside.py"))

        def trust_gate():
            project = {**fixture("untrusted"), "trusted": False}
            try:
                run_verification(project, [patch()], config, uuid.uuid4().hex)
            except (ValueError, PermissionError, RuntimeError):
                return "未标记可信的仓库不能在本机执行"
            raise AssertionError("untrusted local execution accepted")
        case("本机执行可信仓库门禁", "policy", trust_gate)

        def freshness():
            repo = root / "retrieval"
            repo.mkdir()
            file = repo / "service.py"
            file.write_text("def charge():\n    return 'card_declined'\n", encoding="utf-8")
            index = RepositoryIndex(repo)
            index.build()
            old = index.search("card_declined")[0]
            assert "card_declined" in old["content"]
            file.write_text("def charge():\n    return 'payment_accepted'\n", encoding="utf-8")
            index.build()
            latest = index.search("payment_accepted")[0]
            assert latest["sha256"] != old["sha256"]
            assert "card_declined" not in latest["content"]
            file.unlink()
            index.build()
            assert index.files() == []
            return "更新后证据哈希改变；删除后文件不再召回"
        case("检索更新删除和证据版本", "retrieval", freshness)

        def secrets():
            repo = root / "secrets"
            repo.mkdir()
            (repo / ".env").write_text("API_KEY=never-return-this", encoding="utf-8")
            index = RepositoryIndex(repo)
            index.build()
            assert ".env" not in index.files()
            for path in [".env", "../outside.py", str(root / "external.py")]:
                try:
                    index.read(path)
                except (ValueError, PermissionError, FileNotFoundError):
                    continue
                raise AssertionError(f"read accepted {path}")
            return "敏感文件、相对越界和绝对路径均不可读取"
        case("读取边界与敏感文件过滤", "policy", secrets)

        state = Store(root / "state.db")
        project = state.create_project(fixture("queue-fixture"))
        def new_run():
            return state.create_run({"project_id": project["id"], "title": "eval", "query": "failure", "mode": "investigate"})

        def recovery():
            run = new_run()
            first = state.claim_job("before-crash")
            assert first["run_id"] == run["id"]
            state.release_owner("before-crash")
            fresh = Store(state.db_path)
            recovered = fresh.claim_job("after-crash")
            assert recovered["run_id"] == run["id"] and recovered["attempts"] == 2
            fresh.finish_job(run["id"], "investigate", "after-crash")
            return "新 Store 实例从磁盘恢复同一任务与过期租约；产生 recovery 事件"
        case("过期租约与进程级状态恢复", "durability", recovery)

        def approval():
            run = new_run()
            state.update_run(run["id"], status="investigating")
            patches = [patch()]
            digest = patch_digest(patches)
            state.update_run(run["id"], status="awaiting_approval", state={"diagnosis": {"patches": patches}, "patch_digest": digest})
            reopened = Store(state.db_path)
            assert reopened.get_run(run["id"])["status"] == "awaiting_approval"
            try:
                reopened.decide(run["id"], "approve", "0" * 64)
            except ConflictError:
                pass
            else:
                raise AssertionError("digest mismatch accepted")
            reopened.decide(run["id"], "approve", digest)
            try:
                reopened.decide(run["id"], "approve", digest)
            except ConflictError:
                return "审批等待跨实例保留；篡改摘要和重复审批均拒绝"
            raise AssertionError("duplicate approval accepted")
        case("审批持久化与内容绑定", "durability", approval)

    passed = sum(c["passed"] for c in cases)
    report = {"available": True, "generated_at": datetime.now(UTC).isoformat(),
              "kind": "offline-harness", "version": "0.1.0", "total_cases": len(cases), "passed_cases": passed,
              "pass_rate": passed / len(cases), "cases": cases,
              "environment": {"python": platform.python_version(), "platform": platform.platform()},
              "limitations": ["候选补丁来自固定 oracle，分数仅衡量验证器与执行机制，不代表 LLM 修复成功率。",
                              "演示为单模块 Python 缺陷；未证明大型跨仓库故障、生产负载或容器逃逸防护。",
                              "本轮 runner 使用可信样例的本机进程；Docker 能力需可用 daemon 独立验证。"]}
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    report = evaluate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed_cases"] == report["total_cases"] else 1
