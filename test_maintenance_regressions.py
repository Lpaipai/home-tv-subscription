# 2026/10/23 17:50:26 by liaoweipeng1
"""跨轮错台拦截和长期复检公平性；不访问网络或调用真实 OCR。"""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import auto_additions
import auto_maintenance
import candidate_scan
from health_tracking import line_id
import update


NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def candidate(index):
    entry = {"name": "CCTV-14", "url": f"https://example.com/{index}.m3u8",
             "group": "央视", "directives": [], "attrs": {},
             "source_mode": "decode_then_publish", "source_id": "test",
             "source_url": "https://example.com/list"}
    entry["id"] = line_id(entry)
    return entry


class VisualPersistenceTests(unittest.TestCase):
    def scan(self, entries, history, visual_status, budget=3):
        def decode(selected, *args, **kwargs):
            return [{"id": entry["id"], "name": entry["name"],
                     "group": entry["group"], "status": "decoded"}
                    for entry in selected]

        with patch.object(candidate_scan, "scan_entries", side_effect=decode), \
                patch.object(candidate_scan, "check_visual", return_value={
                    "visual_status": visual_status, "frame_count": 2}):
            return candidate_scan.run_scan(entries, history, "ffmpeg", now=NOW,
                                           visual_limit=budget)

    def test_mismatch_survives_restart_eviction_and_only_match_releases(self):
        entry = candidate("held")
        # 刻意让普通健康历史被 2400 条较新记录挤出，错台拦截仍必须保留。
        history = {"version": 1, "lines": {
            str(i): {"observations": [{"at": "2027-01-01T00:00:00+00:00"}]}
            for i in range(2401)}}
        report = self.scan([entry], history, "mismatch")
        self.assertNotIn(entry["id"], history["lines"])
        self.assertEqual(report["eligible_for_auto_publish"], 0)
        with tempfile.TemporaryDirectory(dir=update.ROOT) as folder:
            root = Path(folder)
            for name in ("config.json", "channel_names.json", "family_selection.json",
                     "candidate_sources.json"):
                shutil.copy2(update.ROOT / name, root / name)
            shutil.copytree(update.ROOT / "public", root / "public")
            public = root / "public"
            with patch.object(update, "ROOT", root):
                result = auto_additions.publish_scan([entry], report, history)
                self.assertEqual(result["promoted"], 0)
                for status in ("not_checked_budget", "unknown", "tool_missing",
                               "unsupported", "ocr_unavailable", "capture_timeout"):
                    history = json.loads((public / "candidate-health-history.json").read_text(
                        encoding="utf-8"))
                    report = self.scan([entry], history, status,
                                       budget=0 if status == "not_checked_budget" else 3)
                    self.assertTrue(report["results"][0]["visual_blocked"])
                    result = auto_additions.publish_scan([entry], report, history)
                    self.assertEqual(result["promoted"], 0)
                    self.assertNotIn(entry["url"], (public / "live.m3u").read_text(
                        encoding="utf-8"))
                report = self.scan([entry], history, "match")
                result = auto_additions.publish_scan([entry], report, history)
                self.assertEqual(result["promoted"], 1)
                self.assertEqual(history["visual_holds"], {})
                self.assertIn(entry["url"], (public / "live.m3u").read_text(encoding="utf-8"))

    def test_fresh_unknown_still_auto_admits_and_holds_get_ocr_priority(self):
        entries = [candidate(i) for i in range(4)]
        history = {"version": 1, "lines": {}}
        report = self.scan(entries, history, "tool_missing")
        self.assertEqual(len(auto_additions.select_additions(
            entries, report, {"entries": {}}, [], {})), 4)
        held = entries[-1]
        held["name"] = "广西卫视"
        history["visual_holds"][held["id"]] = {"name": held["name"]}
        report = self.scan(entries, history, "match", budget=1)
        result = next(r for r in report["results"] if r["id"] == held["id"])
        self.assertEqual(result["visual_status"], "match")
        self.assertFalse(result["visual_blocked"])


class RecheckFairnessTests(unittest.TestCase):
    def entries(self, count, state):
        items = []
        for index in range(count):
            entry = candidate(f"{state}-{index}")
            entry.update(state=state, accepted_at=(NOW - timedelta(days=30)).isoformat(),
                         failure_days=[])
            items.append(entry)
        return items

    def test_large_suspended_pool_cannot_starve_live_or_other_suspended_lines(self):
        suspended = self.entries(64, "suspended")
        live = self.entries(16, "active")
        entries = suspended + live
        ledger = {"entries": {line_id(e): e for e in entries}}
        seen = set()
        for day in range(30):
            now = NOW + timedelta(days=day)
            selected = auto_maintenance.due(entries, now)
            self.assertLessEqual(len(selected), 8)
            self.assertEqual(len({line_id(e) for e in selected}), len(selected))
            if day == 0:
                self.assertEqual(sum(e["state"] == "suspended" for e in selected), 4)
            seen.update(line_id(e) for e in selected)
            # 长期 403 不应导致状态转换，更不应垄断轮换名额。
            auto_maintenance.apply_observations(ledger, [
                {"id": line_id(e), "status": "blocked"} for e in selected], now)
        self.assertEqual(seen, set(ledger["entries"]))

    def test_single_pool_borrows_budget_and_old_failures_do_not_starve_others(self):
        entries = self.entries(40, "active")
        # 最老的八条不能在冷却结束后挤掉从未轮到的其余线路。
        for entry in entries[:8]:
            entry["accepted_at"] = (NOW - timedelta(days=100)).isoformat()
        ledger = {"entries": {line_id(e): e for e in entries}}
        seen = set()
        for day in range(5):
            now = NOW + timedelta(days=day)
            selected = auto_maintenance.due(entries, now)
            self.assertEqual(len(selected), 8)
            seen.update(line_id(e) for e in selected)
            auto_maintenance.apply_observations(ledger, [
                {"id": line_id(e), "status": "blocked"} for e in selected], now)
        self.assertEqual(seen, set(ledger["entries"]))

    def test_zero_single_slot_and_cooldown_boundaries(self):
        entries = self.entries(1, "active") + self.entries(1, "suspended")
        self.assertEqual(auto_maintenance.due(entries, NOW, limit=0), [])
        selected = [auto_maintenance.due(copy.deepcopy(entries), NOW + timedelta(days=day),
                                         limit=1)[0]["state"] for day in range(2)]
        self.assertEqual(set(selected), {"active", "suspended"})
        for entry in entries:
            entry["last_attempt_at"] = NOW.isoformat()
        self.assertEqual(auto_maintenance.due(entries, NOW + timedelta(days=1)), [])
        self.assertEqual([e["state"] for e in auto_maintenance.due(
            entries, NOW + timedelta(days=2))], ["active"])
        self.assertEqual(len(auto_maintenance.due(entries, NOW + timedelta(days=7))), 2)


if __name__ == "__main__":
    unittest.main()
