import copy
import json
import subprocess
from pathlib import Path

import pytest

from traceforge.config import Settings
from traceforge.retrieval import RepositoryIndex
from traceforge.retrieval_evaluation import (
    METHODS,
    _aggregate_metrics,
    _score_ranking,
    _write_report,
    run_retrieval_evaluation,
)


def make_case(catalog, case_id, query, files, relevant_paths):
    base = catalog / case_id
    public = base / "workspace"
    public.mkdir(parents=True)
    (base / "case.json").write_text(json.dumps({"id": case_id, "relevant_paths": relevant_paths}), encoding="utf-8")
    (public / "PROBLEM.md").write_text(query, encoding="utf-8")
    for name, text in files.items():
        path = public / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return base


def test_metrics_use_unique_path_rankings_and_full_relevance_denominators():
    # Deduplicated ranking is a, x, b, z; the third required path is absent.
    first = _score_ranking(["a", "a", "x", "b", "z"], ["a", "b", "b", "missing"])
    assert first["relevant_count"] == 3
    assert first["retrieved_paths"] == 4
    assert first["hits_at_k"] == {"1": 1, "3": 2, "5": 2, "8": 2}
    assert first["recall_at_k"] == pytest.approx({"1": 1 / 3, "3": 2 / 3, "5": 2 / 3, "8": 2 / 3})
    assert not any(first["all_required_at_k"].values())
    assert first["reciprocal_rank"] == 1
    second = _score_ranking(["irrelevant", "c"], ["c"])
    assert second["reciprocal_rank"] == 0.5
    summary = _aggregate_metrics([first, second])
    # This is a macro mean, not a pooled count (which would give 1/4 and 3/4).
    assert summary["macro_recall_at_k"] == pytest.approx({"1": 1 / 6, "3": 5 / 6, "5": 5 / 6, "8": 5 / 6})
    assert summary["mrr"] == 0.75
    assert summary["all_required_at_k"]["3"] == {"numerator": 1, "denominator": 2, "rate": 0.5}
    assert summary["query_count"] == 2


def test_metrics_keep_empty_results_as_misses_and_use_complete_prefixed_paths():
    missed = _score_ranking([], ["required.py"])
    assert not any(missed["recall_at_k"].values())
    assert not any(missed["all_required_at_k"].values())
    assert missed["reciprocal_rank"] == 0
    result = _score_ranking(["repo000/test.py", "repo001/test.py"], ["repo001/test.py"])
    assert result["reciprocal_rank"] == 0.5
    assert result["recall_at_k"]["1"] == 0
    assert _aggregate_metrics([result, missed])["all_required_at_k"]["3"]["denominator"] == 2
    with pytest.raises(ValueError):
        _score_ranking(["one.py"], [])
    with pytest.raises(ValueError):
        _aggregate_metrics([])


