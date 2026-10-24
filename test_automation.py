# 2026/10/22 23:40:00 by liaoweipeng1
"""自动发布保护、点播保留和有限额抽检的回归测试。"""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import scan_live
import update
import curation
import network_policy
import source_review
import candidate_scan
from datetime import datetime, timezone, timedelta


class AutomationTests(unittest.TestCase):
    def test_guard_rejects_missing_stale_or_shrunken_sources(self):
        old = {"live_entries": 100, "live": [{"id": "cn", "entries": 100}]}
        good = {"live_entries": 100, "live": [{"id": "cn", "entries": 100, "cached": False}]}
        update.validate_live_update(good, old)
        variants = [
            {"live_entries": 100, "live": [{"id": "cn", "error": "offline"}]},
            {"live_entries": 100, "live": [{"id": "cn", "entries": 100, "cached": True}]},
            {"live_entries": 79, "live": good["live"]},
            {"live_entries": 100, "live": [{"id": "cn", "entries": 59, "cached": False}]},
        ]
        for report in variants:
            with self.subTest(report=report), self.assertRaises(RuntimeError):
                update.validate_live_update(report, old)

    def test_live_only_preserves_vod_and_health_without_fetching_vod(self):
        with tempfile.TemporaryDirectory(dir=update.ROOT) as folder:
            root = Path(folder)
            for name in ("config.json", "channel_names.json", "family_selection.json"):
                shutil.copy2(update.ROOT / name, root / name)
            shutil.copytree(update.ROOT / "public", root / "public")
            public = root / "public"
            (public / "health.json").write_text('{"sampled": 1}', encoding="utf-8")
            (public / "health.md").write_text("上次抽检", encoding="utf-8")
            update.write_json(public / "health-history.json", {"version": 1, "lines": {}})
            preserved = [public / "subscription.json", public / "live-config.json",
                         public / "health.json", public / "health.md", *list((public / "vod").glob("*.json"))]
            before = {p.relative_to(public): p.read_bytes() for p in preserved}
            status = json.loads((public / "status.json").read_text(encoding="utf-8"))
            old_vod = copy.deepcopy(status["vod"])
            status.update(live_entries=0, live=[])
            update.write_json(public / "status.json", status)
            sources = json.loads((root / "config.json").read_text(encoding="utf-8"))["live_sources"]
            allowed = {source["url"] for source in sources}

            def fetch(url, validator):
                self.assertIn(url, allowed, "直播更新不能抓取点播配置")
                if "dearbulut.github.io" in url:
                    return validator(json.dumps([{"id": "Test.us", "name": "Test", "streams": [
                        {"url": "https://example.com/live", "feed": "SD"}]}])), {"cached": False}
                return validator("#EXTM3U\n#EXTINF:-1,Test\nhttps://example.com/live\n"), {"cached": False}

            with patch.object(update, "ROOT", root), patch.object(update, "STATE", root / "state"), \
                    patch.object(update, "fetch", side_effect=fetch), \
                    patch.object(sys, "argv", ["update.py", "--live-only", "--guarded"]):
                update.main()
            for relative, data in before.items():
                self.assertEqual((public / relative).read_bytes(), data)
            new_status = json.loads((public / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(new_status["vod"], old_vod)
            self.assertTrue(list((root / "backups").iterdir()))

    def test_failed_directory_swap_restores_previous_public(self):
        with tempfile.TemporaryDirectory(dir=update.ROOT) as folder:
            root = Path(folder)
            (root / "public").mkdir()
            (root / "public" / "marker").write_text("old", encoding="utf-8")
            with patch.object(update, "ROOT", root), self.assertRaises(OSError):
                update.publish_directory(root / "missing-build")
            self.assertEqual((root / "public" / "marker").read_text(encoding="utf-8"), "old")

    def test_scan_rotation_limit_and_deduplication(self):
        entries = [{"name": str(i), "url": f"https://example.com/{i}", "directives": [],
                    "group": "央视" if i < 10 else "美国"} for i in range(50)]
        first = scan_live.select_entries(entries + entries[:2], 20, 1)
        second = scan_live.select_entries(entries, 20, 2)
        self.assertEqual(len(first), 20)
        self.assertEqual(len({scan_live.line_id(e) for e in first}), 20)
        self.assertEqual(sum(e["group"] == "央视" for e in first), 10)
        self.assertNotEqual(first, second)

    def test_probe_requires_decoded_frames_and_preserves_headers(self):
        entry = {"name": "Test", "group": "央视", "url": "https://example.com/live",
                 "directives": ["#EXTVLCOPT:http-referrer=https://example.com/"]}
        completed = subprocess.CompletedProcess([], 0, b"\x80" * (32 * 18 * 4), b"")
        with patch.object(scan_live.subprocess, "run", return_value=completed) as run:
            self.assertEqual(scan_live.probe(entry, "ffmpeg")["status"], "decoded")
            self.assertIn("Referer: https://example.com/\r\n", run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["timeout"], 16)
        completed.stdout = b""
        with patch.object(scan_live.subprocess, "run", return_value=completed):
            self.assertEqual(scan_live.probe(entry, "ffmpeg")["status"], "failed")
        with patch.object(scan_live.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 16)):
            self.assertEqual(scan_live.probe(entry, "ffmpeg")["status"], "failed")
        entry["directives"] = ["#KODIPROP:inputstream.adaptive.license_type=widevine"]
        with patch.object(scan_live.subprocess, "run") as run:
            self.assertEqual(scan_live.probe(entry, "ffmpeg")["status"], "skipped")
            run.assert_not_called()


    def test_daily_sample_prefers_distinct_important_channels_and_rotates(self):
        important = [{"name": f"CCTV-{i}", "url": f"https://example.com/cctv/{i}/{j}",
                      "group": "央视", "directives": []}
                     for i in range(12) for j in range(3)]
        others = [{"name": f"Other-{i}", "url": f"https://example.com/other/{i}",
                   "group": "美国", "directives": []} for i in range(12)]
        first = scan_live.select_entries(important + others, 8, 100, important_ratio=0.75)
        second = scan_live.select_entries(important + others, 8, 101, important_ratio=0.75)
        self.assertEqual(len(first), 8)
        self.assertEqual(sum(e["group"] == "央视" for e in first), 6)
        self.assertEqual(len({e["name"] for e in first if e["group"] == "央视"}), 6)
        self.assertEqual(len({scan_live.line_id(e) for e in first}), 8)
        self.assertNotEqual({e["url"] for e in first}, {e["url"] for e in second})

    def test_priority_sampling_uses_oldest_unchecked_backup(self):
        a = {"name": "CCTV-1", "url": "https://example.com/old", "group": "央视", "directives": []}
        b = dict(a, url="https://example.com/unchecked")
        history = {"version": 1, "lines": {
            scan_live.line_id(a): {"checked_at": "2026-10-23T08:00:00+08:00"}}}
        result = scan_live.select_entries([a, b], 1, 10, important_ratio=1, history=history)
        self.assertEqual(result[0]["url"], b["url"])

    def test_blocked_and_skipped_attempts_rotate_without_changing_health(self):
        from health_tracking import record_results

        entries = [{"name": "CCTV-10 科教", "group": "央视",
                    "url": f"https://example.com/{index}", "directives": []}
                   for index in range(3)]
        now = datetime(2026, 10, 23, tzinfo=timezone.utc)
        for status in ("blocked", "skipped"):
            with self.subTest(status=status):
                history = {"version": 1, "lines": {}}
                selected_ids = []
                for day in range(4):
                    chosen = scan_live.select_entries(entries, 1, day, important_ratio=1,
                                                      history=history)[0]
                    key = scan_live.line_id(chosen)
                    selected_ids.append(key)
                    record_results(history, [{"id": key, "status": status}],
                                   (now + timedelta(days=day)).isoformat())
                self.assertEqual(len(set(selected_ids[:3])), 3)
                self.assertEqual(selected_ids[3], selected_ids[0])
                for item in history["lines"].values():
                    self.assertEqual(item["score"], 50.0)
                    self.assertEqual(item["failures"], 0)
                    self.assertNotIn("checked_at", item)

    def test_sampling_compares_attempt_times_across_timezones(self):
        a = {"name": "Test", "group": "央视", "url": "https://example.com/a", "directives": []}
        b = dict(a, url="https://example.com/b")
        history = {"lines": {
            scan_live.line_id(a): {"observations": [{"at": "2026-10-23T08:00:00+08:00"}]},
            scan_live.line_id(b): {"observations": [{"at": "2026-10-23T01:00:00+00:00"}]},
        }}
        chosen = scan_live.select_entries([a, b], 1, 0, important_ratio=1, history=history)
        self.assertEqual(chosen, [a])

    def test_daily_probe_budget_and_no_duplicate_checks(self):
        entries = [{"name": f"Channel {i}", "url": f"https://example.com/{i}",
                    "group": "央视", "directives": []} for i in range(24)]
        counts = {}
        def fake_probe(entry, ffmpeg):
            key = scan_live.line_id(entry)
            counts[key] = counts.get(key, 0) + 1
            return {"id": key, "status": "failed"}
        with patch.object(scan_live, "probe", side_effect=fake_probe):
            results = scan_live.scan_entries(entries, "ffmpeg", retry_limit=8)
        self.assertEqual(len(results), 24)
        self.assertEqual(sum(counts.values()), 32)
        self.assertEqual(sum(v == 2 for v in counts.values()), 8)



    def test_ipv6_literals_are_deferred_without_score_penalty(self):
        ipv6 = {"url": "http://[2409:8087:8:21::18]:6610/stream.m3u8",
                "name": "CCTV-1 综合", "group": "央视", "directives": []}
        ipv4 = dict(ipv6, url="http://192.0.2.3/stream.m3u8")
        self.assertEqual(network_policy.network_type(ipv6["url"]), "ipv6_literal")
        self.assertEqual(network_policy.network_type(ipv4["url"]), "ipv4_literal")
        self.assertEqual(network_policy.network_type("https://example.org/stream"), "hostname_unresolved")
        self.assertEqual([x["url"] for x in scan_live.select_entries([ipv6, ipv4], 4, 3)],
                         [ipv4["url"]])
        with patch.object(scan_live.subprocess, "run") as called:
            self.assertEqual(scan_live.probe(ipv6, "ffmpeg")["status"], "skipped")
            called.assert_not_called()
        history = {"version": 1, "lines": {}}
        from health_tracking import record_results
        record_results(history, [{"id": scan_live.line_id(ipv6), "status": "skipped"}],
                       datetime.now(timezone.utc).isoformat())
        self.assertEqual(history["lines"][scan_live.line_id(ipv6)]["score"], 50.0)

    def test_regional_target_alias_and_priority(self):
        catalog = {"sources": [{
            "id": "regional", "url": "https://source.example/regional.txt",
            "name": "regional", "type": "community", "rights": "unverified",
            "homepage": "https://source.example", "mode": "decode_then_publish",
            "include_channels": ["灌阳新闻综合", "桂林新闻综合", "广西新闻"],
        }]}
        def sample(name, token):
            return {"name": name, "group": "广西", "url": "https://example.org/" + token,
                    "attrs": {}, "directives": []}
        items = [sample("灌阳新闻综合[1024*576]", "gy"),
                 sample("广西桂林综合", "gl"),
                 sample("广西新闻", "gx"),
                 sample("桂林公共", "stopped")]
        report, queue = source_review.summarize_candidates(
            catalog, [], lambda url, parse: (items, {"cached": False}), with_entries=True)
        self.assertEqual({e["name"] for e in queue},
                         {"灌阳新闻综合", "桂林新闻综合", "广西新闻"})
        self.assertEqual(report["priority_unlisted"], 3)
        self.assertEqual([e["priority"] for e in queue],
                         ["featured_local_channel", "regional_channel",
                          "regional_channel"])
        self.assertTrue(all(e["group"] == "地方" for e in queue))

    def test_local_display_priority_does_not_reserve_scan_slots(self):
        def entry(i, priority, group):
            return {"id": f"test-{i}", "url": f"https://example.org/{i}.m3u8",
                    "priority": priority, "group": group, "name": "Test"}
        entries = ([entry(i, "featured_local_channel", "地方") for i in range(8)] +
                   [entry(i + 10, "regional_channel", "地方") for i in range(8)] +
                   [entry(i + 20, "target_missing_channel", "地方") for i in range(12)] +
                   [entry(i + 40, "domestic_channel_candidate", "央视") for i in range(12)])
        selected = candidate_scan.choose_candidates(entries, {"lines": {}}, 12, 10)
        self.assertEqual(len(selected), 12)
        self.assertEqual(sum(e["priority"] == "target_missing_channel" for e in selected), 6)
        self.assertEqual(sum(e["priority"] == "domestic_channel_candidate" for e in selected), 4)
        self.assertLessEqual(sum(e["priority"] == "featured_local_channel" for e in selected), 2)

    def test_regional_category_order_preserves_satellites(self):
        from channel_catalog import organize
        settings = json.loads((update.ROOT / "channel_names.json").read_text(encoding="utf-8"))
        def entry(name, i):
            return {"name": name, "group": "上游", "source": "cn",
                    "url": f"https://example.org/{i}", "attrs": {}, "directives": []}
        names = ["广西新闻", "广西卫视", "桂林科教旅游", "灌阳新闻综合",
                 "桂林新闻综合", "广东新闻", "CCTV-1 综合"]
        output = organize([entry(n, i) for i, n in enumerate(names)], settings)
        self.assertEqual([e["name"] for e in output if e["group"] == "广西"],
                         ["灌阳新闻综合", "广西新闻", "桂林科教旅游", "桂林新闻综合"])
        self.assertEqual(next(e["group"] for e in output if e["name"] == "广西卫视"), "卫视")
        self.assertEqual(next(e["group"] for e in output if e["name"] == "广东新闻"), "地方")

    def test_family_selection_is_a_consistent_managed_directory(self):
        selection = json.loads((update.ROOT / "family_selection.json").read_text(encoding="utf-8"))
        channels = selection["channels"]
        self.assertIn("CCTV-1 综合", channels)
        self.assertEqual(selection["mode"], "fixed")
        self.assertIn("CCTV-6 电影", channels)
        self.assertEqual(len(channels), len(set(channels)))
        self.assertEqual(set(channels), {e["name"] for e in selection["fixed_entries"]})

    def test_htv_target_source_filters_and_normalizes(self):
        catalog = {"sources": [{
            "id": "htv-test", "url": "https://source.example/list.txt",
            "name": "htv", "type": "community", "rights": "unverified",
            "homepage": "https://source.example", "mode": "decode_then_publish",
            "include_channels": ["深圳都市", "广东新闻"],
        }]}
        def entry(name, suffix):
            return {"name": name, "group": "local", "url": "https://example.org/" + suffix,
                    "attrs": {}, "directives": []}
        rows = [entry("广东深圳都市", "urban"), entry("广东新闻", "news"),
                entry("购物广告", "shopping")]
        report, queue = source_review.summarize_candidates(
            catalog, [], lambda url, validator: (rows, {"cached": False}), with_entries=True)
        self.assertEqual({e["name"] for e in queue}, {"深圳都市", "广东新闻"})
        self.assertEqual(report["priority_unlisted"], 2)
        self.assertTrue(all(e["priority"] == "target_missing_channel" for e in queue))
        self.assertTrue(all(e["group"] == "地方" for e in queue))

    def test_htv_targets_get_bounded_scan_slots(self):
        def item(i, priority, group):
            return {"id": f"id-{i}", "url": f"https://example.org/{i}.m3u8",
                    "name": "channel", "group": group, "priority": priority}
        entries = ([item(i, "target_missing_channel", "地方") for i in range(15)] +
                   [item(i + 20, "domestic_channel_candidate", "央视") for i in range(15)] +
                   [item(i + 40, "other", "其他") for i in range(15)])
        selected = candidate_scan.choose_candidates(entries, {"lines": {}}, 12, 10)
        self.assertEqual(len(selected), 12)
        self.assertEqual(sum(e["priority"] == "target_missing_channel" for e in selected), 6)
        self.assertEqual(len({e["id"] for e in selected}), 12)

    def test_candidate_source_is_review_only_and_deduplicated(self):
        base = {"name": "CCTV-1", "group": "央视", "url": "https://example.com/known",
                "attrs": {}, "directives": []}
        extra = dict(base, url="https://example.com/new")
        catalog = {"sources": [
            {"id": "one", "url": "https://source.example/1",
             "name": "one", "type": "community", "rights": "unverified",
             "homepage": "https://source.example", "mode": "review_only"},
            {"id": "two", "url": "https://source.example/2",
             "name": "two", "type": "community", "rights": "unverified",
             "homepage": "https://source.example", "mode": "review_only"}]}
        def fake_download(url, validator):
            return [base, extra], {"cached": False}
        result = source_review.summarize_candidates(catalog, [base], fake_download)
        self.assertEqual(result["total_unique"], 2)
        self.assertEqual(result["not_in_main"], 1)
        self.assertEqual(result["priority_unlisted"], 1)
        self.assertEqual(result["preview"][0]["sources"], ["one", "two"])
        self.assertFalse(any("url" in e for e in result["preview"]))

    def test_candidate_rejects_unconditional_publish_configuration(self):
        catalog = {"sources": [{"id": "bad", "type": "community", "mode": "publish"}]}
        with self.assertRaises(ValueError):
            source_review.summarize_candidates(catalog, [], lambda *_: ([], {}))

    def test_family_review_never_includes_unchecked_new_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "family_selection.json").write_text(json.dumps(
                {"channels": ["CCTV-4 中文国际", "广西卫视"],
                 "max_lines_per_channel": 2}), encoding="utf-8")
            out = root / "public"
            out.mkdir()
            existing = {"name": "CCTV-4 中文国际", "group": "央视",
                        "url": "https://example.com/4", "attrs": {}, "directives": []}
            other = dict(existing, name="广西卫视", group="卫视", url="https://example.com/gx")
            config = {"live_additions": [{"name": "CCTV-4 中文国际", "url": existing["url"],
                       "verified_at": "2026-10-22", "verification": "画面同台"}]}
            curation.build_family_outputs([existing, other], {"lines": {}}, config, out,
                                          datetime(2026, 10, 23, tzinfo=timezone.utc),
                                          update.render_live)
            preview = (out / "family-preview.m3u").read_text(encoding="utf-8")
            checked = (out / "family-content-checked.m3u").read_text(encoding="utf-8")
            summary = json.loads((out / "family-review.json").read_text(encoding="utf-8"))
            self.assertIn("广西卫视", preview)
            self.assertNotIn("广西卫视", checked)
            self.assertIn("CCTV-4 中文国际", checked)
            self.assertEqual(summary["manual_content_checked_lines"], 1)

    def test_phoenix_excluded_from_family_but_kept_in_full_list(self):
        with tempfile.TemporaryDirectory(dir=update.ROOT) as folder:
            root = Path(folder)
            shutil.copy2(update.ROOT / "family_selection.json", root / "family_selection.json")
            selection = json.loads((root / "family_selection.json").read_text(encoding="utf-8"))
            selection["mode"] = "health_ranked"
            (root / "family_selection.json").write_text(json.dumps(selection), encoding="utf-8")
            out = root / "public"
            out.mkdir()
            entry = {"name": "凤凰中文台", "group": "香港", "url": "https://example.com/phoenix",
                     "attrs": {}, "directives": []}
            preview = curation.build_family_outputs([entry], {"lines": {}}, {}, out,
                                                   datetime.now(timezone.utc), update.render_live)
            self.assertEqual(preview, [])
            update.render_live(curation.family_group_entries(preview) + [entry], out)
            for filename in ("live.m3u", "live.txt"):
                parsed = update.parse_live((out / filename).read_text(encoding="utf-8"))
                self.assertEqual(parsed[0]["name"], "凤凰中文台")
                self.assertEqual(parsed[0]["group"], "香港")

    def test_expired_manual_check_is_not_treated_as_current(self):
        self.assertFalse(curation.is_recent_content_check(
            "https://example.com/4",
            [{"url": "https://example.com/4", "verified_at": "2026-09-01",
              "verification": "old"}],
            datetime(2026, 10, 23, tzinfo=timezone.utc)))


    def test_unified_subscription_has_favorites_first_and_preserves_full_list(self):
        all_entries = []
        for name, group in (("CCTV-1 综合", "央视"), ("广西卫视", "卫视"),
                            ("海外频道", "美国")):
            all_entries.append({"name": name, "group": group, "attrs": {"tvg-name": name},
                                "url": "https://example.com/" + str(len(all_entries)),
                                "directives": []})
        preview = all_entries[:2]
        favorites = curation.family_group_entries(preview)
        self.assertEqual([e["group"] for e in favorites], ["家庭精选", "家庭精选"])
        self.assertEqual([e["group"] for e in all_entries], ["央视", "卫视", "美国"])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            update.render_live(favorites + all_entries, root)
            parsed = update.parse_live((root / "live.m3u").read_text(encoding="utf-8"))
            self.assertEqual([e["group"] for e in parsed],
                             ["家庭精选", "家庭精选", "央视", "卫视", "美国"])
            txt = (root / "live.txt").read_text(encoding="utf-8")
            self.assertTrue(txt.startswith("家庭精选,#genre#\n"))
            # 精选保留相同 URL，不新增频道源或破坏原列表。
            self.assertEqual(parsed[0]["url"], parsed[2]["url"])
            self.assertEqual(parsed[1]["url"], parsed[3]["url"])

    def test_favorite_clones_do_not_consume_live_scan_slots(self):
        favorites = [
            {"name": "CCTV-1 综合", "group": "家庭精选", "url": "https://example.com/1",
             "attrs": {}, "directives": []}]
        original = [dict(favorites[0], group="央视")]
        selected = scan_live.select_entries(favorites + original, 1, 2)
        self.assertEqual(selected[0]["group"], "央视")
        self.assertEqual(len(selected), 1)

    def test_review_only_candidate_scan_is_bounded_and_does_not_promote(self):
        def entry(i, group):
            url = f"https://sample.example/{i}"
            return {"id": scan_live.line_id({"url": url, "directives": []}),
                    "name": "CCTV-1" if group == "央视" else "Other",
                    "url": url, "group": group, "directives": []}
        entries = [entry(i, "央视" if i < 16 else "其他") for i in range(24)]
        for bad in ("http://127.0.0.1/a.m3u8", "http://169.254.169.254/latest/meta-data",
                    "http://192.168.1.1", "http://[::1]/", "http://localhost/"):
            self.assertFalse(candidate_scan.allowed_candidate(
                {"url": bad, "name": "ignore"}))
        self.assertTrue(candidate_scan.allowed_candidate(entries[0]))
        seen = {}
        def fake_probe(e, ffmpeg):
            seen[e["id"]] = seen.get(e["id"], 0) + 1
            return {"id": e["id"], "name": e["name"], "group": e["group"],
                    "status": "decoded", "reason": "only decodable"}
        with patch.object(scan_live, "probe", side_effect=fake_probe):
            # scan_entries 从 scan_live 导入，共享的模拟函数仍生效。
            history = {"version": 1, "lines": {}}
            report = candidate_scan.run_scan(entries, history, "ffmpeg",
                                             limit=12, retry_limit=4, visual_limit=0,
                                             now=datetime(2026, 10, 23, tzinfo=timezone.utc))
        self.assertEqual(report["sampled"], 12)
        self.assertEqual(report["attempts"], 12)
        self.assertEqual(sum(r["group"] == "央视" for r in report["results"]), 9)
        self.assertEqual(report["eligible_for_auto_publish"], 0)
        self.assertEqual(len(history["lines"]), 12)
        self.assertFalse(any("url" in r for r in report["results"]))

    def test_candidate_report_preserves_existing_api_and_returns_private_queue(self):
        known = {"name": "CCTV-1", "group": "央视", "url": "https://example.com/known",
                 "attrs": {}, "directives": []}
        new = dict(known, url="https://example.com/new")
        source = {"id": "src", "url": "https://example.com/source", "name": "src",
                  "type": "community", "mode": "review_only",
                  "rights": "unverified", "homepage": "https://example.com"}
        data, queue = source_review.summarize_candidates(
            {"sources": [source]}, [known],
            lambda url, validator: ([known, new], {"cached": False}), with_entries=True)
        self.assertEqual(data["not_in_main"], 1)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["url"], new["url"])
        self.assertEqual(queue[0]["id"], scan_live.line_id(new))
        self.assertFalse(any("url" in r for r in data["preview"]))


if __name__ == "__main__":
    unittest.main()
