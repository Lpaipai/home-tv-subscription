"""IPv6 免云端实播候选的安全、去重、入库和跨日回归测试。"""
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
import source_review
import update
from health_tracking import line_id
from network_policy import is_public_ipv6_url

SOURCE = "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv6.m3u"
APPROVED = {("fanmingming-ipv6", SOURCE)}
URL = "http://[2409:8087:8:21::18]:6610/otttv/test.m3u8?"


def sample(url=URL, **updates):
    entry = {
        "name": "CCTV-1综合", "group": "央视", "original_group": "央视频道",
        "url": url, "attrs": {}, "directives": [],
        "source_id": "fanmingming-ipv6", "source_url": SOURCE,
        "source_mode": "decode_then_publish",
        "ipv6_direct_publish": True, "region": "cn",
    }
    entry.update(updates)
    entry["id"] = line_id(entry)
    return entry


class IPv6ProvisionalTests(unittest.TestCase):
    def test_only_global_ipv6_http_urls_allowed(self):
        self.assertTrue(is_public_ipv6_url(URL))
        self.assertTrue(is_public_ipv6_url("https://[2001:4860:4860::8888]/a.m3u8"))
        for url in (
            "http://[::1]/tv", "http://[fe80::1]/tv",
            # IPv6 组播段 ff00::/8，包括链路本地、站点与全局组播。
            "http://[ff02::1]/tv", "http://[ff05::2]/tv",
            "http://[ff0e::1]/tv",
            "http://[fc00::123]/tv", "http://[2001:db8::1]/tv",
            "http://[::ffff:8.8.8.8]/tv",
            "http://user:pass@[2409:8087:8:21::18]/tv",
            "http://[fe80::1%25eth0]/tv",
            "http://[2409:8087:8:21::18]:abc/tv",
            "http://[2409:8087:8:21::18]/tv#fragment",
            "http://[2409:8087:8:21::18]/tv|User-Agent=X",
            "file://[2409:8087:8:21::18]/tv",
            "http://127.0.0.1/tv", "https://example.org/tv",
            "http://[2409:8087:8:21::18]/tv\nEXTRA",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_public_ipv6_url(url))

    def test_approved_unverified_ipv6_does_not_consume_cloud_probe_budget(self):
        v6 = sample()
        v4 = sample("https://example.com/live")
        history = {"version": 1, "lines": {}}
        with patch.object(candidate_scan, "scan_entries", return_value=[{
            "id": v4["id"], "name": v4["name"], "group": v4["group"],
            "status": "failed", "reason": "not reachable"}]) as called:
            report = candidate_scan.run_scan([v6, v4], history, "ffmpeg",
                                             limit=12, retry_limit=0, visual_limit=0)
        called.assert_called_once()
        self.assertEqual(len(called.call_args.args[0]), 1)
        self.assertEqual(called.call_args.args[0][0]["url"], v4["url"])
        self.assertEqual(report["eligible_for_auto_publish"], 0)
        added = auto_additions.select_ipv6_provisional(
            [v6, v4], {"entries": {}}, [], {}, APPROVED)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["admission"], "ipv6_unverified")
        self.assertNotIn("last_decoded_at", added[0])
        self.assertEqual(added[0]["state"], "unverified")

    def test_source_opt_in_dedup_blacklist_holds_and_headers(self):
        allowed = sample()
        repeated = sample(name="重复台名")
        untrusted = sample("http://[2409:8087:8:21::19]/tv",
                           source_id="unknown", source_url="https://bad.example/list")
        private = sample("http://[fc00::1]/tv")
        multicast = sample("http://[ff02::1]/tv")
        headers = sample("http://[2409:8087:8:21::20]/tv",
                         directives=["#KODIPROP:inputstream.adaptive.license_type=widevine"])
        off = sample("http://[2409:8087:8:21::21]/tv", ipv6_direct_publish=False)
        entries = [allowed, repeated, untrusted, private, multicast, headers, off]
        result = auto_additions.select_ipv6_provisional(
            entries, {"entries": {}}, [], {}, APPROVED)
        self.assertEqual([entry["url"] for entry in result], [URL])
        result = auto_additions.select_ipv6_provisional(
            entries, {"entries": {}}, [], {"live_exclusions": [{"url": URL}]}, APPROVED)
        self.assertEqual(result, [])
        result = auto_additions.select_ipv6_provisional(
            entries, {"entries": {}}, [], {}, APPROVED, held_ids=[allowed["id"]])
        self.assertEqual(result, [])
        result = auto_additions.select_ipv6_provisional(
            entries, {"entries": {allowed["id"]: {"url": URL}}}, [], {}, APPROVED)
        self.assertEqual(result, [])

    def test_discovery_keeps_opt_in_when_same_line_appears_in_two_sources(self):
        first = {"id": "generic", "name": "普通", "url": "https://example.org/a",
                 "type": "community", "rights": "unverified",
                 "mode": "decode_then_publish", "homepage": "https://example.org"}
        second = dict(first, id="fanmingming-ipv6", url=SOURCE,
                      ipv6_direct_publish=True)
        source, queue = source_review.summarize_candidates(
            {"sources": [first, second]}, [],
            lambda *_: ([sample()], {"cached": False}), with_entries=True)
        self.assertEqual(len(queue), 1)
        self.assertTrue(queue[0]["ipv6_direct_publish"])
        self.assertEqual(queue[0]["source_id"], "fanmingming-ipv6")
        self.assertEqual(queue[0]["source_url"], SOURCE)

    def test_publish_keeps_decoding_evidence_separate_from_ipv6_and_next_rechecks(self):
        with tempfile.TemporaryDirectory(dir=update.ROOT) as folder:
            root = Path(folder)
            for name in ("config.json", "channel_names.json", "family_selection.json",
                         "candidate_sources.json"):
                shutil.copy2(update.ROOT / name, root / name)
            shutil.copytree(update.ROOT / "public", root / "public")
            line = sample()
            report = {"checked_at": datetime.now(timezone.utc).isoformat(),
                      "results": [], "promoted": 0}
            history = {"version": 1, "lines": {}}
            with patch.object(update, "ROOT", root):
                published = auto_additions.publish_scan([line], report, history)
                self.assertEqual(published["promoted"], 1)
                self.assertEqual(published["promoted_decoded"], 0)
                self.assertEqual(published["promoted_ipv6_unverified"], 1)
                ledger = auto_additions.load_additions(root / "public")
                item = ledger["entries"][line["id"]]
                self.assertEqual(item["state"], "unverified")
                self.assertEqual(item["admission"], "ipv6_unverified")
                text = (root / "public" / "live.m3u").read_text(encoding="utf-8")
                self.assertIn(URL, text)
                self.assertIn(URL, (root / "public" / "live.txt").read_text(encoding="utf-8"))
                self.assertNotIn(line["id"], json.loads(
                    (root / "public" / "health-history.json").read_text(encoding="utf-8"))["lines"])
                status = json.loads((root / "public" / "status.json").read_text(encoding="utf-8"))
                self.assertGreaterEqual(status["auto_additions_ipv6_unverified"], 1)
                future = datetime.now(timezone.utc) + timedelta(days=90)
                self.assertNotIn(line["id"], [line_id(e) for e in
                                     auto_maintenance.due(ledger["entries"].values(), future)])
                auto_maintenance.apply_observations(ledger, [{
                    "id": line["id"], "status": "failed"}], future)
                self.assertEqual(ledger["entries"][line["id"]]["state"], "unverified")
                self.assertIsNone(auto_maintenance.decoded_at(item))
                # 再次扫描不会重复收录，且本地既有线路保持不变。
                repeated = auto_additions.publish_scan([line], report, history)
                self.assertEqual(repeated["promoted"], 0)

    def test_refuse_forged_private_ipv6_ledger(self):
        for bad_url in ("http://[::1]/private", "http://[ff02::1]/tv",
                        "http://[ff0e::1]/tv"):
            with self.subTest(bad_url=bad_url), tempfile.TemporaryDirectory() as folder:
                p = Path(folder) / "auto-additions.json"
                item = {"url": bad_url, "directives": [],
                        "admission": "ipv6_unverified", "state": "unverified",
                        "origins": [SOURCE]}
                p.write_text(json.dumps({"version": 1, "entries": {
                    line_id(item): item}}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    auto_additions.load_additions(p.parent)


if __name__ == "__main__":
    unittest.main()
