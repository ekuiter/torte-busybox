#!/usr/bin/env python3
"""Local review UI for BusyBox feature-mapping candidates."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import webbrowser
import zipfile
from collections import defaultdict
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DEFAULT_RESULTS = ROOT.parent / "lmstudio-feature-mappings-q6-final" / "results.jsonl"
DEFAULT_REPO = (
    ROOT.parent / "torte-busybox"
    if (ROOT.parent / "torte-busybox").exists()
    else ROOT.parents[1]
)
DEFAULT_FEATURES = (
    Path("stages/main/")
    / "6_extract_kconfig_models/extract-kconfig-models-with-kclause/busybox"
)
DEFAULT_KCONFIGREADER_MODELS = DEFAULT_FEATURES.parent.parent / "extract-kconfig-models-with-kconfigreader" / "busybox"
DEFAULT_REVIEWS = ROOT / "reviews.json"
DEFAULT_EXPORT = ROOT.parent / "busybox-feature-mappings.zip"
DECISIONS = {"pending", "accepted", "rejected", "uncertain"}
DATE_PREFIX = re.compile(r"^\[[^]]+\]")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def latest_records(path: Path) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid JSON at {path}:{number}: {exc}") from exc
            records[record["commit"]] = record
    return list(records.values())


def release_name(path: Path) -> str:
    return DATE_PREFIX.sub("", path.stem)


def release_key(release: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in release.split("_"))
    except ValueError as exc:
        raise RuntimeError(f"unsupported BusyBox release name: {release}") from exc


def load_release_pairs(directory: Path) -> list[dict[str, Any]]:
    releases: list[tuple[str, set[str]]] = []
    for path in directory.glob("*.features"):
        releases.append((release_name(path), set(path.read_text(encoding="utf-8").splitlines())))
    releases.sort(key=lambda item: release_key(item[0]))
    return [
        {
            "left": left[0],
            "right": right[0],
            "removed": left[1] - right[1],
            "added": right[1] - left[1],
        }
        for left, right in zip(releases, releases[1:])
    ]


def load_model_release_pairs(directory: Path) -> list[dict[str, Any]]:
    # Clausy parses only variables that occur in the formula. KConfigReader's
    # `#item` declarations also list unconstrained symbols, but those do not
    # affect formula semantics and must not make a mapping export-compatible.
    # Capture the complete token inside def(...). KConfigReader represents
    # non-Boolean symbols as value atoms such as `NAME=n` or `NAME=3`; treating
    # their `NAME` prefix as a standalone variable would make invalid mappings
    # appear compatible with Clausy.
    variable_pattern = re.compile(r"def\(([^()]*)\)")
    releases: list[tuple[str, set[str]]] = []
    for path in directory.glob("*.model"):
        variables = set(variable_pattern.findall(path.read_text(encoding="utf-8")))
        releases.append((release_name(path), variables))
    releases.sort(key=lambda item: release_key(item[0]))
    return [
        {
            "left": left[0],
            "right": right[0],
            "removed": left[1] - right[1],
            "added": right[1] - left[1],
        }
        for left, right in zip(releases, releases[1:])
    ]


def candidate_id(commit: str, index: int, candidate: dict[str, Any]) -> str:
    identity = json.dumps(
        [commit, index, candidate.get("old_features", []), candidate.get("new_features", [])],
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:20]


def clausy_mapping(candidate: dict[str, Any]) -> str:
    return f"{','.join(candidate['old_features'])}={','.join(candidate['new_features'])}"


def applicable_pairs(candidate: dict[str, Any], pairs: list[dict[str, Any]]) -> list[dict[str, str]]:
    old = set(candidate["old_features"])
    new = set(candidate["new_features"])
    return [
        {"left": pair["left"], "right": pair["right"]}
        for pair in pairs
        if old <= pair["removed"] and new <= pair["added"]
    ]


class ReviewStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, dict[str, Any]] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.data = raw.get("reviews", raw)

    def get(self, identifier: str) -> dict[str, Any]:
        return {"decision": "pending", "note": "", "excluded": False, **self.data.get(identifier, {})}

    def save(self, identifier: str, decision: str, note: str) -> None:
        if decision not in DECISIONS:
            raise ValueError(f"invalid decision: {decision}")
        with self.lock:
            excluded = bool(self.get(identifier)["excluded"])
            self.data[identifier] = {"decision": decision, "note": note.strip()}
            if excluded:
                self.data[identifier]["excluded"] = True
            self._write()

    def save_many(self, identifiers: list[str], decision: str, note: str, only_pending: bool = True) -> int:
        if decision not in DECISIONS:
            raise ValueError(f"invalid decision: {decision}")
        changed = 0
        with self.lock:
            for identifier in identifiers:
                review = self.get(identifier)
                if review["excluded"]:
                    continue
                current = review["decision"]
                if only_pending and current != "pending":
                    continue
                self.data[identifier] = {"decision": decision, "note": note.strip()}
                changed += 1
            self._write()
        return changed

    def _write(self) -> None:
        payload = json.dumps({"schema_version": 1, "reviews": self.data}, indent=2, sort_keys=True)
        atomic_write(self.path, payload + "\n")


class ReviewData:
    def __init__(
        self, results: Path, repo: Path, features: Path, kclause_models: Path, kconfigreader_models: Path,
        reviews: Path, export: Path,
    ):
        self.results_path = results
        self.repo = repo
        self.features_path = features
        self.export_path = export
        self.store = ReviewStore(reviews)
        pairs = load_release_pairs(features)
        extractor_pairs = {
            "kclause": {
                (pair["left"], pair["right"]): pair for pair in load_model_release_pairs(kclause_models)
            },
            "kconfigreader": {
                (pair["left"], pair["right"]): pair for pair in load_model_release_pairs(kconfigreader_models)
            },
        }
        self.candidates: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        for record in latest_records(results):
            if record.get("status") != "ok":
                continue
            for index, raw in enumerate(record.get("result", {}).get("candidates", [])):
                identifier = candidate_id(record["commit"], index, raw)
                release_pairs = applicable_pairs(raw, pairs)
                for pair in release_pairs:
                    key = (pair["left"], pair["right"])
                    pair["extractors"] = [
                        extractor
                        for extractor, model_pairs in extractor_pairs.items()
                        if (model_pair := model_pairs.get(key)) and applicable_pairs(raw, [model_pair])
                    ]
                    pair["incompatible_extractors"] = [
                        extractor for extractor in extractor_pairs if extractor not in pair["extractors"]
                    ]
                item = {
                    **raw,
                    "id": identifier,
                    "commit": record["commit"],
                    "parent": record.get("parent"),
                    "subject": record.get("subject", ""),
                    "author_date": record.get("author_date", ""),
                    "summary": record.get("result", {}).get("summary", ""),
                    "analyzed_at": record.get("analyzed_at", ""),
                    "duration_seconds": record.get("duration_seconds"),
                    "model": record.get("model", ""),
                    "mapping": clausy_mapping(raw),
                    "release_pairs": release_pairs,
                }
                self.candidates.append(item)
                self.by_id[identifier] = item

    def public_candidates(self) -> list[dict[str, Any]]:
        return [
            {**item, **review}
            for item in self.candidates
            if not (review := self.store.get(item["id"]))["excluded"]
        ]

    def stats(self) -> dict[str, Any]:
        candidates = self.public_candidates()
        decisions = {decision: 0 for decision in DECISIONS}
        for item in candidates:
            decisions[item["decision"]] += 1
        return {
            "candidates": len(candidates),
            "commits": len({item["commit"] for item in candidates}),
            "applicable": sum(bool(item["release_pairs"]) for item in candidates),
            "excluded": sum(bool(self.store.get(item["id"])["excluded"]) for item in self.candidates),
            "decisions": decisions,
        }

    def git_diff(self, identifier: str, full: bool = False) -> str:
        candidate = self.by_id[identifier]
        return self._git_diff(candidate["commit"], candidate.get("parent"), [
            *candidate["old_features"], *candidate["new_features"],
        ], full)

    def git_commit_diff(self, commit: str, full: bool = False) -> str:
        candidates = [
            item for item in self.candidates
            if item["commit"] == commit and not self.store.get(item["id"])["excluded"]
        ]
        if not candidates:
            raise KeyError(commit)
        symbols = [
            symbol
            for candidate in candidates
            for symbol in [*candidate["old_features"], *candidate["new_features"]]
        ]
        return self._git_diff(commit, candidates[0].get("parent"), symbols, full)

    def _git_diff(self, commit: str, parent: str | None, symbols: list[str], full: bool) -> str:
        if not parent:
            return "This root commit has no parent diff."
        command = [
            "git", "-C", str(self.repo), "diff", "--find-renames", "--find-copies",
            "--no-ext-diff", "--no-color", "-U8", parent, commit,
        ]
        process = subprocess.run(command, text=True, errors="replace", capture_output=True)
        if process.returncode:
            raise RuntimeError(process.stderr.strip())
        diff = process.stdout
        if full:
            return diff[:2_000_000]
        return focused_diff(diff, symbols) or diff[:250_000]

    def export(self) -> dict[str, Any]:
        grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        skipped_no_pair = 0
        incompatible: list[dict[str, str]] = []
        for item in self.public_candidates():
            if item["decision"] != "accepted":
                continue
            if not item["release_pairs"]:
                skipped_no_pair += 1
                continue
            for pair in item["release_pairs"]:
                for extractor in pair["extractors"]:
                    grouped[(extractor, pair["left"], pair["right"])].append(item)
                for extractor in pair["incompatible_extractors"]:
                    incompatible.append({
                        "candidate": item["id"],
                        "commit": item["commit"],
                        "mapping": item["mapping"],
                        "left_release": pair["left"],
                        "right_release": pair["right"],
                        "extractor": extractor,
                        "reason": "at least one mapping endpoint is absent from the extractor's .model formula",
                    })

        manifest = ["extractor,left_release,right_release,map_file,accepted_mappings"]
        archive_files: dict[str, str] = {}
        exported_rules = 0
        sort_key = lambda value: (release_key(value[0][1]), value[0][0])
        for (extractor, left, right), items in sorted(grouped.items(), key=sort_key):
            filename = f"{left}--{right}--{extractor}.map"
            seen: set[str] = set()
            lines = [
                "# generated by the BusyBox mapping review UI",
                f"# release comparison: {left} -> {right}",
                f"# extractor: {extractor}",
            ]
            for item in items:
                mapping = item["mapping"]
                if mapping in seen:
                    continue
                seen.add(mapping)
                rationale = " ".join(str(item.get("rationale", "")).split())
                lines.extend([
                    f"# candidate {item['id']}; commit {item['commit']}",
                    f"# {item['kind']}; LM confidence {item['confidence']} ({item['confidence_score']})",
                    f"# {rationale}",
                ])
                review_note = str(item.get("note", "")).strip()
                if review_note:
                    lines.append("# review note:")
                    lines.extend(f"#   {line}" if line else "#" for line in review_note.splitlines())
                lines.append(mapping)
            archive_files[f"reviewed-mappings/{filename}"] = "\n".join(lines) + "\n"
            manifest.append(f"{extractor},{left},{right},{filename},{len(seen)}")
            exported_rules += len(seen)
        archive_files["reviewed-mappings/release-pairs.csv"] = "\n".join(manifest) + "\n"
        archive_files["reviewed-mappings/excluded-incompatible.json"] = json.dumps(
            incompatible, indent=2, sort_keys=True
        ) + "\n"
        archive_files["reviewed-mappings/review-decisions.json"] = json.dumps([
            {
                "candidate": item["id"],
                "commit": item["commit"],
                "mapping": item["mapping"],
                **self.store.get(item["id"]),
            }
            for item in self.candidates
        ], indent=2, sort_keys=True) + "\n"

        csv_fields = [
            "candidate_id", "local_commit", "upstream_commit", "commit_date", "kind",
            "old_features", "new_features", "clausy_mapping", "confidence", "confidence_score",
            "decision", "excluded", "review_note", "rationale", "evidence", "manual_check",
            "model_summary", "source_release_pairs", "kclause_release_pairs",
            "kconfigreader_release_pairs", "exported", "export_explanation", "model",
            "analyzed_at", "duration_seconds",
        ]
        csv_buffer = io.StringIO(newline="")
        writer = csv.DictWriter(csv_buffer, fieldnames=csv_fields, lineterminator="\n")
        writer.writeheader()
        for item in self.candidates:
            review = self.store.get(item["id"])
            source_pairs = [f"{pair['left']} -> {pair['right']}" for pair in item["release_pairs"]]
            extractor_release_pairs = {
                extractor: [
                    f"{pair['left']} -> {pair['right']}"
                    for pair in item["release_pairs"] if extractor in pair["extractors"]
                ]
                for extractor in ("kclause", "kconfigreader")
            }
            exported = bool(
                review["decision"] == "accepted"
                and not review["excluded"]
                and any(extractor_release_pairs.values())
            )
            if review["excluded"]:
                export_explanation = "Excluded after human review; decision retained for audit"
            elif review["decision"] != "accepted":
                export_explanation = f"Review decision is {review['decision']}"
            elif not source_pairs:
                export_explanation = "No matching consecutive staged release pair"
            elif not any(extractor_release_pairs.values()):
                export_explanation = "Mapping endpoints are absent from the extractor formulas"
            else:
                export_explanation = "Included in at least one extractor-specific map"
            writer.writerow({
                "candidate_id": item["id"],
                "local_commit": item["commit"],
                "upstream_commit": item["subject"],
                "commit_date": item["author_date"],
                "kind": item["kind"],
                "old_features": ";".join(item["old_features"]),
                "new_features": ";".join(item["new_features"]),
                "clausy_mapping": item["mapping"],
                "confidence": item["confidence"],
                "confidence_score": item["confidence_score"],
                "decision": review["decision"],
                "excluded": str(bool(review["excluded"])).lower(),
                "review_note": review["note"],
                "rationale": item.get("rationale", ""),
                "evidence": "\n".join(item.get("evidence", [])),
                "manual_check": item.get("manual_check", ""),
                "model_summary": item.get("summary", ""),
                "source_release_pairs": ";".join(source_pairs),
                "kclause_release_pairs": ";".join(extractor_release_pairs["kclause"]),
                "kconfigreader_release_pairs": ";".join(extractor_release_pairs["kconfigreader"]),
                "exported": str(exported).lower(),
                "export_explanation": export_explanation,
                "model": item.get("model", ""),
                "analyzed_at": item.get("analyzed_at", ""),
                "duration_seconds": item.get("duration_seconds", ""),
            })
        archive_files["reviewed-mappings/mapping-review.csv"] = csv_buffer.getvalue()
        archive_files["reviewed-mappings/metadata.json"] = json.dumps({
            "format": 1,
            "source_results": self.results_path.name,
            "results_sha256": hashlib.sha256(self.results_path.read_bytes()).hexdigest(),
            "reviews_sha256": hashlib.sha256(self.store.path.read_bytes()).hexdigest(),
            "candidate_records": len(self.candidates),
            "active_reviewed_candidates": len(self.public_candidates()),
            "accepted_mapping_rules": exported_rules,
            "extractor_release_pairs": len(grouped),
            "accepted_without_release_pair": skipped_no_pair,
            "extractor_incompatible_rules": len(incompatible),
        }, indent=2, sort_keys=True) + "\n"

        self.export_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.export_path.name}.", suffix=".tmp", dir=self.export_path.parent
        )
        os.close(descriptor)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, contents in sorted(archive_files.items()):
                    archive.writestr(name, contents)
            os.replace(temporary, self.export_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {
            "archive": str(self.export_path),
            "release_pairs": len(grouped),
            "mappings": exported_rules,
            "skipped_without_release_pair": skipped_no_pair,
            "skipped_incompatible": len(incompatible),
        }


def focused_diff(diff: str, symbols: list[str]) -> str:
    needles = tuple(dict.fromkeys(symbols))
    selected: list[str] = []
    for block in re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE):
        if not block.strip():
            continue
        header, *hunks = re.split(r"(?=^@@ )", block, flags=re.MULTILINE)
        matching = [hunk for hunk in hunks if any(symbol in hunk for symbol in needles)]
        if matching:
            selected.append(header + "".join(matching))
    return "".join(selected)


class Handler(SimpleHTTPRequestHandler):
    data: ReviewData

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(ROOT / "static"), **kwargs)

    def json_response(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/candidates":
                self.json_response({"stats": self.data.stats(), "candidates": self.data.public_candidates()})
                return
            if parsed.path == "/api/diff":
                query = parse_qs(parsed.query)
                identifier = query.get("id", [""])[0]
                if identifier not in self.data.by_id:
                    self.json_response({"error": "unknown candidate"}, HTTPStatus.NOT_FOUND)
                    return
                self.json_response({"diff": self.data.git_diff(identifier, query.get("full") == ["1"])})
                return
            if parsed.path == "/api/commit-diff":
                query = parse_qs(parsed.query)
                commit = query.get("commit", [""])[0]
                if not any(item["commit"] == commit for item in self.data.candidates):
                    self.json_response({"error": "unknown commit"}, HTTPStatus.NOT_FOUND)
                    return
                self.json_response({"diff": self.data.git_commit_diff(commit, query.get("full") == ["1"])})
                return
            super().do_GET()
        except Exception as exc:
            self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        try:
            size = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(size) or b"{}")
            if self.path == "/api/reviews":
                identifier = str(payload.get("id", ""))
                if identifier not in self.data.by_id:
                    self.json_response({"error": "unknown candidate"}, HTTPStatus.NOT_FOUND)
                    return
                self.data.store.save(identifier, str(payload.get("decision", "pending")), str(payload.get("note", "")))
                self.json_response({"ok": True, "stats": self.data.stats()})
                return
            if self.path == "/api/reviews/bulk":
                commit = str(payload.get("commit", ""))
                identifiers = [item["id"] for item in self.data.candidates if item["commit"] == commit]
                if not identifiers:
                    self.json_response({"error": "unknown commit"}, HTTPStatus.NOT_FOUND)
                    return
                changed = self.data.store.save_many(
                    identifiers,
                    str(payload.get("decision", "pending")),
                    str(payload.get("note", "")),
                    bool(payload.get("only_pending", True)),
                )
                self.json_response({"ok": True, "changed": changed, "stats": self.data.stats()})
                return
            if self.path == "/api/export":
                self.json_response(self.data.export())
                return
            self.json_response({"error": "unknown endpoint"}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as exc:
            self.json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.json_response({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, message: str, *args: Any) -> None:
        print(f"[review-ui] {message % args}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--features", type=Path, default=DEFAULT_FEATURES)
    parser.add_argument("--kclause-models", type=Path, default=DEFAULT_FEATURES)
    parser.add_argument("--kconfigreader-models", type=Path, default=DEFAULT_KCONFIGREADER_MODELS)
    parser.add_argument("--reviews", type=Path, default=DEFAULT_REVIEWS)
    parser.add_argument("--export-archive", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--check", action="store_true", help="validate inputs and print dataset statistics")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data = ReviewData(
        args.results.resolve(), args.repo.resolve(), args.features.resolve(), args.kclause_models.resolve(),
        args.kconfigreader_models.resolve(), args.reviews.resolve(), args.export_archive.resolve(),
    )
    stats = data.stats()
    if args.check:
        print(json.dumps(stats, indent=2, sort_keys=True))
        return 0
    Handler.data = data
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"BusyBox mapping review UI: {url}")
    print(
        f"Review units: {stats['commits']} commits containing {stats['candidates']} mappings; "
        f"{stats['applicable']} mappings apply to a release pair"
    )
    print(f"Reviews: {args.reviews.resolve()}")
    print(f"Approved-map archive: {args.export_archive.resolve()}")
    if not args.no_open:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
