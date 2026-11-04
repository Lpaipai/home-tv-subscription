"""电影频道专用候选识别、定向轮换与音频抽检回归测试。"""
import subprocess
import unittest
from unittest.mock import patch

import candidate_scan
import movie_scan
from health_tracking import line_id


def entry(name, url, group="电影候选", directives=None):
    return {"name": name, "url": url, "group": group,
            "directives": directives or [], "attrs": {}}


class MovieCandidateTests(unittest.TestCase):
    def setUp(self):
        self.settings = movie_scan.load_settings()
        self.aliases = movie_scan.load_aliases()

    def test_known_movie_channels_and_aliases_are_targeted(self):
        self.assertTrue(movie_scan.is_movie_candidate("CHC Action", self.settings, self.aliases))
        self.assertTrue(movie_scan.is_movie_candidate("CHC 家庭影院", self.settings, self.aliases))
        self.assertTrue(movie_scan.is_movie_candidate("美亚电影台", self.settings, self.aliases))
        self.assertTrue(movie_scan.is_movie_candidate("CCTV-6", self.settings, self.aliases))
        self.assertFalse(movie_scan.is_movie_candidate("CCTV-5 体育", self.settings, self.aliases))

    def test_non_movie_or_excluded_names_are_not_targeted(self):
        for name in ("浙江教科影视", "Girlfriends Films", "FilmRise Forensic Files",
                     "FilmRise Anime", "普通综合频道"):
            with self.subTest(name=name):
                self.assertFalse(movie_scan.is_movie_candidate(name, self.settings, self.aliases))

    def test_general_candidate_scanner_leaves_movie_candidates_to_targeted_scan(self):
        movie = entry("CHC 动作电影", "https://example.com/movie.m3u8")
        regular = entry("普通综合频道", "https://example.com/general.m3u8")
        regular["priority"] = "other"
        regular["id"] = line_id(regular)
        movie["priority"] = "other"
        movie["id"] = line_id(movie)
        selected = candidate_scan.choose_candidates(
            [movie, regular], {"lines": {}}, limit=2, day=0)
        self.assertNotIn(movie["id"], {item["id"] for item in selected})
        self.assertIn(regular["id"], {item["id"] for item in selected})


class MovieSelectionTests(unittest.TestCase):
    def setUp(self):
        self.settings = movie_scan.load_settings()
        self.aliases = movie_scan.load_aliases()
        self.history = {"version": 1, "lines": {}}

    def test_family_movie_channels_are_always_selected_first(self):
        items = [
            entry("CHC 动作电影", "https://example.com/action.m3u8"),
            entry("CHC 家庭影院", "https://example.com/home.m3u8"),
            entry("CCTV-6 电影", "https://example.com/cctv6.m3u8", "电影频道"),
            entry("美亚电影台", "https://example.com/meiah.m3u8"),
            entry("天映频道", "https://example.com/celestial.m3u8"),
        ]
        selected = movie_scan.select_movie_entries(
            items, self.history, self.settings, self.aliases, limit=4, rotation=0)
        names = [item["name"] for item in selected]
        self.assertEqual(names[:3], self.settings["daily_scan"]["always_check"])
        self.assertEqual(len(selected), 4)

    def test_candidate_channels_rotate_without_duplicate_lines(self):
        items = [entry("美亚电影台", "https://example.com/meiah-1.m3u8"),
                 entry("美亚电影台", "https://example.com/meiah-2.m3u8"),
                 entry("天映频道", "https://example.com/celestial.m3u8"),
                 entry("东森洋片台", "https://example.com/ebc.m3u8")]
        first = movie_scan.select_movie_entries(
            items, self.history, self.settings, self.aliases, limit=4, rotation=0)
        second = movie_scan.select_movie_entries(
            items, self.history, self.settings, self.aliases, limit=4, rotation=1)
        self.assertEqual(len({line_id(item) for item in first}), len(first))
        self.assertNotEqual([item["name"] for item in first],
                            [item["name"] for item in second])


class MovieProbeTests(unittest.TestCase):
    def test_audio_probe_preserves_headers_and_decodes_audio_stream(self):
        item = entry("CHC 动作电影", "https://example.com/movie.m3u8",
                     directives=["#EXTVLCOPT:http-user-agent=MovieTest-UA",
                                 "#EXTVLCOPT:http-referrer=https://example.com/"])
        completed = subprocess.CompletedProcess([], 0, b"", b"")
        with patch.object(movie_scan.subprocess, "run", return_value=completed) as run:
            result = movie_scan.probe_audio(item, "ffmpeg")
        self.assertEqual(result["status"], "decoded")
        command = run.call_args.args[0]
        self.assertIn("0:a:0", command)
        self.assertIn("-vn", command)
        self.assertIn("User-Agent: MovieTest-UA\r\nReferer: https://example.com/\r\n",
                      command[command.index("-headers") + 1])
        self.assertEqual(run.call_args.kwargs["timeout"], 16)

    def test_audio_probe_timeout_and_failure_are_not_reported_as_success(self):
        item = entry("CHC 家庭影院", "https://example.com/movie.m3u8")
        with patch.object(movie_scan.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired([], 16)):
            self.assertEqual(movie_scan.probe_audio(item, "ffmpeg")["status"], "failed")
        completed = subprocess.CompletedProcess([], 1, b"", b"no audio stream")
        with patch.object(movie_scan.subprocess, "run", return_value=completed):
            self.assertEqual(movie_scan.probe_audio(item, "ffmpeg")["status"], "failed")

    def test_movie_probe_requires_both_video_and_audio(self):
        item = entry("CHC 动作电影", "https://example.com/movie.m3u8")
        video = {"id": line_id(item), "name": item["name"], "group": item["group"],
                 "status": "decoded", "reason": "video decoded"}
        audio = {"id": line_id(item), "status": "decoded", "reason": "audio decoded"}
        with patch.object(movie_scan, "probe_video", return_value=video), \
             patch.object(movie_scan, "probe_audio", return_value=audio):
            self.assertEqual(movie_scan.probe_movie(item, "ffmpeg")["status"], "decoded")
        with patch.object(movie_scan, "probe_video", return_value=video), \
             patch.object(movie_scan, "probe_audio", return_value=dict(audio, status="failed")):
            self.assertEqual(movie_scan.probe_movie(item, "ffmpeg")["status"], "failed")
        with patch.object(movie_scan, "probe_video", return_value=dict(video, status="failed")), \
             patch.object(movie_scan, "probe_audio") as audio_probe:
            self.assertEqual(movie_scan.probe_movie(item, "ffmpeg")["audio_status"], "not_checked")
            audio_probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