def test_pilot_reads_only_public_inputs_and_calls_production_rankers(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    beta = make_case(catalog, "beta", "externalquerymarker payment error", {
        "payment.py": "def payment(total):\n    return total\n",
        "test_public.py": "assert payment(4) == 4\n",
        ".env": "do_not_read_env",
        "oracle.json": "do_not_read_oracle",
        "nested/hidden/answer.py": "do_not_read_hidden",
        "test_hidden.py": "do_not_read_hidden_test",
        "nested/PROBLEM.md": "do_not_copy_problem",
        "case.json": "do_not_copy_labels",
    }, ["payment.py", "test_public.py"])
    make_case(catalog, "alpha", "externalquerymarker allocation error", {
        "allocation.py": "def allocation(size):\n    return size\n",
        "test_public.py": "assert allocation(2) == 2\n",
    }, ["allocation.py", "test_public.py"])
    (beta / "hidden").mkdir()
    (beta / "hidden" / "test_hidden.py").write_text("must_not_open", encoding="utf-8")
    (beta / "oracle.json").write_text("must_not_open", encoding="utf-8")
    monkeypatch.setattr("traceforge.retrieval_evaluation.CASES_ROOT", catalog)
    original_open = Path.open
    opened = []

    def guarded_open(path, *args, **kwargs):
        if path.is_relative_to(catalog):
            relative = path.relative_to(catalog).as_posix()
            assert not any("oracle" in part or "hidden" in part or part == ".env"
                           for part in path.relative_to(catalog).parts), relative
            opened.append(relative)
        return original_open(path, *args, **kwargs)

    def forbidden_execution(*args, **kwargs):
        raise AssertionError("Retrieval evaluation must not execute tests or launch processes")

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(subprocess, "run", forbidden_execution)
    monkeypatch.setattr(subprocess, "Popen", forbidden_execution)
    original_search = RepositoryIndex.search
    calls = []

    def recording_search(index, query, limit=8, *, method="rrf"):
        calls.append((query, method, limit))
        assert all("PROBLEM.md" not in path and "case.json" not in path for path in index._records)
        assert all("externalquerymarker" not in line for record in index._records.values() for line in record.lines)
        return original_search(index, query, limit, method=method)

    monkeypatch.setattr(RepositoryIndex, "search", recording_search)
    settings = Settings(data_dir=tmp_path / "reports", api_key="not_to_be_persisted")
    report = run_retrieval_evaluation(settings)
    assert report["kind"] == "synthetic-retrieval-pilot"
    assert report["provider_calls"] == 0
    assert report["corpus"]["files"] == 4
    assert [case["case_id"] for case in report["cases"]] == ["alpha", "beta"]
    assert [case["corpus_prefix"] for case in report["cases"]] == ["repo000", "repo001"]
    assert len(calls) == 6
    assert [method for _, method, _ in calls] == ["bm25", "char", "rrf"] * 2
    assert all(limit == 50 for _, _, limit in calls)
    assert "beta/workspace/PROBLEM.md" in opened
    assert not any("nested/PROBLEM.md" in item or item.endswith("workspace/case.json") for item in opened)
    for case in report["cases"]:
        assert set(case["methods"]) == set(METHODS)
        assert set(case["versus_bm25"]) == {"alphabetical", "char", "rrf"}
        for result in case["methods"].values():
            assert len(set(result["ranking"])) == len(result["ranking"])
    for summary in report["methods"].values():
        assert summary["query_count"] == 2
        assert summary["all_required_at_k"]["1"] == {"numerator": 0, "denominator": 2, "rate": 0.0}
    saved = json.loads((settings.data_dir / "retrieval-evaluation.json").read_text(encoding="utf-8"))
    assert saved == report
    assert "not_to_be_persisted" not in json.dumps(report)
    assert not list(settings.data_dir.glob("*.tmp"))


def stable_report(report):
    result = copy.deepcopy(report)
    result.pop("generated_at")
    result.pop("timings")
    for method in result["methods"].values():
        method.pop("timings_ms")
    for case in result["cases"]:
        for method in case["methods"].values():
            method.pop("query_ms")
    return result


def test_packaged_pilot_data_hashes_and_rankings_repeat(tmp_path):
    settings = Settings(data_dir=tmp_path / "reports")
    first = run_retrieval_evaluation(settings)
    second = run_retrieval_evaluation(settings)
    assert stable_report(first) == stable_report(second)
    assert len(first["cases"]) == 10
    assert first["corpus"]["files"] == 33
    assert all(len(case["relevant_paths"]) == 2 for case in first["cases"])
    assert len(first["corpus"]["sha256"]) == len(first["queries_sha256"]) == len(first["qrels_sha256"]) == 64
    assert first["configuration"]["candidate_limit_per_lexical_route"] == 200
    assert first["timings"]["cold_build_ms"] > 0
    assert all(summary["query_count"] == 10 for summary in first["methods"].values())
    assert all(summary["all_required_at_k"]["1"]["rate"] == 0 for summary in first["methods"].values())


@pytest.mark.parametrize("relevant", [["missing.py"], [".env"], ["../oracle.json"], [], ["hidden/answer.py"]])
def test_invalid_or_unindexable_labels_fail_instead_of_shrinking_denominator(tmp_path, monkeypatch, relevant):
    catalog = tmp_path / "catalog"
    make_case(catalog, "sample", "public query", {"main.py": "pass\n"}, relevant)
    monkeypatch.setattr("traceforge.retrieval_evaluation.CASES_ROOT", catalog)
    with pytest.raises((ValueError, OSError)):
        run_retrieval_evaluation(Settings(data_dir=tmp_path / "reports"))
    assert not (tmp_path / "reports" / "retrieval-evaluation.json").exists()


def test_file_budget_failure_preserves_previous_report(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    make_case(catalog, "sample", "public query", {"a.py": "pass", "b.py": "pass"}, ["a.py", "b.py"])
    monkeypatch.setattr("traceforge.retrieval_evaluation.CASES_ROOT", catalog)
    directory = tmp_path / "reports"
    directory.mkdir()
    saved = directory / "retrieval-evaluation.json"
    saved.write_text('{"previous": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="max_files"):
        run_retrieval_evaluation(Settings(data_dir=directory, max_files=1))
    assert json.loads(saved.read_text(encoding="utf-8")) == {"previous": True}


def test_atomic_report_replace_failure_keeps_previous_file_and_removes_temporary(tmp_path, monkeypatch):
    destination = tmp_path / "retrieval-evaluation.json"
    destination.write_text('{"previous": true}', encoding="utf-8")

    def failed_replace(source, target):
        assert source.parent == target.parent == tmp_path
        assert json.loads(source.read_text(encoding="utf-8")) == {"new": True}
        assert json.loads(destination.read_text(encoding="utf-8")) == {"previous": True}
        raise OSError("synthetic atomic replace failure")

    monkeypatch.setattr(Path, "replace", failed_replace)
    with pytest.raises(OSError, match="replace failure"):
        _write_report({"new": True}, tmp_path)
    assert json.loads(destination.read_text(encoding="utf-8")) == {"previous": True}
    assert list(tmp_path.iterdir()) == [destination]


def test_changed_public_file_updates_corpus_hash_without_changing_query_or_qrels(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    base = make_case(catalog, "sample", "allocation fails", {"main.py": "allocation = 1"}, ["main.py"])
    monkeypatch.setattr("traceforge.retrieval_evaluation.CASES_ROOT", catalog)
    settings = Settings(data_dir=tmp_path / "reports")
    first = run_retrieval_evaluation(settings)
    (base / "workspace" / "main.py").write_text("allocation = 2", encoding="utf-8")
    second = run_retrieval_evaluation(settings)
    assert first["corpus"]["sha256"] != second["corpus"]["sha256"]
    assert first["queries_sha256"] == second["queries_sha256"]
    assert first["qrels_sha256"] == second["qrels_sha256"]
