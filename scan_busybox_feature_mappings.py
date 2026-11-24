#!/usr/bin/env python3
"""Find likely Kconfig feature renames, splits, and merges with local LM Studio.

Each Git commit is analyzed by one independent OpenAI-compatible chat-completion
request.  Results are resumably stored as JSONL, then exported as review CSV and
Clausy-compatible mapping files (old/parent features on the left, new/commit
features on the right).

The script uses only Python's standard library.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


CONFIG_DECL = re.compile(r"^[+-]\s*(?:menu)?config\s+([A-Za-z0-9_]+)\s*$")
CONFIDENCES = ("possible", "likely", "very_likely")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

SYSTEM_PROMPT = r"""You are analyzing one BusyBox Git commit for feature-model evolution.
Treat Kconfig symbols as features. Determine whether this commit contains a clean feature
rename, split, or merge that should be supplied as additional knowledge to semantic feature-
model differencing.

Direction and meanings:
- rename: exactly one feature in the parent became exactly one feature in the commit.
- split: exactly one parent feature became two or more commit features.
- merge: two or more parent features became exactly one commit feature.

Be conservative. A removed feature next to an unrelated added feature is not a rename. Ordinary
feature addition/removal is not a candidate.
Every old feature must come from old_only_features, and every new feature from new_only_features;
this exclusivity is required by clausy. If the evidence is weak, use "possible". Use "likely"
for strong circumstantial evidence and "very_likely" only for explicit or near-conclusive evidence.
Do not invent symbols. A symbol merely moved between files under the same name is not a rename.
If either exclusivity list is empty, no rename, split, or merge is possible. Confidence scores
must be decimals from 0.0 to 1.0, not percentages. Do not report many-to-many mappings.
Return no candidates when appropriate.
Each candidate should explain what a human should verify before using it in clausy."""

RESPONSE_SCHEMA: dict[str, Any] = {
    "name": "busybox_feature_mapping_candidates",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string", "enum": ["rename", "split", "merge"]},
                        "old_features": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "new_features": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "confidence": {"type": "string", "enum": list(CONFIDENCES)},
                        "confidence_score": {"type": "number", "minimum": 0, "maximum": 1},
                        "rationale": {"type": "string"},
                        "evidence": {"type": "array", "items": {"type": "string"}},
                        "manual_check": {"type": "string"},
                    },
                    "required": [
                        "kind", "old_features", "new_features", "confidence",
                        "confidence_score", "rationale", "evidence", "manual_check"
                    ],
                },
            },
        },
        "required": ["summary", "candidates"],
    },
}


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def get_commits(repo: Path, revision: str) -> list[str]:
    # topo-order keeps parents before children when reversed, including non-linear histories.
    return git(repo, "rev-list", "--reverse", "--topo-order", revision).splitlines()


def commit_metadata(repo: Path, commit: str) -> dict[str, str]:
    raw = git(repo, "show", "-s", "--format=%H%x00%P%x00%aI%x00%s%x00%b", commit)
    parts = raw.rstrip("\n").split("\x00", 4)
    return dict(zip(("commit", "parents", "author_date", "subject", "body"), parts, strict=True))


def changed_features(patch: str) -> tuple[list[str], list[str]]:
    removed: set[str] = set()
    added: set[str] = set()
    for line in patch.splitlines():
        match = CONFIG_DECL.match(line)
        if not match:
            continue
        (added if line.startswith("+") else removed).add(match.group(1))
    # A declaration merely moved or was edited, but kept its name.
    common = removed & added
    return sorted(removed - common), sorted(added - common)


def relevant_hunks(patch: str, features: Iterable[str]) -> str:
    """Retain diff hunks mentioning candidate symbols, including their local context."""
    needles = tuple({f for name in features for f in (name, f"CONFIG_{name}")})
    if not needles:
        return ""
    blocks = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
    chosen: list[str] = []
    for block in blocks:
        header, *hunks = re.split(r"(?=^@@ )", block, flags=re.MULTILINE)
        matching = [h for h in hunks if any(n in h for n in needles)]
        if matching:
            chosen.append(header + "".join(matching))
    return "".join(chosen)


def make_evidence(
    repo: Path, commit: str, parent: str, metadata: dict[str, str], max_chars: int
) -> tuple[str, list[str], list[str], bool]:
    patch = git(repo, "diff", "--find-renames", "--find-copies", "--no-ext-diff", "-U5", parent, commit)
    old_only, new_only = changed_features(patch)
    stat = git(repo, "diff", "--find-renames", "--stat", parent, commit)
    header = (
        f"commit: {commit}\nparent: {parent if parent != EMPTY_TREE else '(empty tree)'}\n"
        f"author_date: {metadata['author_date']}\nsubject: {metadata['subject']}\n"
        f"body:\n{metadata['body'].strip() or '(none)'}\n\nstat:\n{stat.strip() or '(none)'}\n\n"
        f"old_only_features: {json.dumps(old_only)}\n"
        f"new_only_features: {json.dumps(new_only)}\n\npatch:\n"
    )
    if len(header) + len(patch) <= max_chars:
        return header + patch, old_only, new_only, False

    focused = relevant_hunks(patch, [*old_only, *new_only])
    budget = max(0, max_chars - len(header) - 120)
    if len(focused) < budget:
        # Add the start of the full patch to expose context unrelated to explicit declarations.
        remainder = budget - len(focused)
        focused += "\n[START OF FULL PATCH]\n" + patch[:remainder]
    evidence = header + focused[:budget] + "\n[PATCH TRUNCATED; raise --max-diff-chars to include more]\n"
    return evidence, old_only, new_only, True


def http_json(url: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer lm-studio"},
        method="GET" if data is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LM Studio HTTP {exc.code}: {body[:2000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach LM Studio at {url}: {exc.reason}") from exc


def choose_model(base_url: str, requested: str | None, timeout: float) -> str:
    if requested:
        return requested
    models = http_json(f"{base_url}/models", None, timeout).get("data", [])
    if not models:
        raise RuntimeError("LM Studio reports no loaded models; load one or pass --model")
    return str(models[0]["id"])


def lmstudio_runtime_info(base_url: str, model: str, timeout: float) -> dict[str, Any] | None:
    """Return LM Studio variant/context details when its native API is available."""
    parsed = urllib.parse.urlsplit(base_url)
    native_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/api/v1/models", "", ""))
    try:
        models = http_json(native_url, None, min(timeout, 5.0)).get("models", [])
    except Exception:
        return None  # Keep compatibility with non-LM-Studio OpenAI endpoints.
    matched_item: dict[str, Any] | None = None
    for item in models:
        if item.get("key") == model:
            matched_item = item
        instances = item.get("loaded_instances", [])
        instance = next((x for x in instances if x.get("id") == model), None)
        if instance is None and item.get("key") == model and instances:
            instance = instances[0]
        if instance is not None:
            return {
                "loaded": True,
                "key": item.get("key"),
                "variant": item.get("selected_variant"),
                "quantization": item.get("quantization", {}).get("name"),
                "context_length": instance.get("config", {}).get("context_length"),
                "max_context_length": item.get("max_context_length"),
                "reasoning_default": item.get("capabilities", {}).get("reasoning", {}).get("default"),
            }
    # Some LM Studio builds expose the loaded model through inference and
    # `lms ps`, while /api/v1/models incorrectly leaves loaded_instances empty.
    # The CLI also distinguishes the concrete quantization variant accurately.
    if shutil.which("lms"):
        try:
            proc = subprocess.run(
                ["lms", "ps", "--json"], text=True, timeout=min(timeout, 5.0),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for item in json.loads(proc.stdout) if proc.returncode == 0 else []:
                if item.get("identifier") == model:
                    return {
                        "loaded": True,
                        "key": item.get("identifier"),
                        "variant": item.get("modelKey"),
                        "quantization": item.get("quantization", {}).get("name"),
                        "context_length": item.get("contextLength"),
                        "max_context_length": item.get("maxContextLength"),
                        "parallel": item.get("parallel"),
                    }
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
    if matched_item is not None:
        return {
            "loaded": False,
            "key": matched_item.get("key"),
            "variant": matched_item.get("selected_variant"),
            "quantization": matched_item.get("quantization", {}).get("name"),
            "context_length": None,
            "max_context_length": matched_item.get("max_context_length"),
        }
    return None


def analyze(
    base_url: str, model: str, evidence: str, timeout: float,
    max_tokens: int, reasoning_effort: str,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": evidence},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_schema", "json_schema": RESPONSE_SCHEMA},
    }
    if reasoning_effort != "default":
        # LM Studio maps "none" to Qwen's thinking-off chat template. Without
        # this, small output budgets can be consumed entirely by hidden reasoning.
        payload["reasoning_effort"] = reasoning_effort
    response = http_json(f"{base_url}/chat/completions", payload, timeout)
    try:
        content = response["choices"][0]["message"]["content"]
        if not content:
            reasoning = response["choices"][0]["message"].get("reasoning_content", "")
            finish = response["choices"][0].get("finish_reason")
            raise RuntimeError(
                f"model returned no final JSON (finish_reason={finish!r}, "
                f"reasoning_tokens_or_chars={len(reasoning)}); use --reasoning-effort none "
                "or increase --max-tokens"
            )
        if isinstance(content, dict):
            return content
        return json.loads(content)
    except RuntimeError:
        raise
    except json.JSONDecodeError as exc:
        choice = response.get("choices", [{}])[0]
        usage = response.get("usage", {})
        raise RuntimeError(
            f"model returned invalid/incomplete JSON at character {exc.pos} "
            f"(finish_reason={choice.get('finish_reason')!r}, "
            f"completion_tokens={usage.get('completion_tokens')!r}); "
            "increase --max-tokens and retry this commit"
        ) from exc
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected LM Studio response: {json.dumps(response)[:2000]}") from exc


def clean_symbol(value: Any, allowed: set[str]) -> str:
    symbol = str(value).strip().strip("`'\"")
    if symbol in allowed:
        return symbol
    # Models sometimes use the C preprocessor spelling CONFIG_FOO for a Kconfig
    # declaration named FOO. Preserve literal historical declarations that were
    # themselves named CONFIG_FOO by checking the allowed set before stripping.
    stripped = symbol[7:] if symbol.startswith("CONFIG_") else symbol
    return stripped if stripped in allowed else symbol


def deterministic_bulk_prefix_result(
    old_only: list[str], new_only: list[str]
) -> dict[str, Any] | None:
    """Expand an exact, repository-wide CONFIG_ prefix removal without huge LLM output."""
    if len(old_only) < 10 or len(old_only) != len(new_only):
        return None
    expected = {old.removeprefix("CONFIG_") for old in old_only if old.startswith("CONFIG_")}
    if len(expected) != len(old_only) or expected != set(new_only):
        return None
    count = len(old_only)
    rationale = (
        f"Exact bijection across all {count} changed Kconfig declarations: every parent symbol "
        "starts with CONFIG_ and the corresponding commit symbol is identical after removing "
        "that prefix. This is the systematic-prefix example of feature unification."
    )
    candidates = []
    for old in old_only:
        new = old.removeprefix("CONFIG_")
        candidates.append({
            "kind": "rename",
            "old_features": [old],
            "new_features": [new],
            "confidence": "very_likely",
            "confidence_score": 1.0,
            "rationale": rationale,
            "evidence": [f"declaration mapping: {old} -> {new}"],
            "manual_check": (
                "Confirm that this commit intentionally changes the Kconfig symbol naming "
                "convention and that the compared feature-model extractor retains these names."
            ),
        })
    return {
        "summary": f"Detected and expanded a systematic CONFIG_ prefix removal for {count} features.",
        "candidates": candidates,
    }


def validate_result(result: dict[str, Any], old_only: list[str], new_only: list[str]) -> tuple[dict[str, Any], list[str]]:
    valid: list[dict[str, Any]] = []
    rejected: list[str] = []
    old_set, new_set = set(old_only), set(new_only)
    for index, raw in enumerate(result.get("candidates", [])):
        old = list(dict.fromkeys(clean_symbol(x, old_set) for x in raw.get("old_features", [])))
        new = list(dict.fromkeys(clean_symbol(x, new_set) for x in raw.get("new_features", [])))
        kind = raw.get("kind")
        expected = (
            "rename" if len(old) == len(new) == 1 else
            "split" if len(old) == 1 and len(new) >= 2 else
            "merge" if len(old) >= 2 and len(new) == 1 else None
        )
        problems: list[str] = []
        if expected != kind:
            problems.append(f"cardinality implies {expected!r}, not {kind!r}")
        if not set(old) <= old_set:
            problems.append(f"non-exclusive/unknown old features {sorted(set(old) - old_set)}")
        if not set(new) <= new_set:
            problems.append(f"non-exclusive/unknown new features {sorted(set(new) - new_set)}")
        if raw.get("confidence") not in CONFIDENCES:
            problems.append("invalid confidence")
        try:
            score = float(raw.get("confidence_score"))
            if not 0 <= score <= 1:
                problems.append("confidence_score is outside [0,1]")
        except (TypeError, ValueError):
            problems.append("invalid confidence_score")
            score = 0.0
        if problems:
            rejected.append(f"candidate {index}: {'; '.join(problems)}")
            continue
        item = dict(raw)
        item.update(old_features=old, new_features=new, confidence_score=score)
        valid.append(item)
    return {"summary": str(result.get("summary", "")), "candidates": valid}, rejected


def load_completed(path: Path, retry_errors: bool) -> set[str]:
    latest_status: dict[str, str] = {}
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid JSONL at {path}:{number}: {exc}") from exc
            latest_status[record["commit"]] = record.get("status", "ok")
    return {
        commit for commit, status in latest_status.items()
        if not (retry_errors and status == "error")
    }


def append_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def latest_records(path: Path) -> list[dict[str, Any]]:
    by_commit: dict[str, dict[str, Any]] = {}
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    record = json.loads(line)
                    by_commit[record["commit"]] = record
    return list(by_commit.values())


def export_review(output: Path, records: list[dict[str, Any]]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    csv_path = output / "candidates.csv"
    fields = [
        "commit", "parent", "author_date", "subject", "kind", "old_features",
        "new_features", "clausy_mapping", "confidence", "confidence_score",
        "rationale", "evidence", "manual_check",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            for candidate in record.get("result", {}).get("candidates", []):
                mapping = f"{','.join(candidate['old_features'])}={','.join(candidate['new_features'])}"
                writer.writerow({
                    "commit": record["commit"], "parent": record["parent"],
                    "author_date": record.get("author_date", ""), "subject": record.get("subject", ""),
                    "kind": candidate["kind"], "old_features": ",".join(candidate["old_features"]),
                    "new_features": ",".join(candidate["new_features"]), "clausy_mapping": mapping,
                    "confidence": candidate["confidence"], "confidence_score": candidate["confidence_score"],
                    "rationale": candidate.get("rationale", ""),
                    "evidence": " | ".join(candidate.get("evidence", [])),
                    "manual_check": candidate.get("manual_check", ""),
                })

    maps_root = output / "clausy-maps"
    for confidence in CONFIDENCES:
        directory = maps_root / confidence
        directory.mkdir(parents=True, exist_ok=True)
        for old in directory.glob("*.map"):
            old.unlink()
    for record in records:
        grouped: dict[str, list[dict[str, Any]]] = {x: [] for x in CONFIDENCES}
        for candidate in record.get("result", {}).get("candidates", []):
            grouped[candidate["confidence"]].append(candidate)
        for confidence, candidates in grouped.items():
            if not candidates:
                continue
            target = maps_root / confidence / f"{record['commit']}.map"
            lines = [
                f"# parent {record['parent']}", f"# commit {record['commit']}",
                f"# {record.get('subject', '')}", f"# LM confidence: {confidence}; REVIEW BEFORE USE",
            ]
            for candidate in candidates:
                lines.append(f"# {candidate['kind']}: {candidate.get('rationale', '')}")
                lines.append(f"{','.join(candidate['old_features'])}={','.join(candidate['new_features'])}")
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path(__file__).parent / "torte-busybox")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "lmstudio-feature-mappings")
    parser.add_argument("--revision", default="HEAD", help="revision whose reachable commits are scanned")
    parser.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--model", help="LM Studio model id; defaults to the first loaded model")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds per LM Studio request")
    parser.add_argument("--max-tokens", type=int, default=8000, help="maximum response tokens per commit")
    parser.add_argument("--max-diff-chars", type=int, default=60000, help="maximum prompt evidence characters")
    parser.add_argument(
        "--reasoning-effort", choices=("default", "none", "low", "medium", "high"), default="none",
        help="LM Studio reasoning mode; 'none' disables Qwen thinking so it emits the required JSON",
    )
    parser.add_argument("--limit", type=int, help="analyze at most this many not-yet-completed commits")
    parser.add_argument("--retry-errors", action="store_true", help="retry commits whose latest record is an error")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--delay", type=float, default=0.0, help="seconds between requests")
    parser.add_argument("--export-only", action="store_true", help="rebuild CSV/map exports without LM requests")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo.resolve()
    output = args.output.resolve()
    results_path = output / "results.jsonl"
    if args.export_only:
        export_review(output, latest_records(results_path))
        print(f"Rebuilt exports under {output}")
        return 0
    if not (repo / ".git").exists():
        raise RuntimeError(f"not a Git repository: {repo}")

    base_url = args.base_url.rstrip("/")
    model = choose_model(base_url, args.model, args.timeout)
    runtime = lmstudio_runtime_info(base_url, model, args.timeout)
    if runtime and runtime.get("loaded") is False:
        raise RuntimeError(
            f"LM Studio model '{model}' is downloaded but not loaded; load the intended "
            "quantization first so results cannot silently mix model variants"
        )
    effective_max_diff_chars = args.max_diff_chars
    if runtime and runtime.get("context_length"):
        context_length = int(runtime["context_length"])
        # Conservative conversion for source code plus room for the system prompt,
        # JSON schema, and completion. The server remains the final token authority.
        safe_chars = max(1000, int((context_length - args.max_tokens - 2500) * 2.5))
        if effective_max_diff_chars > safe_chars:
            effective_max_diff_chars = safe_chars
            advice = (
                " Reload with at least 32768 context for fuller evidence."
                if context_length < 32768 else
                " This reserves room for the configured completion allowance."
            )
            print(
                f"WARNING: loaded context is {context_length} tokens; limiting patch evidence "
                f"to about {safe_chars} characters.{advice}",
                file=sys.stderr,
            )
    commits = get_commits(repo, args.revision)
    completed = load_completed(results_path, args.retry_errors)
    pending = [commit for commit in commits if commit not in completed]
    if args.limit is not None:
        pending = pending[:args.limit]
    model_line = f"Model: {model}"
    if runtime:
        model_line += (
            f" ({runtime.get('variant') or runtime.get('quantization') or 'unknown variant'}, "
            f"context {runtime.get('context_length') or 'unknown'})"
        )
    print(f"{model_line}\nCommits: {len(commits)} total, {len(pending)} pending\nOutput: {output}")

    for position, commit in enumerate(pending, 1):
        metadata = commit_metadata(repo, commit)
        parents = metadata["parents"].split()
        parent = parents[0] if parents else EMPTY_TREE
        print(f"[{position}/{len(pending)}] {commit[:12]} {metadata['subject']}", flush=True)
        started = time.monotonic()
        try:
            evidence, old_only, new_only, truncated = make_evidence(
                repo, commit, parent, metadata, effective_max_diff_chars
            )
            result = deterministic_bulk_prefix_result(old_only, new_only)
            analysis_method = "deterministic_bulk_prefix_rename" if result is not None else "lmstudio"
            if result is None:
                raw = analyze(
                    base_url, model, evidence, args.timeout, args.max_tokens, args.reasoning_effort
                )
                result, rejected = validate_result(raw, old_only, new_only)
            else:
                rejected = []
            record: dict[str, Any] = {
                "schema_version": 1, "status": "ok", "commit": commit,
                "parent": None if parent == EMPTY_TREE else parent,
                "all_parents": parents, "author_date": metadata["author_date"],
                "subject": metadata["subject"], "old_only_features": old_only,
                "new_only_features": new_only, "evidence_truncated": truncated,
                "model": model, "model_runtime": runtime, "analysis_method": analysis_method,
                "result": result,
                "rejected_candidates": rejected,
                "duration_seconds": round(time.monotonic() - started, 3),
                "analyzed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            append_record(results_path, record)
            print(f"  {len(result['candidates'])} candidate(s), {len(rejected)} rejected")
        except Exception as exc:  # Persist failures so a multi-hour scan remains resumable.
            record = {
                "schema_version": 1, "status": "error", "commit": commit,
                "parent": None if parent == EMPTY_TREE else parent,
                "all_parents": parents, "author_date": metadata["author_date"],
                "subject": metadata["subject"], "model": model, "model_runtime": runtime,
                "error": str(exc), "duration_seconds": round(time.monotonic() - started, 3),
                "analyzed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            append_record(results_path, record)
            print(f"  ERROR: {exc}", file=sys.stderr)
            if args.stop_on_error:
                export_review(output, latest_records(results_path))
                return 1
        if args.delay and position < len(pending):
            time.sleep(args.delay)

    export_review(output, latest_records(results_path))
    print(f"Done. Review {output / 'candidates.csv'} and {output / 'clausy-maps'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted; completed commits are saved and will be skipped next run.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
