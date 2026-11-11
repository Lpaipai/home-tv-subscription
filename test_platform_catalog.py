# 2026/11/10 22:44:00 by liaoweipeng1
"""平台候选准入、恢复、唯一身份与生成结果的独立回归。"""
import json
from pathlib import Path
import tempfile
import unittest

from health_tracking import line_id
from platform_catalog import normalize, apply_results, select_published, is_platform_entry
from platform_scan import choose


class PlatformTests(unittest.TestCase):
    def entry(self, room="10001", name="虚构测试频道"):
        return normalize({"name": name, "group": "一起看", "attrs": {}, "directives": [],
                          "url": "https://live.ottiptv.cc/huya/" + room})

    def result(self, entry, status="decoded", audio="decoded"):
        return {"id": line_id(entry), "status": status, "video_status": status, "audio_status": audio}

    def test_video_without_audio_cannot_enter_directory(self):
        entry = self.entry()
        ledger = {"version": 1, "entries": {}}
        apply_results(ledger, [entry], [self.result(entry, audio="failed")], "2026-01-01T00:00:00+00:00")
        self.assertEqual({}, ledger["entries"])
        apply_results(ledger, [entry], [self.result(entry)], "2026-01-01T00:00:00+00:00")
        self.assertEqual("active", ledger["entries"][line_id(entry)]["state"])

    def test_two_dates_suspend_and_success_recovers(self):
        entry = self.entry()
        ledger = {"version": 1, "entries": {}}
        apply_results(ledger, [entry], [self.result(entry)], "2026-01-01T00:00:00+00:00")
        failure = self.result(entry, "failed")
        for stamp in ["2026-01-02T00:00:00+00:00", "2026-01-02T01:00:00+00:00"]:
            apply_results(ledger, [entry], [failure], stamp)
        self.assertEqual("active", ledger["entries"][line_id(entry)]["state"])
        apply_results(ledger, [entry], [failure], "2026-01-03T00:00:00+00:00")
        self.assertEqual("suspended", ledger["entries"][line_id(entry)]["state"])
        apply_results(ledger, [entry], [self.result(entry)], "2026-01-04T00:00:00+00:00")
        self.assertEqual("active", ledger["entries"][line_id(entry)]["state"])

    def test_same_name_different_rooms_have_separate_ids(self):
        first, second = self.entry(), self.entry("10002")
        self.assertNotEqual(first["attrs"]["tvg-id"], second["attrs"]["tvg-id"])
        self.assertEqual(line_id(first), line_id(normalize(first)))
        self.assertTrue(is_platform_entry(first))

    def test_original_group_and_headers_survive_discovery(self):
        entry = self.entry()
        entry["original_group"] = "电子榨菜"
        entry["group"] = "其他"
        updated = normalize(entry)
        self.assertEqual("电子榨菜", updated["group"])
        self.assertEqual(["#EXTVLCOPT:http-user-agent=Mozilla/5.0"], updated["directives"])
        self.assertIsNone(normalize(dict(entry, url="http://127.0.0.1/test")))

    def test_manual_exclusion_and_capacity_are_respected(self):
        entry = self.entry()
        ledger = {"version": 1, "entries": {}}
        apply_results(ledger, [entry], [self.result(entry)], "2026-01-01T00:00:00+00:00")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            out = root / "public"
            out.mkdir()
            (root / "platform_selection.json").write_text(json.dumps({"enabled": True, "max_channels": 4}), encoding="utf-8")
            (out / "platform-ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
            self.assertEqual(1, len(select_published(out, {})))
            self.assertEqual([], select_published(out, {"live_exclusions": [{"url": entry["url"]}]}))
            (root / "platform_selection.json").write_text(json.dumps({"enabled": True, "max_channels": 0}), encoding="utf-8")
            with self.assertRaises(ValueError):
                select_published(out, {})

    def test_rotation_prioritizes_never_checked_then_oldest(self):
        first, second = self.entry(), self.entry("10002")
        history = {"lines": {line_id(first): {"checked_at": "2026-01-02T00:00:00+00:00"}}}
        self.assertEqual(line_id(second), line_id(choose([first, second], history, 1)[0]))

    def test_group_aliases_and_allowlist_apply_to_cached_and_new_rooms(self):
        settings = {"included_groups": ["电影·演员", "电影·类型", "电影·综合", "一起看"],
                    "group_aliases": {"电影·动作": "电影·类型", "电影·喜剧": "电影·类型"}}
        action = self.entry(name="虚构动作电影")
        result = normalize(action, settings)
        self.assertEqual("电影·类型", result["group"])
        self.assertEqual(result, normalize(result, settings))
        excluded = self.entry()
        excluded["original_group"] = "电子榨菜"
        self.assertIsNone(normalize(excluded, settings))
        self.assertIsNotNone(normalize(self.entry(), settings))


if __name__ == "__main__":
    unittest.main()
