# BusyBox feature-mapping review UI

This local-only UI groups the latest successful LM Studio candidates into commits. Each of the 39 affected commits receives one human decision and note, while all mappings, rationales, manual-check prompts, confidence levels, concrete focused Git diff, and affected BusyBox release comparisons remain visible. Review decisions are stored separately; the scan's `results.jsonl`, CSV, and confidence maps are never modified.

Run it:

```sh
python3 busybox-mapping-review-ui/server.py
```

Then use <http://127.0.0.1:8765>. The server normally opens it automatically. Useful keys when the focus is outside an input are `J`/`K` (next/previous), `A` (accept), `R` (reject), `U` (uncertain), and `P` (pending).

Accepting or rejecting a commit applies that decision to all mapping candidates reported for the commit. This makes the 522-rule systematic prefix migration a single review item rather than 522 separate items.

Reviews are saved atomically to `busybox-mapping-review-ui/reviews.json`. “Export accepted maps” creates one portable archive outside the Torte checkout:

```text
/busybox-feature-mappings.zip
```

The archive contains `reviewed-mappings/release-pairs.csv`, extractor-specific map files, metadata, a complete `review-decisions.json` snapshot, a portable `mapping-review.csv`, and an audit file for accepted mappings that an extractor cannot resolve. The CSV has one row per candidate and includes its symbols, mapping, confidence, release applicability by extractor, decision, exclusion status, rationale, and complete review note. Non-empty review notes are also included as comments immediately above their mapping rules. A rule is included only when all mapping endpoints occur exclusively on the correct sides of that extractor's actual `.model` formulas. Unconstrained symbols dropped by KClause therefore remain documented but are not passed to Clausy. The Torte experiment downloads the ZIP through `download-payload-file` and unpacks only the generated payload copy.

Validate the configured input paths and dataset without starting a server:

```sh
python3 busybox-mapping-review-ui/server.py --check
```

All paths, the export archive, and the listen address can be overridden; run `python3 busybox-mapping-review-ui/server.py --help` for details. The HTTP server binds to `127.0.0.1` by default and uses only Python's standard library.
