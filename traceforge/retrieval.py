"""Bounded repository retrieval: BM25 and character n-gram TF-IDF with RRF.

This is lexical retrieval, not neural embeddings. Source trees are always read-only.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

MAX_CONTENT_CHARS = 8000
MAX_CHUNK_CHARS = 6000
MAX_CHUNK_LINES = 24
LONG_LINE_OVERLAP = 256
PARENT_CONTEXT_LINES = 12
MAX_QUERY_CHARS = 12000
MAX_SEARCH_RESULTS = 50
CANDIDATE_LIMIT = 200
RRF_K = 60
BM25_K1 = 1.5
BM25_B = 0.75
CHAR_NGRAM_SIZES = (2, 3)
RETRIEVAL_METHODS = ("bm25", "char", "rrf")
DENIED_PARTS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    "build", "dist", "target", "vendor", "coverage", ".coverage", ".next", ".nuxt",
    ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".traceforge",
    "secret", "secrets", "credential", "credentials", ".ssh", ".aws", ".azure", ".gnupg",
    ".config", ".docker", ".kube",
})
BINARY_SUFFIXES = frozenset({
    ".exe", ".dll", ".so", ".dylib", ".pyc", ".pyo", ".class", ".jar", ".zip", ".gz",
    ".7z", ".tar", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".woff",
    ".woff2", ".ttf", ".mp3", ".mp4", ".sqlite", ".sqlite3", ".db", ".bin", ".wasm",
    ".p12", ".pfx", ".pem", ".key", ".der", ".keystore", ".jks",
})
_SECRET_NAME = re.compile(r"(^|[._-])(secret[s]?|credential[s]?|password[s]?)([._-]|$)", re.IGNORECASE)
_SECRET_VALUE = re.compile(
    # Match a complete credential name, including service prefixes such as
    # AWS_SECRET_ACCESS_KEY or CUSTOM_API_KEY, without matching suffixes such
    # as API_KEY_LENGTH or unrelated identifiers such as token_count.
    r"(?i)(?<![\w-])(?P<prefix>(?P<key_quote>[\"']?)"
    r"(?:[a-z][a-z0-9]*[_-])*(?:api[_-]?key|secret[_-]?access[_-]?key|"
    r"access[_-]?key(?:[_-]?id)?|secret[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"token|secret|password|passwd|authorization|private[_-]?key)"
    r"(?P=key_quote)(?P<separator>\s*[:=]\s*))"
    r"(?P<value>\"(?:\\.|[^\"\\\r\n])*\"|'(?:\\.|[^'\\\r\n])*'|"
    r"[^\s,;\]\}\[\{\"'\r\n]+)"
)


def _redact_assignment(match: re.Match) -> str:
    value = match.group("value")
    if value[0] in {'"', "'"}:
        replacement = value[0] + "[REDACTED]" + value[0]
    elif value.casefold() in {"null", "none", "true", "false"}:
        return match.group(0)
    elif match.group("key_quote") == '"' and ":" in match.group("separator"):
        # A JSON numeric scalar needs a quoted replacement to remain valid JSON.
        replacement = '"[REDACTED]"'
    else:
        replacement = "[REDACTED]"
    return match.group("prefix") + replacement


def redact(text: str) -> str:
    """Redact common credential forms; callers must apply this to string values."""
    text = str(text)
    text = re.sub(
        r"-----BEGIN (?:[A-Z ]* )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]* )?PRIVATE KEY-----",
        lambda match: "[REDACTED PRIVATE KEY]" + "\n" * match.group(0).count("\n"), text,
    )
    text = re.sub(r"(?i)\b(https?|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s/@]+:[^\s/@]+@",
                  r"\1://[REDACTED]@", text)
    text = re.sub(r"(?i)\b(Bearer|Basic)(\s+)[A-Za-z0-9_.~+/=-]+", r"\1\2[REDACTED]", text)
    text = _SECRET_VALUE.sub(_redact_assignment, text)
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,}|"
                  r"sk-(?:proj-)?[A-Za-z0-9_-]{12,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b",
                  "[REDACTED]", text)
    return text


def is_denied_path(path: str | Path) -> bool:
    """Return True for private, generated, dependency and binary relative paths."""
    value = str(path).replace("\\", "/")
    if not value or "\x00" in value or ":" in value or PureWindowsPath(value).is_absolute():
        return True
    if PurePosixPath(value).is_absolute():
        return True
    parts = PurePosixPath(value).parts
    for part in parts:
        name = part.casefold()
        if part != part.rstrip(" .") or re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", name):
            return True
        if name in {"..", "."} or name in DENIED_PARTS:
            return True
        if name.startswith(".env") or name.endswith((".egg-info", ".dist-info")):
            return True
        if _SECRET_NAME.search(name) or name in {"id_rsa", "id_ed25519", ".netrc", ".npmrc", ".pypirc"}:
            return True
    return PurePosixPath(value).suffix.casefold() in BINARY_SUFFIXES


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def safe_path(root: Path, path: str) -> Path:
    """Resolve an existing relative path without traversing links or denylisted paths."""
    value = str(path).replace("\\", "/")
    if is_denied_path(value):
        raise ValueError("Path is absolute, traversing, private, generated, or binary")
    boundary = Path(root).absolute()
    if _is_link(boundary):
        raise ValueError("Repository root cannot be a symbolic link or reparse point")
    boundary = boundary.resolve(strict=True)
    current = boundary
    for part in PurePosixPath(value).parts:
        current = current / part
        if _is_link(current):
            raise ValueError("Symbolic links and reparse points are excluded")
    target = current.resolve(strict=True)
    if target == boundary or not target.is_relative_to(boundary):
        raise ValueError("Path must stay within the repository")
    return target


def _text(data: bytes) -> str | None:
    if b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    controls = sum(ord(char) < 32 and char not in "\r\n\t\f" for char in text)
    if controls > max(2, len(text) // 100):
        return None
    return text


def _tokens(text: str) -> Counter:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text).casefold()
    terms = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", text)
    terms.extend(part for word in list(terms) if "_" in word for part in word.split("_") if part)
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        terms.extend(segment[index:index + 2] for index in range(len(segment) - 1))
    return Counter(terms)


def _ngrams(text: str) -> Counter:
    value = re.sub(r"\s+", " ", text.casefold())
    return Counter(value[i:i + n] for n in CHAR_NGRAM_SIZES for i in range(len(value) - n + 1))


def make_evidence(source: str, path: str, content: str, *, line_start: int = 1,
                  line_end: int | None = None, score: float = 0, kind: str = "code",
                  sha256: str | None = None) -> dict:
    content = redact(content)[:MAX_CONTENT_CHARS]
    line_end = line_end if line_end is not None else line_start + max(0, len(content.splitlines()) - 1)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    identity = f"{source}\0{path}\0{line_start}\0{line_end}\0{content}"
    return {"id": "ev_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
            "source": source, "path": path, "line_start": line_start, "line_end": line_end,
            "content": content, "score": round(float(score), 8), "kind": kind,
            "sha256": sha256 or digest}


@dataclass
class _Document:
    path: str
    start: int
    end: int
    terms: Counter
    grams: Counter
    norm: float = 0
    # Character offsets refer to the redacted single line, never source columns.
    char_start: int | None = None
    char_end: int | None = None


@dataclass
class _File:
    digest: str
    lines: list[str]
    docs: list[_Document]


def _redacted_lines(text: str) -> list[str]:
    # Redact the complete input before slicing so a credential/private-key value
    # cannot escape redaction when its prefix lies in another chunk or read range.
    # Normalize original line boundaries first; redaction preserves these newlines.
    return redact("\n".join(text.splitlines() or [""])).split("\n")


def _chunk_documents(path: str, lines: list[str]) -> list[_Document]:
    docs = []

    def append(start: int, end: int, content: str, char_start: int | None = None,
               char_end: int | None = None) -> None:
        child = path + "\n" + content
        docs.append(_Document(path, start, end, _tokens(child), _ngrams(child),
                              char_start=char_start, char_end=char_end))

    start, size = 0, 0
    pending: list[str] = []
    for number, line in enumerate(lines):
        if pending and (len(pending) >= MAX_CHUNK_LINES or size + len(line) + 1 > MAX_CHUNK_CHARS):
            append(start, number, "\n".join(pending))
            pending, size = [], 0
        if len(line) > MAX_CHUNK_CHARS:
            # Overlap keeps short symbols spanning a boundary in a complete window.
            for offset in range(0, len(line), MAX_CHUNK_CHARS - LONG_LINE_OVERLAP):
                stop = min(offset + MAX_CHUNK_CHARS, len(line))
                append(number, number + 1, line[offset:stop], offset, stop)
                if stop == len(line):
                    break
            continue
        if not pending:
            start = number
        size += len(line) + bool(pending)
        pending.append(line)
    if pending:
        append(start, len(lines), "\n".join(pending))
    return docs


class RepositoryIndex:
    def __init__(self, root: Path, *, max_files: int = 1200, max_file_bytes: int = 250000):
        candidate = Path(root).absolute()
        if _is_link(candidate):
            raise ValueError("Repository root cannot be a symbolic link or reparse point")
        self.root = candidate.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Repository root must be a directory")
        self.max_files = max(1, min(int(max_files), 10000))
        self.max_file_bytes = max(1, min(int(max_file_bytes), 2_000_000))
        self._records: dict[str, _File] = {}
        self._documents: list[_Document] = []
        self._term_df: Counter = Counter()
        self._gram_df: Counter = Counter()
        self._avg_length = 1.0
        self._lock = threading.RLock()

    def _load(self, path: str) -> tuple[str, str] | None:
        target = safe_path(self.root, path)
        before = target.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > self.max_file_bytes:
            return None
        # Verify the path again after opening, including reparse points on Windows.
        with target.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("Repository file changed while opening")
            verified = safe_path(self.root, path).stat()
            if (verified.st_dev, verified.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("Repository file changed while opening")
            data = handle.read(self.max_file_bytes + 1)
        if len(data) > self.max_file_bytes:
            return None
        text = _text(data)
        return (hashlib.sha256(data).hexdigest(), text) if text is not None else None

    def build(self) -> dict:
        """Refresh content hashes, reusing unchanged chunks and removing deleted files."""
        with self._lock:
            records: dict[str, _File] = {}
            added = updated = unchanged = skipped = 0
            for directory, names, filenames in os.walk(self.root, followlinks=False):
                parent = Path(directory)
                kept = []
                for name in sorted(names):
                    relative = (parent / name).relative_to(self.root).as_posix()
                    try:
                        if not is_denied_path(relative) and not _is_link(parent / name):
                            kept.append(name)
                        else:
                            skipped += 1
                    except OSError:
                        skipped += 1
                names[:] = kept
                for name in sorted(filenames):
                    if len(records) >= self.max_files:
                        break
                    relative = (parent / name).relative_to(self.root).as_posix()
                    if is_denied_path(relative):
                        skipped += 1
                        continue
                    try:
                        loaded = self._load(relative)
                    except (OSError, ValueError):
                        skipped += 1
                        continue
                    if loaded is None:
                        skipped += 1
                        continue
                    digest, text = loaded
                    old = self._records.get(relative)
                    if old and old.digest == digest:
                        records[relative] = old
                        unchanged += 1
                        continue
                    lines = _redacted_lines(text)
                    docs = _chunk_documents(relative, lines)
                    records[relative] = _File(digest, lines, docs)
                    if old:
                        updated += 1
                    else:
                        added += 1
                if len(records) >= self.max_files:
                    break
            deleted = len(self._records.keys() - records.keys())
            changed = bool(added or updated or deleted)
            self._records = records
            if changed or not self._documents:
                self._documents = [doc for record in records.values() for doc in record.docs]
                self._term_df = Counter(term for doc in self._documents for term in doc.terms)
                self._gram_df = Counter(gram for doc in self._documents for gram in doc.grams)
                count = len(self._documents)
                self._avg_length = sum(sum(doc.terms.values()) for doc in self._documents) / max(1, count)
                for doc in self._documents:
                    doc.norm = math.sqrt(sum(((1 + math.log(tf)) * self._idf(df, count)) ** 2
                                             for gram, tf in doc.grams.items() if (df := self._gram_df[gram])))
            return {"files": len(records), "chunks": len(self._documents), "added": added,
                    "updated": updated, "deleted": deleted, "unchanged": unchanged, "skipped": skipped,
                    "method": "BM25 + character n-gram TF-IDF cosine + reciprocal rank fusion"}

    @staticmethod
    def _idf(frequency: int, count: int) -> float:
        return math.log((1 + count) / (1 + frequency)) + 1

    def files(self) -> list[str]:
        with self._lock:
            self.build()
            return sorted(self._records)

    def _evidence(self, doc: _Document, score: float) -> dict:
        record = self._records[doc.path]
        lines = record.lines
        start, end = doc.start, doc.end
        if doc.char_start is not None:
            evidence = make_evidence("repository", doc.path, lines[start][doc.char_start:doc.char_end],
                                     line_start=start + 1, line_end=end, score=score,
                                     kind="log" if Path(doc.path).suffix.casefold() == ".log" else "code",
                                     sha256=record.digest)
            evidence["partial_line"] = True
            return evidence
        # Prefer the matching child, then expand to its surrounding parent context.
        size = len("\n".join(lines[start:end]))
        for _ in range(PARENT_CONTEXT_LINES):
            if start and size + len(lines[start - 1]) + 1 <= MAX_CONTENT_CHARS:
                start -= 1
                size += len(lines[start]) + 1
            if end < len(lines) and size + len(lines[end]) + 1 <= MAX_CONTENT_CHARS:
                size += len(lines[end]) + 1
                end += 1
        return make_evidence("repository", doc.path, "\n".join(lines[start:end]),
                             line_start=start + 1, line_end=end, score=score,
                             kind="log" if Path(doc.path).suffix.casefold() == ".log" else "code",
                             sha256=record.digest)

    def search(self, query: str, limit: int = 8, *,
               method: Literal["bm25", "char", "rrf"] = "rrf") -> list[dict]:
        """Retrieve with a selected lexical ranker and shared evidence boundaries."""
        if method not in RETRIEVAL_METHODS:
            raise ValueError("Unknown retrieval method; expected bm25, char, or rrf")
        query = str(query)[:MAX_QUERY_CHARS]
        limit = max(0, min(int(limit), MAX_SEARCH_RESULTS))
        if not query.strip() or not limit:
            return []
        with self._lock:
            # Each query refreshes real disk state so removed/replaced paths cannot leak stale evidence.
            self.build()
            terms = _tokens(query) if method in {"bm25", "rrf"} else Counter()
            grams = _ngrams(query) if method in {"char", "rrf"} else Counter()
            count = len(self._documents)
            query_weights = {gram: (1 + math.log(tf)) * self._idf(self._gram_df[gram], count)
                             for gram, tf in grams.items() if gram in self._gram_df}
            query_norm = math.sqrt(sum(weight * weight for weight in query_weights.values()))
            bm25, cosine = [], []
            for position, doc in enumerate(self._documents):
                score = 0.0
                length = sum(doc.terms.values())
                for term in terms:
                    tf = doc.terms.get(term, 0)
                    if tf:
                        df = self._term_df[term]
                        inverse = math.log(1 + (count - df + 0.5) / (df + 0.5))
                        score += inverse * tf * (BM25_K1 + 1) / (
                            tf + BM25_K1 * (1 - BM25_B + BM25_B * length / max(1, self._avg_length)))
                if score > 0:
                    bm25.append((position, score))
                if query_norm and doc.norm:
                    dot = sum(weight * (1 + math.log(doc.grams[gram])) * self._idf(self._gram_df[gram], count)
                              for gram, weight in query_weights.items() if gram in doc.grams)
                    if dot > 0:
                        cosine.append((position, dot / (query_norm * doc.norm)))
            rankings = {"bm25": sorted(bm25, key=lambda item: (-item[1], item[0]))[:CANDIDATE_LIMIT],
                        "char": sorted(cosine, key=lambda item: (-item[1], item[0]))[:CANDIDATE_LIMIT]}
            if method == "rrf":
                fused: Counter = Counter()
                for ranking in rankings.values():
                    for rank, (position, _) in enumerate(ranking, 1):
                        fused[position] += 1 / (RRF_K + rank)
                ranking = sorted(fused.items(), key=lambda item: (-item[1], item[0]))
            else:
                ranking = rankings[method]
            results, identities = [], set()
            for position, score in ranking:
                doc = self._documents[position]
                evidence = self._evidence(doc, score)
                # Suppress a child only when its complete range is already present.
                # Nearby character-bounded chunks may contain different evidence.
                if evidence["id"] in identities or any(
                    previous["path"] == doc.path and not previous.get("partial_line")
                    and previous["line_start"] <= doc.start + 1 and previous["line_end"] >= doc.end
                    for previous in results
                ):
                    continue
                results.append(evidence)
                identities.add(evidence["id"])
                if len(results) >= limit:
                    break
            return results

    def read(self, path: str, start: int = 1, end: int = 120) -> dict:
        with self._lock:
            # Validate direct reads independently of index membership.
            canonical = safe_path(self.root, str(path)).relative_to(self.root).as_posix()
            loaded = self._load(canonical)
            if loaded is None:
                raise ValueError("File is binary or exceeds the configured size limit")
            digest, text = loaded
            lines = _redacted_lines(text)
            start, end = int(start), int(end)
            if start < 1 or end < start or start > len(lines):
                raise ValueError("Requested line range is invalid")
            stop = min(end, len(lines), start + 199)
            selected = []
            size = 0
            for line in lines[start - 1:stop]:
                if selected and size + len(line) + 1 > MAX_CONTENT_CHARS:
                    break
                selected.append(line[:MAX_CONTENT_CHARS])
                size += len(selected[-1]) + 1
            evidence = make_evidence("repository", canonical, "\n".join(selected),
                                     line_start=start, line_end=start + len(selected) - 1,
                                     kind="log" if Path(canonical).suffix.casefold() == ".log" else "code", sha256=digest)
            if len(lines[start - 1]) > MAX_CONTENT_CHARS:
                evidence["partial_line"] = True
            return evidence
