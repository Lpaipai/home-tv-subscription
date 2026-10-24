# 2026/10/24 02:26:00 by liaoweipeng1
"""固定精选不能被上游候选、健康排序或旧目录重新填充。"""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from curation import build_family_outputs
from update import ROOT, parse_live, render_live


class FixedFamilyTests(unittest.TestCase):
    def generate(self, selection, upstream=None):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            out = root / "public"
            out.mkdir()
            (root / "family_selection.json").write_text(json.dumps(selection), encoding="utf-8")
            selected = build_family_outputs(upstream or [], {"lines": {}}, {}, out,
                                            datetime.now(timezone.utc), render_live)
            return selected, (out / "family-preview.m3u").read_text(encoding="utf-8"), (out / "family-preview.txt").read_text(encoding="utf-8")

    def setUp(self):
        self.config = json.loads((ROOT / "family_selection.json").read_text(encoding="utf-8"))

    def test_fixed_directory_survives_empty_upstream(self):
        selected, m3u, _ = self.generate(self.config)
        self.assertEqual(len(selected), len(self.config["channels"]))
        self.assertEqual([e["name"] for e in selected], self.config["channels"])
        self.assertEqual(len(parse_live(m3u)), len(selected))

    def test_upstream_cannot_replace_current_lines(self):
        fake = copy.deepcopy(self.config["fixed_entries"][0])
        fake["url"] = "https://example.com/unwanted.m3u8"
        selected, m3u, _ = self.generate(self.config, [fake])
        self.assertNotIn("unwanted", m3u)
        self.assertEqual(selected[0]["url"], self.config["fixed_entries"][0]["url"])

    def test_headers_preserved_and_not_lost_in_txt(self):
        _, m3u, txt = self.generate(self.config)
        parsed = parse_live(m3u)
        self.assertEqual(parsed[0]["attrs"]["http-user-agent"], "AptvPlayer-UA")
        self.assertIn("#EXTVLCOPT:http-user-agent=AptvPlayer-UA", parsed[0]["directives"])
        self.assertEqual(len(parse_live(txt)), sum(not e["directives"] for e in self.config["fixed_entries"]))
        self.assertNotIn("101.35.240.114", txt)

    def test_missing_fixed_channel_is_rejected(self):
        self.config["fixed_entries"].pop()
        with self.assertRaises(ValueError):
            self.generate(self.config)

    def test_unknown_mode_is_rejected(self):
        self.config["mode"] = "fixde"
        with self.assertRaises(ValueError):
            self.generate(self.config)

    def test_verification_metadata_is_carried_without_inference(self):
        expected = [e.get("device_verification") for e in self.config["fixed_entries"]]
        selected, _, _ = self.generate(self.config)
        self.assertEqual([e.get("device_verification") for e in selected], expected)

    def test_new_channel_and_category_need_only_configuration(self):
        entry = {"name": "未来新增频道", "group": "新增分类", "url": "https://example.com/new.m3u8",
                 "attrs": {}, "directives": []}
        self.config["channels"].append(entry["name"])
        self.config["fixed_entries"].append(entry)
        selected, m3u, _ = self.generate(self.config)
        self.assertEqual(selected[-1], entry)
        self.assertIn('group-title="新增分类"', m3u)
        self.assertEqual(len(selected), len(self.config["channels"]))


if __name__ == "__main__":
    unittest.main()
