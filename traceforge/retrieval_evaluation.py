"""Reproducible lexical-retrieval ablation on a small synthetic public corpus.

Queries and relevance labels stay outside the indexed corpus. This evaluates the
production retrieval entry point, not LLM diagnosis, repair, or production quality.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from statistics import median

from .config import Settings
from .paths import CASES_ROOT
from .retrieval import (
    BM25_B,
    BM25_K1,
    CANDIDATE_LIMIT,
    CHAR_NGRAM_SIZES,
    LONG_LINE_OVERLAP,
    MAX_CHUNK_CHARS,
    MAX_CHUNK_LINES,
    MAX_CONTENT_CHARS,
    MAX_QUERY_CHARS,
    MAX_SEARCH_RESULTS,
    PARENT_CONTEXT_LINES,
    RRF_K,
    RepositoryIndex,
    _text,
    is_denied_path,
    safe_path,
)

METHODS = ("alphabetical", "bm25", "char", "rrf")
CUTOFFS = (1, 3, 5, 8)
MAX_CASES = 20
_PRIVATE_MATERIAL = re.compile(r"(^|[._-])(hidden|oracle)([._-]|$)", re.IGNORECASE)


def _sha_json(value: object) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _excluded(path: str) -> bool:
    return is_denied_path(path) or any(
        part.casefold() in {"problem.md", "case.json"} or _PRIVATE_MATERIAL.search(part)
        for part in PurePosixPath(path.replace("\\", "/")).parts
    )


def _read_bytes(root: Path, relative: str, maximum: int) -> bytes | None:
    """Bound reads and reuse the production path/link boundary before copying."""
    target = safe_path(root, relative)
    before = target.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
        return None
    with target.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        verified = safe_path(root, relative).stat()
        identity = (opened.st_dev, opened.st_ino)
        if identity != (before.st_dev, before.st_ino) or identity != (verified.st_dev, verified.st_ino):
            raise ValueError("Public corpus file changed while opening")
        data = handle.read(maximum + 1)
    return data if len(data) <= maximum and _text(data) is not None else None


def _copy_public_workspace(source: Path, destination: Path, maximum: int) -> tuple[list[dict], int]:
    """Visit only this public workspace; prune private material before any read."""
    manifest, skipped = [], 0
    for directory, names, filenames in os.walk(source, followlinks=False):
        parent = Path(directory)
        kept = []
        for name in sorted(names):
            relative = (parent / name).relative_to(source).as_posix()
            if _excluded(relative):
                skipped += 1
                continue
            try:
                safe_path(source, relative)
            except (OSError, ValueError):
                skipped += 1
                continue
            kept.append(name)
        names[:] = kept
        for name in sorted(filenames):
            relative = (parent / name).relative_to(source).as_posix()
            if _excluded(relative):
                skipped += 1
                continue
            try:
                data = _read_bytes(source, relative, maximum)
            except (OSError, ValueError):
                skipped += 1
                continue
            if data is None:
                skipped += 1
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            manifest.append({"path": f"{destination.name}/{relative}", "size_bytes": len(data),
                             "sha256": hashlib.sha256(data).hexdigest()})
    return manifest, skipped


def _prepare_corpus(root: Path, settings: Settings) -> tuple[list[dict], list[dict], int]:
    manifests = sorted(CASES_ROOT.glob("*/case.json"))
    if not manifests or len(manifests) > MAX_CASES:
        raise ValueError("Synthetic retrieval catalog is empty or exceeds the case budget")
    cases, files, skipped = [], [], 0
    maximum = max(1, min(int(settings.max_file_bytes), 2_000_000))
    for number, manifest in enumerate(manifests):
        case_id = manifest.parent.name
        if not re.fullmatch(r"[a-z0-9_]{1,60}", case_id):
            raise ValueError("Invalid synthetic retrieval case identifier")
        data = _read_bytes(CASES_ROOT, f"{case_id}/case.json", 65536)
        if data is None:
            raise ValueError(f"Unreadable public case metadata: {case_id}")
        case = json.loads(data.decode("utf-8-sig"))
        if (not isinstance(case, dict) or case.get("id") != case_id
                or not isinstance(case.get("relevant_paths"), list) or not case["relevant_paths"]
                or any(not isinstance(path, str) or _excluded(path) for path in case["relevant_paths"])):
            raise ValueError(f"Invalid public relevance labels: {case_id}")
        # safe_path also rejects a linked workspace before os.walk enters it.
        source = safe_path(CASES_ROOT, f"{case_id}/workspace")
        query_data = _read_bytes(source, "PROBLEM.md", maximum)
        if query_data is None:
            raise ValueError(f"Unreadable public query: {case_id}")
        query_text = _text(query_data)
        assert query_text is not None
        query = query_text[:MAX_QUERY_CHARS]
        if not query.strip():
            raise ValueError(f"Empty public query: {case_id}")
        prefix = f"repo{number:03d}"
        copied, omitted = _copy_public_workspace(source, root / prefix, maximum)
        paths = {item["path"] for item in copied}
        qrels = sorted({f"{prefix}/{safe_path(source, path).relative_to(source).as_posix()}"
                        for path in case["relevant_paths"]})
        if not set(qrels) <= paths:
            raise ValueError(f"Relevant paths were excluded from the public corpus: {case_id}")
        cases.append({"case_id": case_id, "corpus_prefix": prefix, "query": query,
                      "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                      "query_source_sha256": hashlib.sha256(query_data).hexdigest(),
                      "query_truncated": len(query_text) > MAX_QUERY_CHARS,
                      "relevant_paths": qrels, "qrels_sha256": _sha_json(qrels)})
        files.extend(copied)
        skipped += omitted
    return cases, sorted(files, key=lambda item: item["path"]), skipped


def _score_ranking(ranking: list[str], relevant_paths: list[str]) -> dict:
    """Path-level metrics; duplicate labels/results never change a denominator."""
    paths = list(dict.fromkeys(ranking))
    relevant = set(relevant_paths)
    if not relevant:
        raise ValueError("At least one relevant path is required for a scored query")
    hits = {str(k): len(set(paths[:k]) & relevant) for k in CUTOFFS}
    return {"relevant_count": len(relevant), "retrieved_paths": len(paths), "hits_at_k": hits,
            "recall_at_k": {str(k): hits[str(k)] / len(relevant) for k in CUTOFFS},
            "all_required_at_k": {str(k): hits[str(k)] == len(relevant) for k in CUTOFFS},
            "reciprocal_rank": next((1 / rank for rank, path in enumerate(paths, 1) if path in relevant), 0.0)}


def _aggregate_metrics(metrics: list[dict]) -> dict:
    if not metrics:
        raise ValueError("At least one scored query is required")
    count = len(metrics)
    totals = {str(k): sum(item["all_required_at_k"][str(k)] for item in metrics) for k in CUTOFFS}
    return {"query_count": count,
            "macro_recall_at_k": {str(k): sum(item["recall_at_k"][str(k)] for item in metrics) / count
                                  for k in CUTOFFS},
            "mrr": sum(item["reciprocal_rank"] for item in metrics) / count,
            "all_required_at_k": {
                key: {"numerator": total, "denominator": count, "rate": total / count}
                for key, total in totals.items()}}


def _differences(methods: dict, relevant: list[str]) -> dict:
    baseline = methods["bm25"]
    baseline_ranks = {path: rank for rank, path in enumerate(baseline["ranking"], 1)}
    differences = {}
    for method, result in methods.items():
        if method == "bm25":
            continue
        ranks = {path: rank for rank, path in enumerate(result["ranking"], 1)}
        differences[method] = {
            "recall_at_k_delta": {str(k): result["metrics"]["recall_at_k"][str(k)]
                                  - baseline["metrics"]["recall_at_k"][str(k)] for k in CUTOFFS},
            "reciprocal_rank_delta": result["metrics"]["reciprocal_rank"]
                                     - baseline["metrics"]["reciprocal_rank"],
            "relevant_rank_changes": [{"path": path, "bm25_rank": baseline_ranks.get(path),
                                       "method_rank": ranks.get(path)} for path in relevant],
        }
    return differences


def _write_report(report: dict, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".retrieval-evaluation-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(directory / "retrieval-evaluation.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_retrieval_evaluation(settings: Settings) -> dict:
    """Evaluate the public synthetic pilot; never instantiate a provider or execute code."""
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="traceforge-retrieval-pilot-") as temporary:
        root = Path(temporary)
        cases, file_manifest, skipped = _prepare_corpus(root, settings)
        prepared_ms = (time.perf_counter() - started) * 1000
        index = RepositoryIndex(root, max_files=settings.max_files, max_file_bytes=settings.max_file_bytes)
        if len(file_manifest) > index.max_files:
            raise ValueError("Configured max_files cannot index the complete public retrieval corpus")
        cold_started = time.perf_counter()
        stats = index.build()
        cold_ms = (time.perf_counter() - cold_started) * 1000
        if stats["files"] != len(file_manifest):
            raise ValueError("Prepared public corpus differs from the production index")
        alphabetical = [item["path"] for item in file_manifest][:MAX_SEARCH_RESULTS]
        for case in cases:
            methods = {}
            for method in METHODS:
                query_started = time.perf_counter()
                if method == "alphabetical":
                    ranking, returned = list(alphabetical), len(alphabetical)
                else:
                    evidence = index.search(case["query"], limit=MAX_SEARCH_RESULTS, method=method)
                    ranking = list(dict.fromkeys(item["path"] for item in evidence))
                    returned = len(evidence)
                elapsed_ms = (time.perf_counter() - query_started) * 1000
                methods[method] = {"ranking": ranking, "returned_items": returned,
                                   "query_ms": round(elapsed_ms, 4),
                                   "metrics": _score_ranking(ranking, case["relevant_paths"])}
            case["methods"] = methods
            case["versus_bm25"] = _differences(methods, case["relevant_paths"])
        methods = {}
        for method in METHODS:
            elapsed = [case["methods"][method]["query_ms"] for case in cases]
            methods[method] = {**_aggregate_metrics([case["methods"][method]["metrics"] for case in cases]),
                               "timings_ms": {"total": round(sum(elapsed), 4),
                                              "p50": median(elapsed),
                                              "p95": sorted(elapsed)[math.ceil(0.95 * len(elapsed)) - 1]}}
        report = {
            "schema_version": 1, "kind": "synthetic-retrieval-pilot", "status": "completed",
            "generated_at": datetime.now(UTC).isoformat(),
            "label": "SYNTHETIC RETRIEVAL PILOT — lexical ablation, no model or production accuracy claim",
            "corpus": {"sha256": _sha_json(file_manifest), "files": stats["files"], "chunks": stats["chunks"],
                       "bytes": sum(item["size_bytes"] for item in file_manifest), "file_manifest": file_manifest,
                       "excluded_entries": skipped},
            "queries_sha256": _sha_json([{"case_id": case["case_id"], "query": case["query"]} for case in cases]),
            "qrels_sha256": _sha_json([{"case_id": case["case_id"], "relevant_paths": case["relevant_paths"]}
                                       for case in cases]),
            "configuration": {
                "cutoffs": list(CUTOFFS), "method_order": list(METHODS), "max_cases": MAX_CASES,
                "max_files": index.max_files, "max_file_bytes": index.max_file_bytes,
                "candidate_limit_per_lexical_route": CANDIDATE_LIMIT,
                "returned_evidence_limit": MAX_SEARCH_RESULTS, "query_character_limit": MAX_QUERY_CHARS,
                "chunk_lines": MAX_CHUNK_LINES, "chunk_characters": MAX_CHUNK_CHARS,
                "long_line_overlap": LONG_LINE_OVERLAP, "parent_context_lines": PARENT_CONTEXT_LINES,
                "evidence_characters": MAX_CONTENT_CHARS, "bm25": {"k1": BM25_K1, "b": BM25_B},
                "char": {"ngram_sizes": list(CHAR_NGRAM_SIZES), "tf": "1 + log(tf)",
                         "idf": "log((1 + document_count) / (1 + document_frequency)) + 1",
                         "similarity": "cosine"}, "rrf": {"k": RRF_K},
                "tie_break": "stable document order from sorted repository traversal",
                "path_aggregation": "first occurrence of the complete neutral-prefixed path before top-k",
                "excluded": ["PROBLEM.md", "case.json", "hidden/oracle path components",
                             "production denylist, links, binary, non-UTF-8 and oversized files"],
            },
            "definitions": {
                "macro_recall_at_k": "mean across all cases of unique relevant paths in top-k / all unique qrels",
                "mrr": "mean reciprocal rank of first relevant path in returned path ranking; missing is zero",
                "all_required_at_k": "cases whose complete nonempty qrel set appears in top-k / all cases",
                "alphabetical": "query-independent sorted full corpus paths, capped at the returned-item limit",
                "timings": "one cold build; one query per method per case; lexical queries include production disk refresh",
                "p95": "nearest-rank percentile of these single-pass query durations",
                "hashes": "SHA256 of sorted compact UTF-8 JSON; corpus hashes path/size/file-byte-SHA manifest",
            },
            "limitations": [
                "Small synthetic pilot with shared fixture conventions; results do not establish production quality.",
                "No LLM calls, diagnosis, repair, hidden assertions, oracle patches, or code execution are evaluated.",
                "Queries are public PROBLEM.md text kept outside the corpus; labels are public case.json paths.",
                "All selected cases, including abstention cases, remain in every method's metric denominator.",
                "Lexical methods share production chunks, candidate caps, expansion and evidence deduplication.",
                "MRR is bounded by the returned evidence limit; path deduplication may yield fewer ranked paths.",
                "Timing is a single local pass after copying; filesystem caching and fixed method order affect it.",
                "No held-out tuning split, confidence intervals, semantic embeddings, or real incident dataset is included.",
            ],
            "timings": {"corpus_prepare_ms": round(prepared_ms, 4), "cold_build_ms": round(cold_ms, 4),
                        "total_ms": round((time.perf_counter() - started) * 1000, 4)},
            "provider_calls": 0, "methods": methods, "cases": cases,
        }
    _write_report(report, settings.data_dir)
    return report
