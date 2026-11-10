# 2026/10/24 02:26:00 by liaoweipeng1
"""固定精选不能被上游候选、健康排序或旧目录重新填充。"""
import copy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from curation import build_family_outputs
from health_tracking import line_id
from update import ROOT, parse_live, render_live


class FixedFamilyTests(unittest.TestCase):
    def generate(self, selection, upstream=None, movie_selection=None, history=None):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            out = root / "public"
            out.mkdir()
            (root / "family_selection.json").write_text(json.dumps(selection), encoding="utf-8")
            if movie_selection is not None:
                (root / "movie_selection.json").write_text(json.dumps(movie_selection), encoding="utf-8")
            selected = build_family_outputs(upstream or [], history or {"lines": {}}, {}, out,
                                            datetime.now(timezone.utc), render_live)
            return selected, (out / "family-preview.m3u").read_text(encoding="utf-8"), (out / "family-preview.txt").read_text(encoding="utf-8")

    def setUp(self):
        self.config = json.loads((ROOT / "family_selection.json").read_text(encoding="utf-8"))

    def test_fixed_directory_survives_empty_upstream(self):
        selected, m3u, _ = self.generate(self.config)
        self.assertEqual(len(selected), len(self.config["fixed_entries"]))
        self.assertEqual([e["name"] for e in selected if e.get("line_role") != "backup_trial"],
                         self.config["channels"])
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
        missing = self.config["channels"][-1]
        self.config["fixed_entries"] = [e for e in self.config["fixed_entries"] if e["name"] != missing]
        with self.assertRaises(ValueError):
            self.generate(self.config)

    def test_unknown_mode_is_rejected(self):
        self.config["mode"] = "fixde"
        with self.assertRaises(ValueError):
            self.generate(self.config)

    def test_verification_metadata_is_carried_without_inference(self):
        expected = {line_id(e): e.get("device_verification") for e in self.config["fixed_entries"]}
        selected, _, _ = self.generate(self.config)
        self.assertEqual({line_id(e): e.get("device_verification") for e in selected}, expected)

    def test_movie_channels_are_added_as_a_separate_family_group(self):
        movie = json.loads((ROOT / "movie_selection.json").read_text(encoding="utf-8"))
        selected, m3u, txt = self.generate(self.config, movie_selection=movie)
        parsed = parse_live(m3u)
        movie_entries = [entry for entry in parsed if entry["group"] == "电影"]
        self.assertEqual(len(selected), len(self.config["fixed_entries"]) + len(movie["fixed_entries"]))
        self.assertEqual([entry["name"] for entry in movie_entries], movie["channels"])
        self.assertEqual(len(parsed), len(selected))
        self.assertEqual(len(movie_entries), 0)
        self.assertEqual(sum(e["name"] == "CCTV-6 电影" for e in parsed), 1)
        cctv6 = next(e for e in parsed if e["name"] == "CCTV-6 电影")
        self.assertEqual(cctv6["group"], "央视")
        self.assertEqual(cctv6["attrs"]["tvg-id"], "CCTV6")
        self.assertNotIn('group-title="电影频道"', m3u)
        self.assertNotIn('group-title="电影"', m3u)
        self.assertEqual(len(parse_live(txt)), sum(not entry.get("directives") for entry in selected))


    def test_auto_fill_needs_two_distinct_success_days_on_trusted_host(self):
        target = {"name": "广西卫视", "group": "央视",
                  "url": "http://101.35.240.114:88/live.php?id=广西卫视",
                  "attrs": {}, "directives": []}
        now = datetime.now(timezone.utc)
        observation = {
            "score": 90, "status": "decoded",
            "observations": [
                {"at": (now - timedelta(days=2)).isoformat(), "status": "decoded"},
                {"at": (now - timedelta(days=1)).isoformat(), "status": "decoded"},
            ],
        }
        selected, _, _ = self.generate(
            self.config, [target], history={"lines": {line_id(target): observation}})
        self.assertEqual(len(selected), len(self.config["fixed_entries"]) + 1)
        self.assertEqual(selected[-1]["name"], "广西卫视")
        # 同一日期重复成功，不允许晋级。
        observation["observations"][0]["at"] = observation["observations"][1]["at"]
        selected, _, _ = self.generate(
            self.config, [target], history={"lines": {line_id(target): observation}})
        self.assertEqual(len(selected), len(self.config["fixed_entries"]))
        # 其它源站即使跨日成功，也不允许绕过家庭同源准入。
        observation["observations"][0]["at"] = (now - timedelta(days=2)).isoformat()
        target["url"] = "https://untrusted.example/live.php?id=广西卫视"
        selected, _, _ = self.generate(
            self.config, [target], history={"lines": {line_id(target): observation}})
        self.assertEqual(len(selected), len(self.config["fixed_entries"]))

    def test_same_source_trials_and_backups_are_grouped_without_app_duplicates(self):
        selected, m3u, _ = self.generate(self.config)
        grouped = {}
        for entry in selected:
            grouped.setdefault(entry["name"], []).append(entry)
        self.assertEqual(len(self.config["channels"]), 54)
        self.assertEqual(len(self.config["fixed_entries"]), 90)
        self.assertEqual(self.config["max_lines_per_channel"], 2)
        cctv16 = grouped["广西卫视"]
        self.assertEqual(len(cctv16), 2)
        self.assertEqual(cctv16[0]["url"], "http://101.35.240.114:88/live.php?id=广西卫视")
        self.assertEqual(cctv16[0]["attrs"]["http-user-agent"], "iPhone")
        self.assertEqual(cctv16[0]["line_role"], "same_source_trial")
        self.assertEqual(cctv16[1]["line_role"], "backup_trial")
        beijing = grouped["北京卫视"]
        self.assertEqual(beijing[0]["url"], "http://101.35.240.114:88/live.php?id=北京卫视")
        self.assertEqual(beijing[1]["line_role"], "backup_trial")
        self.assertEqual(len(grouped["深圳卫视"]), 1)
        self.assertEqual(grouped["CGTN法语"][0]["url"], "https://0472.org/hls/cgtnf.m3u8")
        self.assertNotIn("CHC", m3u)

    def test_new_channel_and_category_need_only_configuration(self):
        entry = {"name": "未来新增频道", "group": "新增分类", "url": "https://example.com/new.m3u8",
                 "attrs": {}, "directives": []}
        self.config["channels"].append(entry["name"])
        self.config["fixed_entries"].append(entry)
        selected, m3u, _ = self.generate(self.config)
        self.assertEqual(selected[-1], entry)
        self.assertIn('group-title="新增分类"', m3u)
        self.assertEqual(len(selected), len(self.config["fixed_entries"]))


if __name__ == "__main__":
    unittest.main()
