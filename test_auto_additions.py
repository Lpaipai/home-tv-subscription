# 2026/10/23 14:23:05 by liaoweipeng1
"""验证解码通过收录、排除优先、跨日保留及失败回滚。"""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import auto_additions
import candidate_scan
from health_tracking import line_id
import source_review
import update


class AutoAdditionTests(unittest.TestCase):
    def entry(self, number, **overrides):
        entry = {"name": "CCTV-1", "group": "央视", "url": f"https://example.com/new-{number}",
                 "attrs": {"tvg-id": "CCTV1.cn@SD"}, "directives": [],
                 "source_id": "fixture", "source_url": "https://example.com/list.m3u",
                 "source_mode": "decode_then_publish", "region": "cn"}
        entry.update(overrides)
        entry["id"] = line_id(entry)
        return entry

    def report(self, entries, statuses=None):
        statuses = statuses or ["decoded"] * len(entries)
        return {"checked_at": datetime.now(timezone.utc).isoformat(),
                "results": [{"id": line_id(entry), "status": status}
                            for entry, status in zip(entries, statuses)]}

    def fixture(self, root):
        for name in ("config.json", "channel_names.json", "family_selection.json",
                     "candidate_sources.json"):
            shutil.copy2(update.ROOT / name, root / name)
        shutil.copytree(update.ROOT / "public", root / "public")

    def test_only_current_decoded_enabled_unique_nonexcluded_lines_are_added(self):
        entries = [self.entry(i) for i in range(7)]
        entries[4]["source_mode"] = "review_only"
        config = {"live_exclusions": [{"url": entries[5]["url"], "name": "另一个名字"}]}
        ledger = {"version": 1, "entries": {}}
        report = self.report(entries, ["decoded", "failed", "blocked", "skipped",
                                       "decoded", "decoded", "decoded"])
        added = auto_additions.select_additions(entries, report, ledger, [entries[6]], config)
        self.assertEqual([line_id(e) for e in added], [line_id(entries[0])])
        ledger["entries"][line_id(added[0])] = added[0]
        self.assertEqual(auto_additions.select_additions(entries, report, ledger, [entries[6]], config), [])
        self.assertFalse(added[0]["content_verified"])
        self.assertEqual(added[0]["origins"], [entries[0]["source_url"]])

    def test_retry_success_is_eligible_but_http_only_failure_is_not(self):
        entries = [self.entry(1), self.entry(2)]
        calls = {}

        def probe(entry, ffmpeg):
            key = line_id(entry)
            calls[key] = calls.get(key, 0) + 1
            return {"id": key, "name": entry["name"], "group": entry["group"],
                    "status": "decoded" if entry == entries[0] and calls[key] == 2 else "failed",
                    "reason": "fixture"}

        with patch("scan_live.probe", side_effect=probe):
            report = candidate_scan.run_scan(entries, {"version": 1, "lines": {}}, "ffmpeg", limit=2, visual_limit=0)
        self.assertEqual(report["attempts"], 4)
        self.assertEqual(report["eligible_for_auto_publish"], 1)

    def test_discovery_keeps_metadata_and_excludes_blacklisted_urls(self):
        good = self.entry(1, directives=["#EXTVLCOPT:http-user-agent=TV"])
        bad = self.entry(2)
        source = {"id": "fixture", "name": "fixture", "url": good["source_url"],
                  "mode": "decode_then_publish", "type": "community", "rights": "unverified",
                  "homepage": "https://example.com"}
        report, entries = source_review.summarize_candidates(
            {"sources": [source]}, [], lambda *_: ([good, bad], {}), with_entries=True,
            excluded_urls={bad["url"]})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["attrs"], good["attrs"])
        self.assertEqual(entries[0]["directives"], good["directives"])
        self.assertEqual(entries[0]["source_mode"], "decode_then_publish")

    def test_mixed_catalog_channel_inherits_existing_region(self):
        settings = {"group_order": ["地方", "香港", "台湾"], "aliases": {}}
        new = self.entry(1, name="翡翠台", source="candidate-fixture")
        existing = dict(new, group="香港")
        aligned = auto_additions.align_regions([new], [existing], settings)
        self.assertEqual(aligned[0]["region"], "hk")
        self.assertEqual(new["region"], "cn")
        self.assertEqual(auto_additions.organize(aligned, settings)[0]["group"], "香港")
        ambiguous = auto_additions.align_regions([new], [existing, dict(existing, group="台湾")], settings)
        self.assertEqual(ambiguous[0]["region"], "cn")

    def test_publish_preserves_headers_vod_and_survives_next_update(self):
        with tempfile.TemporaryDirectory(dir=update.ROOT) as folder:
            root = Path(folder)
            self.fixture(root)
            public = root / "public"
            original = update.parse_live((public / "live.m3u").read_text(encoding="utf-8"))
            original_counts = Counter((e["group"], e["name"], line_id(e)) for e in original
                                      if e["group"] != "家庭精选")
            preserved = {p.relative_to(public): p.read_bytes() for p in (public / "vod").glob("*")}
            preserved[Path("subscription.json")] = (public / "subscription.json").read_bytes()
            entries = [self.entry(1), self.entry(2, directives=["#EXTVLCOPT:http-user-agent=TV"])]
            report = self.report(entries)
            with patch.object(update, "ROOT", root), patch.object(update, "STATE", root / "state"):
                published = auto_additions.publish_scan(entries, report, {"version": 1, "lines": {}})
                self.assertEqual(published["promoted"], 2)
                self.assertTrue(all(line_id(e) in auto_additions.load_additions(public)["entries"] for e in entries))
                m3u = update.parse_live((public / "live.m3u").read_text(encoding="utf-8"))
                published_counts = Counter((e["group"], e["name"], line_id(e)) for e in m3u
                                           if e["group"] != "家庭精选")
                self.assertFalse(original_counts - published_counts)
                self.assertTrue(all(line_id(e) in {line_id(x) for x in m3u} for e in entries))
                txt = (public / "live.txt").read_text(encoding="utf-8")
                self.assertIn(entries[0]["url"], txt)
                self.assertNotIn(entries[1]["url"], txt)
                self.assertTrue(any(e["name"] == "CCTV-1 综合" and e["url"] == entries[0]["url"] for e in m3u))
                origins = json.loads((public / "line-origins.json").read_text(encoding="utf-8"))
                self.assertIn(entries[0]["source_url"], origins[line_id(entries[0])]["sources"])
                repeated = auto_additions.publish_scan(entries, report, {"version": 1, "lines": {}})
                self.assertEqual(repeated["promoted"], 0)

                def fetch(url, validator):
                    if "dearbulut.github.io" in url:
                        return validator(json.dumps([{"id": "Test.us", "name": "Test", "streams": [
                            {"url": "https://example.com/baseline"}]}])), {"cached": False}
                    return validator("#EXTM3U\n#EXTINF:-1,Baseline\nhttps://example.com/baseline\n"), {"cached": False}

                with patch.object(update, "fetch", side_effect=fetch), \
                        patch.object(sys, "argv", ["update.py", "--live-only"]):
                    update.main()
                next_entries = update.parse_live((public / "live.m3u").read_text(encoding="utf-8"))
                self.assertTrue(all(line_id(e) in {line_id(x) for x in next_entries} for e in entries))
                # 用户之后反馈错台：旧收录记录仍可审计，但不会重新进入下一轮订阅。
                config = json.loads((root / "config.json").read_text(encoding="utf-8"))
                config["live_exclusions"].append({"name": "改名后", "url": entries[0]["url"]})
                update.write_json(root / "config.json", config)
                with patch.object(update, "fetch", side_effect=fetch), \
                        patch.object(sys, "argv", ["update.py", "--live-only"]):
                    update.main()
                self.assertNotIn(entries[0]["url"], (public / "live.m3u").read_text(encoding="utf-8"))
            for relative, content in preserved.items():
                self.assertEqual((public / relative).read_bytes(), content)

    def test_render_failure_cannot_partially_publish_ledger_or_reports(self):
        with tempfile.TemporaryDirectory(dir=update.ROOT) as folder:
            root = Path(folder)
            self.fixture(root)
            public = root / "public"
            before = {p.relative_to(public): p.read_bytes() for p in public.rglob("*") if p.is_file()}
            entry = self.entry(1)
            with patch.object(update, "ROOT", root), patch.object(update, "render_live", side_effect=RuntimeError("fixture")):
                with self.assertRaises(RuntimeError):
                    auto_additions.publish_scan([entry], self.report([entry]), {"version": 1, "lines": {}})
            after = {p.relative_to(public): p.read_bytes() for p in public.rglob("*") if p.is_file()}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
