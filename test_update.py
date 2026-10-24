# 2026/10/22 19:52:00 by liaoweipeng1
"""验证移动订阅时容易造成失效的格式、依赖路径和回退行为。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import update
from channel_catalog import organize, summarize


class SubscriptionTests(unittest.TestCase):
    def test_channel_aliases_order_and_distinct_cctv_variants(self):
        settings = json.loads((update.ROOT / "channel_names.json").read_text(encoding="utf-8"))
        def entry(name, url):
            return {"name": name, "group": "old", "source": "cn", "url": url,
                    "attrs": {}, "directives": []}
        data = [entry("CCTV-13 HD (1080p)", "https://a/13"),
                entry("CCTV-5+", "https://a/5plus"), entry("CCTV-5", "https://a/5"),
                entry("CCTV-13 (720p)", "https://b/13"), entry("CCTV-4K HD (1080p)", "https://a/4k"),
                entry("CCTV-13 (720p)", "https://a/13"),
                entry("Beijing Satellite TV [Not 24/7]", "https://a/bj")]
        result = organize(data, settings)
        self.assertEqual([e["name"] for e in result[:4]],
                         ["CCTV-5 体育", "CCTV-5+ 体育赛事", "CCTV-13 新闻", "CCTV-13 新闻"])
        self.assertEqual(len(result), 6)
        self.assertEqual(result[-1]["group"], "卫视")
        self.assertEqual(result[-1]["name"], "北京卫视")
        self.assertEqual(summarize(result)[0]["channels"][2]["lines"], 2)

    def test_region_and_broadcast_versions_do_not_merge(self):
        settings = {"group_order": ["美国", "日本"], "aliases": {}}
        data = [{"name": "Test HD (1080p)", "group": "News", "source": region,
                 "url": "https://a/" + region + version, "attrs": {"tvg-id": "Test.us@" + version},
                 "directives": []} for region, version in [("us", "East"), ("us", "West"), ("jp", "East")]]
        result = organize(data, settings)
        self.assertEqual(len({(e["group"], e["name"]) for e in result}), 3)
        self.assertIn("东部版", result[0]["name"])
        self.assertEqual(result[-1]["group"], "日本")

    def test_m3u_preserves_header_and_quoted_comma(self):
        text = '#EXTM3U\n#EXTINF:-1 tvg-logo="https://a/x,y" group-title="新闻",新闻台\n#EXTVLCOPT:http-referrer=https://a/\nhttps://a/live.m3u8\n'
        result = update.parse_live(text)
        self.assertEqual(result[0]["name"], "新闻台")
        self.assertEqual(result[0]["attrs"]["tvg-logo"], "https://a/x,y")
        self.assertEqual(len(result[0]["directives"]), 1)

    def test_txt_metadata_is_not_a_channel(self):
        text = '更新时间,#genre#\n2026-10-22 00:00:00,https://a/meta\n央视,#genre#\nCCTV-1,https://a/live?token=x$y\n'
        result = update.parse_live(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], "https://a/live?token=x$y")

    def test_relative_dependencies_use_original_location(self):
        value = {"spider": "./fty.jar;md5;abc", "sites": [{"api": "csp_Test", "ext": "../rule.json", "jar": "libs/test.jar"}]}
        result = update.absolutize(value, "https://upstream.example/box/m.json")
        self.assertEqual(result["spider"], "https://upstream.example/box/fty.jar;md5;abc")
        self.assertEqual(result["sites"][0]["ext"], "https://upstream.example/rule.json")
        self.assertEqual(result["sites"][0]["jar"], "https://upstream.example/box/libs/test.jar")
        self.assertEqual(result["sites"][0]["api"], "csp_Test")

    def test_dedup_keeps_different_playback_headers(self):
        a = {"name": "CCTV-1", "url": "https://a/live", "directives": []}
        b = dict(a, directives=["#EXTVLCOPT:http-user-agent=TV"])
        self.assertEqual(len(update.deduplicate([a, a, b])), 2)

    def test_invalid_response_cannot_replace_cache(self):
        with tempfile.TemporaryDirectory(dir=update.ROOT) as folder:
            with patch.object(update, "CACHE", Path(folder)):
                import hashlib
                url = "https://example.com/source"
                key = hashlib.sha256(url.encode()).hexdigest()
                p = Path(folder) / (key + ".txt")
                p.write_text('{"sites":[{"key":"test"}]}', encoding="utf-8")
                (Path(folder) / (key + ".json")).write_text(json.dumps({"url": url}), encoding="utf-8")
                with patch.object(update, "urlopen", side_effect=TimeoutError("offline")):
                    value, meta = update.fetch(url, update.parse_json)
                self.assertEqual(value["sites"][0]["key"], "test")
                self.assertTrue(meta["cached"])

    def test_html_is_rejected(self):
        with self.assertRaises(ValueError):
            update.parse_live("<html>gateway error</html>")

    def test_empty_vod_cannot_replace_valid_source(self):
        for text in ('{}', '{"sites":[]}', '{"urls":[]}'):
            with self.assertRaises(ValueError):
                update.parse_vod(text)

    def test_grouped_output_keeps_channels_and_headers(self):
        text = '#EXTM3U\n#EXTINF:-1 group-title="A",One\nhttps://a/1\n#EXTINF:-1 group-title="B",Two\nhttps://b/2\n#EXTINF:-1 group-title="A",Three\n#EXTVLCOPT:http-referrer=https://a/\nhttps://a/3\n'
        entries = update.parse_live(text)
        with tempfile.TemporaryDirectory(dir=update.ROOT) as folder:
            out = Path(folder)
            self.assertEqual(update.render_live(entries, out), 1)
            actual = update.parse_live((out / "live.m3u").read_text(encoding="utf-8"))
            self.assertEqual([e["name"] for e in actual], ["One", "Three", "Two"])
            self.assertEqual(actual[1]["directives"], entries[2]["directives"])
            self.assertEqual(len(update.parse_live((out / "live.txt").read_text(encoding="utf-8"))), 2)


if __name__ == "__main__":
    unittest.main()
