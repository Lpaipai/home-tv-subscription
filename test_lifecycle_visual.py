"""复检停用及 OCR 视觉判断回归。无真实视频流、无外部网络或 OCR 依赖。"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import update

import auto_additions
import auto_maintenance
import candidate_scan
import visual_check
from health_tracking import line_id


UTC = timezone.utc


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 20, 12, tzinfo=UTC)
        entry = {"name": "CCTV-1 综合", "group": "央视", "region": "cn",
                 "url": "https://example.com/live-1.m3u8", "directives": [],
                 "attrs": {}, "admission": "video_decoded", "state": "active",
                 "accepted_at": (self.now - timedelta(days=15)).isoformat(),
                 "last_decoded_at": (self.now-timedelta(days=15)).isoformat(),
                 "failure_days": []}
        self.key = line_id(entry)
        self.entry = entry
        self.ledger = {"version": 1, "entries": {self.key: entry}}

    def result(self, status):
        return [{"id": self.key, "name": "CCTV-1 综合", "status": status}]

    def test_due_does_not_delete_untested_stale_stream(self):
        selected = auto_maintenance.due(self.ledger["entries"].values(), self.now)
        self.assertEqual(len(selected), 1)
        # 14天并不会删除；无两次跨日失败，不得自动停用。
        self.assertEqual(auto_maintenance.state_at(self.entry, self.now), "due")
        self.assertEqual(len(auto_additions.active_additions(self.ledger, {})), 1)

    def test_failures_from_two_distinct_days_and_expired_success_suspend(self):
        first = self.now
        auto_maintenance.apply_observations(self.ledger, self.result("failed"), first)
        self.assertEqual(self.entry["state"], "probation")
        auto_maintenance.apply_observations(self.ledger, self.result("failed"), first)
        self.assertNotEqual(self.entry["state"], "suspended")
        auto_maintenance.apply_observations(self.ledger, self.result("failed"),
                                            first + timedelta(days=2))
        self.assertEqual(self.entry["state"], "suspended")
        self.assertEqual(len(auto_additions.active_additions(self.ledger, {})), 0)
        self.assertEqual(auto_maintenance.due(self.ledger["entries"].values(),
                                             first + timedelta(days=3)), [])
        self.assertEqual(len(auto_maintenance.due(self.ledger["entries"].values(),
                                                 first + timedelta(days=10))), 1)

    def test_blocked_and_skipped_do_not_suspend(self):
        auto_maintenance.apply_observations(self.ledger, self.result("blocked"), self.now)
        auto_maintenance.apply_observations(self.ledger, self.result("skipped"),
                                            self.now + timedelta(days=3))
        self.assertNotEqual(self.entry["state"], "suspended")
        self.assertEqual(self.entry["failure_days"], [])
        self.assertEqual(len(auto_additions.active_additions(self.ledger, {})), 1)

    def test_success_restores_suspended_and_resets_failures(self):
        self.entry["state"] = "suspended"
        self.entry["failure_days"] = ["2026-11-10", "2026-11-12"]
        auto_maintenance.apply_observations(self.ledger, self.result("decoded"), self.now)
        self.assertEqual(self.entry["state"], "active")
        self.assertEqual(self.entry["failure_days"], [])
        self.assertEqual(self.entry["last_decoded_at"], self.now.isoformat(timespec="seconds"))
        self.assertEqual(len(auto_additions.active_additions(self.ledger, {})), 1)

    def test_recent_formal_decode_renews_and_prevents_wrong_expiry(self):
        recent = (self.now - timedelta(days=1)).isoformat()
        history = {"lines": {self.key: {"observations": [
            {"at": recent, "status": "decoded"}]}}}
        self.assertEqual(auto_maintenance.sync_successful_formal_checks(self.ledger, history), 1)
        self.assertEqual(auto_maintenance.due(self.ledger["entries"].values(), self.now), [])
        self.assertEqual(auto_maintenance.sync_successful_formal_checks(self.ledger, history), 0)

    def test_no_auto_delete_on_expiry_without_actual_probe(self):
        with patch.object(auto_maintenance, "scan_entries", return_value=[]) as spy:
            info = auto_maintenance.recheck(self.ledger, "ffmpeg", self.now, limit=0)
        self.assertEqual(info["sampled"], 0)
        spy.assert_not_called()
        self.assertNotEqual(self.entry["state"], "suspended")


    def test_atomic_rebuild_preserves_origins_and_handles_suspend_resume(self):
        with tempfile.TemporaryDirectory(dir=update.ROOT) as folder:
            root = Path(folder)
            for name in ("config.json", "channel_names.json", "family_selection.json"):
                shutil.copy2(update.ROOT / name, root / name)
            shutil.copytree(update.ROOT / "public", root / "public")
            public = root / "public"
            before_origins = json.loads((public / "line-origins.json").read_text(encoding="utf-8"))
            ledger = auto_additions.load_additions(public)
            self.assertTrue(ledger["entries"])
            key, candidate = next(iter(ledger["entries"].items()))
            original_url = candidate["url"]
            candidate["state"] = "suspended"
            report = {"checked_at": self.now.isoformat(), "sampled": 0,
                      "suspended_total": 1, "results": []}
            with patch.object(update, "ROOT", root):
                auto_maintenance.publish_recheck(report, ledger, [])
                after_origins = json.loads((public / "line-origins.json").read_text(encoding="utf-8"))
                self.assertNotIn(key, after_origins)
                self.assertNotIn(original_url, (public / "live.m3u").read_text(encoding="utf-8"))
                # 除被暂停线路外，其余线路仍可追溯到上游。
                for line, info in before_origins.items():
                    if line == key:
                        continue
                    self.assertEqual(after_origins[line]["sources"], info["sources"])

                candidate["state"] = "active"
                candidate["last_decoded_at"] = self.now.isoformat()
                auto_maintenance.publish_recheck(
                    {"checked_at": self.now.isoformat(), "sampled": 1,
                     "suspended_total": 0, "results": []}, ledger, [])
                self.assertIn(original_url, (public / "live.m3u").read_text(encoding="utf-8"))
                self.assertIn(key, json.loads(
                    (public / "line-origins.json").read_text(encoding="utf-8")))


class VisualTests(unittest.TestCase):
    def test_cctv_number_match_mismatch_and_ambiguity(self):
        self.assertEqual(visual_check.classify("CCTV-14 少儿", ["CCTV-14", "CCTV 14"]), "match")
        self.assertEqual(visual_check.classify("CCTV-14 少儿", ["CCTV-4", "CCTV 4"]), "mismatch")
        self.assertEqual(visual_check.classify("CCTV-5 体育", ["CCTV-5+", "CCTV-5+"]), "unknown")
        self.assertEqual(visual_check.classify("CCTV-5+ 体育赛事", ["CCTV-5", "CCTV-5"]), "unknown")
        self.assertEqual(visual_check.classify("CCTV-1 综合", ["no logo", "CCTV 2"]), "unknown")

    def test_satellite_name_text_and_unverifiable_logo(self):
        self.assertEqual(visual_check.classify("广西卫视", ["广西卫视", "广西卫视"]), "match")
        self.assertEqual(visual_check.classify("广西卫视", ["湖南卫视", "湖南卫视"]), "mismatch")
        self.assertEqual(visual_check.classify("广西卫视", ["logo graphic", ""]), "unknown")
        self.assertEqual(visual_check.classify("BBC", ["CCTV-1", "CCTV-1"]), "unsupported")

    def test_candidate_scan_blocks_confident_mismatch_but_keeps_unknown(self):
        def entry(i, name):
            url = f"https://example.com/{i}.m3u8"
            return {"id": line_id({"url": url, "directives": []}), "url": url,
                    "name": name, "group": "央视", "source_mode": "decode_then_publish",
                    "directives": []}
        entries = [entry(1, "CCTV-1 综合"), entry(2, "CCTV-2 财经")]
        reports = [{"id": e["id"], "name": e["name"], "group": e["group"],
                    "status": "decoded"} for e in entries]
        with patch.object(candidate_scan, "scan_entries", return_value=reports), \
                patch.object(candidate_scan, "check_visual", side_effect=[
                    {"visual_status": "mismatch", "frame_count": 2},
                    {"visual_status": "unknown", "frame_count": 2}]):
            result = candidate_scan.run_scan(entries, {"version": 1, "lines": {}},
                                             "ffmpeg", 2, 0, self.now())
        self.assertEqual(result["visual_checked"], 2)
        self.assertEqual(result["eligible_for_auto_publish"], 1)
        self.assertEqual(result["results"][0]["visual_status"], "mismatch")
        self.assertEqual(result["results"][1]["visual_status"], "unknown")

    @staticmethod
    def now():
        return datetime(2026, 10, 23, tzinfo=UTC)

    def test_visual_budget_prefers_cctv_over_graphic_satellites(self):
        names = ["四川卫视", "湖南卫视", "CCTV-12", "CCTV-14"]
        entries = []
        for i, name in enumerate(names):
            url = f"https://example.com/{i}"
            entries.append({"id": line_id({"url": url, "directives": []}),
                            "name": name, "url": url, "group": "央视",
                            "source_mode": "decode_then_publish", "directives": []})
        def fake_scan(selected, ffmpeg, retry_limit):
            return [{"id": e["id"], "name": e["name"], "group": e["group"],
                     "status": "decoded"} for e in selected]
        calls = []
        def visual(entry, ffmpeg):
            calls.append(entry["name"])
            return {"visual_status": "unknown", "frame_count": 2}
        with patch.object(candidate_scan, "scan_entries", side_effect=fake_scan), \
                patch.object(candidate_scan, "check_visual", side_effect=visual):
            out = candidate_scan.run_scan(entries, {"version": 1, "lines": {}},
                                          "ffmpeg", limit=4, retry_limit=0,
                                          visual_limit=2, now=self.now())
        self.assertEqual(set(calls), {"CCTV-12", "CCTV-14"})
        self.assertEqual(out["visual_checked"], 2)
        statuses = {r["name"]: r["visual_status"] for r in out["results"]}
        self.assertEqual(statuses["四川卫视"], "not_checked_budget")
        self.assertEqual(statuses["湖南卫视"], "not_checked_budget")

    def test_missing_ocr_never_pretends_to_verify(self):
        with patch.object(visual_check.shutil, "which", return_value=None):
            r = visual_check.check({"name": "CCTV-1 综合"}, "ffmpeg")
        self.assertEqual(r["visual_status"], "tool_missing")

    def test_select_additions_does_not_publish_mismatch(self):
        e = {"name": "CCTV-14", "url": "https://example.com/cctv14.m3u8",
             "source_id": "guovin", "source_url": "https://example.com/list",
             "source_mode": "decode_then_publish", "group": "央视",
             "directives": [], "attrs": {}}
        e["id"] = line_id(e)
        rep = {"checked_at": self.now().isoformat(),
               "results": [{"id": e["id"], "status": "decoded", "visual_status": "mismatch"}]}
        self.assertEqual(auto_additions.select_additions([e], rep, {"entries": {}}, [], {}), [])
        rep["results"][0]["visual_status"] = "unknown"
        self.assertEqual(len(auto_additions.select_additions([e], rep,
                                                            {"entries": {}}, [], {})), 1)


if __name__ == "__main__":
    unittest.main()
