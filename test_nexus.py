# 2026/10/23 00:26:16 by liaoweipeng1
"""新来源的参数、来源归并、云端限额与历史降级恢复验证。"""
from datetime import datetime, timezone, timedelta
import copy
import json
import subprocess
import unittest
from unittest.mock import patch

from channel_catalog import organize
from health_tracking import line_id, rank_entries, record_results
from nexus_sources import align_names, parse_nexus
import scan_live
import update


class NexusTests(unittest.TestCase):
    def entry(self, url="https://example.com/1"):
        return {"name": "Test", "group": "美国", "source": "us", "url": url,
                "attrs": {"tvg-id": "Test.us@East"}, "directives": [], "origins": ["baseline"]}

    def test_import_preserves_feeds_headers_and_does_not_trust_health(self):
        streams = [{"url": "https://a/" + feed, "feed": feed, "user_agent": "Player",
                    "referrer": "https://a/", "health": {"status": "offline"}} for feed in ("East", "West")]
        entries = parse_nexus(json.dumps([{"id": "Test.us", "name": "Test", "streams": streams}]))
        for entry in entries:
            entry.update(source="nexus-us", region="us")
        result = organize(entries, {"group_order": ["美国"]})
        self.assertEqual(len(result), 2)
        self.assertEqual(len({e["name"] for e in result}), 2)
        self.assertTrue(all(e["group"] == "美国" for e in result))
        self.assertIn("#EXTVLCOPT:http-user-agent=Player", result[0]["directives"])
        self.assertIn("Referer: https://a/\r\n", scan_live.playback_headers(result[0]))
        streams[0]["referrer"] = "https://a/\nInjected: value"
        with self.assertRaises(ValueError):
            parse_nexus(json.dumps([{"id": "Test.us", "name": "Test", "streams": streams}]))

    def test_duplicate_merges_provenance_not_different_headers(self):
        original = self.entry()
        nexus = copy.deepcopy(original)
        nexus.update(source="nexus-us", region="us", name="Other spelling", origins=["nexus"])
        entries = [original, nexus]
        align_names(entries)
        merged = update.deduplicate(entries)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["origins"], ["baseline", "nexus"])
        variant = dict(nexus, directives=["#EXTVLCOPT:http-user-agent=Different"])
        self.assertEqual(len(update.deduplicate([original, variant])), 2)

    def test_history_recovery_blocking_expiration_and_bounded_size(self):
        a, b = self.entry(), self.entry("https://example.com/2")
        history = {"version": 1, "lines": {}}
        now = datetime.now(timezone.utc)
        def record(status):
            record_results(history, [{"id": line_id(a), "status": status}], now.isoformat())
        record("failed")
        self.assertEqual(rank_entries([a, b], history, now), [a, b])
        record("failed")
        self.assertEqual(rank_entries([a, b], history, now), [b, a])
        before = copy.deepcopy(history["lines"][line_id(a)])
        record("blocked")
        record("skipped")
        self.assertEqual(history["lines"][line_id(a)]["score"], before["score"])
        self.assertEqual(history["lines"][line_id(a)]["failures"], 2)
        record("decoded")
        self.assertEqual(rank_entries([b, a], history, now), [a, b])
        self.assertEqual(history["lines"][line_id(a)]["failures"], 0)
        self.assertEqual(rank_entries([b, a], history, now + timedelta(days=22)), [b, a])
        for _ in range(40):
            record("decoded")
        self.assertEqual(len(history["lines"][line_id(a)]["observations"]), 30)

    def test_retry_budget_and_recovery_count_one_observation(self):
        entries = [self.entry(f"https://example.com/{i}") for i in range(120)]
        calls = {}
        def fake_probe(entry, ffmpeg):
            key = line_id(entry)
            calls[key] = calls.get(key, 0) + 1
            return {"id": key, "status": "failed" if calls[key] == 1 else "decoded"}
        with patch.object(scan_live, "probe", side_effect=fake_probe):
            results = scan_live.scan_entries(entries, "ffmpeg")
        self.assertEqual(sum(calls.values()), 160)
        self.assertEqual(sum(r["status"] == "decoded" for r in results), 40)
        history = {"version": 1, "lines": {}}
        record_results(history, results, datetime.now(timezone.utc).isoformat())
        self.assertTrue(all(len(item["observations"]) == 1 for item in history["lines"].values()))

    def test_access_restrictions_are_not_failed_streams(self):
        for code in (401, 403, 429, 451):
            completed = subprocess.CompletedProcess([], 1, b"", f"Server returned {code} Forbidden".encode())
            with patch.object(scan_live.subprocess, "run", return_value=completed):
                self.assertEqual(scan_live.probe(self.entry(), "ffmpeg")["status"], "blocked")

    def test_exclusions_apply_to_renamed_reimported_urls(self):
        old = self.entry()
        renamed = dict(old, name="新别名", source="nexus-us")
        good = self.entry("https://example.com/good")
        rules = [{"name": "旧名称", "url": old["url"]}]
        self.assertEqual(update.exclude_entries([old, renamed, good], rules), [good])

    def test_ranking_does_not_drop_lines_or_mix_channels(self):
        a = self.entry()
        b = dict(self.entry("https://example.com/b"), name="Other")
        c = self.entry("https://example.com/c")
        now = datetime.now(timezone.utc)
        history = {"version": 1, "lines": {}}
        record_results(history, [{"id": line_id(c), "status": "decoded"}], now.isoformat())
        self.assertEqual(rank_entries([a, c, b], history, now), [c, a, b])


if __name__ == "__main__":
    unittest.main()
