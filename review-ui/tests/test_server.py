import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("review_server", Path(__file__).parents[1] / "server.py")
server = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(server)


class ReviewUiTests(unittest.TestCase):
    def test_release_pair_matching_requires_exclusive_old_and_new_features(self):
        candidate = {"old_features": ["OLD"], "new_features": ["NEW"]}
        pairs = [
            {"left": "1_0", "right": "1_1", "removed": {"OLD"}, "added": {"NEW"}},
            {"left": "1_1", "right": "1_2", "removed": {"OLD"}, "added": {"OTHER"}},
        ]
        self.assertEqual(server.applicable_pairs(candidate, pairs), [{"left": "1_0", "right": "1_1"}])

    def test_reviews_are_written_without_touching_source_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reviews.json"
            store = server.ReviewStore(path)
            store.save("candidate", "accepted", "checked in the diff")
            payload = json.loads(path.read_text())
            self.assertEqual(payload["reviews"]["candidate"]["decision"], "accepted")

    def test_bulk_review_preserves_already_reviewed_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            store = server.ReviewStore(Path(directory) / "reviews.json")
            store.save("first", "rejected", "individual review")
            changed = store.save_many(["first", "second"], "accepted", "bulk review")
            self.assertEqual(changed, 1)
            self.assertEqual(store.get("first")["decision"], "rejected")
            self.assertEqual(store.get("second")["decision"], "accepted")

    def test_bulk_review_preserves_excluded_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reviews.json"
            path.write_text(json.dumps({
                "reviews": {
                    "split": {"decision": "rejected", "note": "invalid split", "excluded": True},
                    "rename": {"decision": "uncertain", "note": "still deciding"},
                }
            }))
            store = server.ReviewStore(path)
            changed = store.save_many(["split", "rename"], "accepted", "commit accepted", only_pending=False)
            self.assertEqual(changed, 1)
            self.assertEqual(store.get("split")["decision"], "rejected")
            self.assertTrue(store.get("split")["excluded"])
            self.assertEqual(store.get("rename")["decision"], "accepted")

    def test_focused_diff_keeps_only_symbol_hunks(self):
        diff = "diff --git a/a b/a\n@@ -1 +1 @@\n-OLD\n+NEW\ndiff --git a/b b/b\n@@ -1 +1 @@\n-x\n+y\n"
        focused = server.focused_diff(diff, ["OLD", "NEW"])
        self.assertIn("a/a", focused)
        self.assertNotIn("a/b", focused)

    def test_commit_diff_focuses_on_all_candidate_symbols(self):
        data = object.__new__(server.ReviewData)
        data.candidates = [
            {"id": "first", "commit": "abc", "parent": "parent", "old_features": ["OLD_A"], "new_features": ["NEW_A"]},
            {"id": "second", "commit": "abc", "parent": "parent", "old_features": ["OLD_B"], "new_features": ["NEW_B"]},
        ]
        data.store = type("Store", (), {"get": lambda self, identifier: {"excluded": False}})()
        captured = {}
        data._git_diff = lambda commit, parent, symbols, full: captured.update(
            commit=commit, parent=parent, symbols=symbols, full=full
        ) or "diff"
        self.assertEqual(data.git_commit_diff("abc"), "diff")
        self.assertEqual(set(captured["symbols"]), {"OLD_A", "NEW_A", "OLD_B", "NEW_B"})


if __name__ == "__main__":
    unittest.main()
