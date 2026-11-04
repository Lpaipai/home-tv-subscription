"""电影频道候选发现与每日定向音视频抽检；不自动将候选加入家庭精选。"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import re
import shutil
import subprocess

from health_tracking import line_id, record_results
from network_policy import is_ipv6_literal
from scan_live import playback_headers, probe as probe_video
from update import ROOT, parse_live, write_json


def load_settings():
    return json.loads((ROOT / "movie_selection.json").read_text(encoding="utf-8"))


def load_aliases():
    path = ROOT / "channel_names.json"
    return json.loads(path.read_text(encoding="utf-8")).get("aliases", {})


def canonical_name(name, aliases):
    name = str(name or "").strip()
    canonical = aliases.get(name, name)
    if re.fullmatch(r"CCTV[- ]?6(?:\s*电影)?", canonical, re.I):
        return "CCTV-6 电影"
    return canonical


def is_movie_candidate(name, settings=None, aliases=None):
    """只把明确电影台及电影/影视关键词候选交给专用队列。"""
    settings = settings or load_settings()
    aliases = aliases if aliases is not None else load_aliases()
    canonical = canonical_name(name, aliases)
    scan = settings.get("daily_scan", {})
    lowered = canonical.casefold()
    if any(str(word).casefold() in lowered
           for word in scan.get("exclude_keywords", [])):
        return False
    if canonical in set(scan.get("target_names", [])):
        return True
    return any(str(word).casefold() in lowered
               for word in scan.get("keywords", []))


def _last_attempt(entry, history):
    item = history.get("lines", {}).get(line_id(entry), {})
    observations = item.get("observations", [])
    value = observations[-1].get("at") if observations else item.get("checked_at")
    try:
        return datetime.fromisoformat(value).timestamp() if value else 0
    except (TypeError, ValueError):
        return 0


def select_movie_entries(entries, history, settings, aliases, limit, rotation):
    """固定电影精选每日必测；其余候选按频道去重、最久未测优先轮换。"""
    unique = {}
    for original in entries:
        if not isinstance(original, dict) or not original.get("url"):
            continue
        name = canonical_name(original.get("name"), aliases)
        if not is_movie_candidate(name, settings, aliases):
            continue
        entry = dict(original, name=name)
        unique.setdefault(line_id(entry), entry)

    grouped = {}
    for entry in unique.values():
        grouped.setdefault(entry["name"], []).append(entry)
    for options in grouped.values():
        options.sort(key=lambda entry: (
            not entry.get("family_priority", False),
            _last_attempt(entry, history), line_id(entry)))

    scan = settings.get("daily_scan", {})
    always = scan.get("always_check", [])
    selected = []
    selected_ids = set()
    for name in always:
        options = grouped.get(name, [])
        if options and len(selected) < limit:
            entry = options[0]
            selected.append(entry)
            selected_ids.add(line_id(entry))

    remaining_names = [name for name in grouped if name not in set(always)]
    if remaining_names:
        ordered = sorted(remaining_names)
        offset = rotation % len(ordered)
        rotated = ordered[offset:] + ordered[:offset]
        order = {name: index for index, name in enumerate(rotated)}
        rotated.sort(key=lambda name: (
            _last_attempt(grouped[name][0], history), order[name], name))
        for name in rotated:
            if len(selected) >= limit:
                break
            entry = next((item for item in grouped[name]
                          if line_id(item) not in selected_ids), None)
            if entry is not None:
                selected.append(entry)
                selected_ids.add(line_id(entry))

    depth = 1
    while len(selected) < limit:
        added = False
        for name in sorted(grouped):
            options = grouped[name]
            if depth < len(options):
                entry = options[depth]
                key = line_id(entry)
                if key not in selected_ids:
                    selected.append(entry)
                    selected_ids.add(key)
                    added = True
                    if len(selected) >= limit:
                        break
        if not added:
            break
        depth += 1
    return selected


def probe_audio(entry, ffmpeg):
    result = {"id": line_id(entry), "name": entry["name"],
              "group": entry.get("group", "电影候选")}
    if is_ipv6_literal(entry):
        return dict(result, status="skipped", reason="IPv6 字面量地址未在云端验证，须在家庭网络复核")
    if not entry.get("url", "").startswith(("https://", "http://")) or "|" in entry["url"]:
        return dict(result, status="skipped", reason="此协议或地址参数不在音频抽检范围")
    try:
        headers = playback_headers(entry)
    except ValueError as error:
        return dict(result, status="skipped", reason=str(error))
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-threads", "1", "-rw_timeout", "5000000",
               "-protocol_whitelist", "http,https,tcp,tls,crypto"]
    if headers:
        command += ["-headers", headers]
    command += ["-i", entry["url"], "-map", "0:a:0", "-vn", "-t", "4",
                "-f", "null", "-"]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=16, check=False)
    except subprocess.TimeoutExpired:
        return dict(result, status="failed", reason="音频解码超过 16 秒，需家庭网络复核")
    if completed.returncode != 0:
        if re.search(rb"(?:HTTP error|Server returned|HTTP/\S+)\s+(?:401|403|429|451)\b",
                     completed.stderr or b"", re.I):
            return dict(result, status="blocked", reason="音频请求遇到授权、地区限制或限流")
        return dict(result, status="failed", reason="未完成音频流解码，需家庭网络复核")
    return dict(result, status="decoded", reason="音频流完成 4 秒短时解码")


def probe_movie(entry, ffmpeg):
    video = probe_video(entry, ffmpeg)
    result = {"id": line_id(entry), "name": entry["name"],
              "group": entry.get("group", "电影候选"),
              "video_status": video["status"], "audio_status": "not_checked"}
    if video["status"] != "decoded":
        return dict(result, status=video["status"],
                    reason="视频未通过：" + video.get("reason", "未完成视频解码"))
    audio = probe_audio(entry, ffmpeg)
    result["audio_status"] = audio["status"]
    if audio["status"] == "decoded":
        return dict(result, status="decoded",
                    reason="视频与音频均完成短时解码；未核验实际节目身份和长期稳定性")
    return dict(result, status=audio["status"],
                reason="视频已通过，但音频未通过：" + audio.get("reason", "未知原因"))


def scan_entries(selected, ffmpeg, retry_limit=4):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda entry: probe_movie(entry, ffmpeg), selected))
        retry_indices = [index for index, result in enumerate(results)
                         if result["status"] == "failed"][:retry_limit]
        retried = list(pool.map(lambda index: probe_movie(selected[index], ffmpeg),
                                retry_indices))
    for index, result in zip(retry_indices, retried):
        results[index] = dict(result, attempts=2, initial_status="failed")
    return results


def run_scan(entries, history, ffmpeg, settings, aliases=None,
             limit=12, retry_limit=4, now=None):
    if not 1 <= limit <= 24 or not 0 <= retry_limit <= 8:
        raise ValueError("电影频道抽检必须限制在每日 24 条、失败复测 8 条以内")
    aliases = aliases if aliases is not None else load_aliases()
    now = now or datetime.now(timezone.utc)
    rotation = int(now.timestamp()) // 86400
    selected = select_movie_entries(entries, history, settings, aliases, limit, rotation)
    results = scan_entries(selected, ffmpeg, retry_limit)
    checked_at = now.isoformat(timespec="seconds")
    record_results(history, results, checked_at)
    counts = dict(Counter(result["status"] for result in results))
    always = set(settings.get("daily_scan", {}).get("always_check", []))
    family_names = set(settings.get("channels", [])) | always
    return {
        "checked_at": checked_at,
        "mode": "daily_movie_targeted",
        "environment": "GitHub 标准 Linux 云端",
        "catalog_entries": len({line_id(entry) for entry in entries
                                if entry.get("url") and is_movie_candidate(entry.get("name", ""), settings, aliases)}),
        "sampled": len(results),
        "attempts": sum(result.get("attempts", 1) for result in results),
        "counts": counts,
        "family_channels_tested": sorted({r["name"] for r in results} & family_names),
        "candidate_channels_tested": sorted({r["name"] for r in results} - family_names),
        "boundary": "视频与音频分别进行短时解码；不识别电影片名或节目身份，不证明家庭网络可用、版权许可或长期稳定。",
        "results": results,
    }


def load_history(path):
    if path.exists():
        history = json.loads(path.read_text(encoding="utf-8"))
        if history.get("version") != 1 or not isinstance(history.get("lines"), dict):
            raise ValueError("电影频道健康历史格式不兼容，停止覆盖")
        return history
    return {"version": 1, "lines": {}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-candidates", action="store_true",
                        help="同时抽检本轮发现、尚未进入正式目录的电影候选")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--retry-limit", type=int, default=None)
    args = parser.parse_args()

    settings = load_settings()
    daily = settings.get("daily_scan", {})
    limit = args.limit if args.limit is not None else daily.get("limit", 12)
    retry_limit = args.retry_limit if args.retry_limit is not None else daily.get("retry_limit", 4)
    if not 1 <= limit <= 24 or not 0 <= retry_limit <= 8:
        parser.error("电影频道每日首测最多 24 条、失败复测最多 8 条")

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("缺少 FFmpeg，不能生成电影频道抽检报告")
    public = ROOT / "public"
    live_entries = parse_live((public / "live.m3u").read_text(encoding="utf-8"))
    family = json.loads((ROOT / "family_selection.json").read_text(encoding="utf-8"))
    always = set(daily.get("always_check", []))
    preferred = [dict(entry, family_priority=True)
                 for entry in settings.get("fixed_entries", [])]
    preferred.extend(dict(entry, family_priority=True)
                     for entry in family.get("fixed_entries", [])
                     if entry.get("name") in always)
    entries = preferred + live_entries

    candidate_path = ROOT / "state" / "candidate_entries.json"
    if args.include_candidates and candidate_path.is_file():
        candidates = json.loads(candidate_path.read_text(encoding="utf-8"))
        if not isinstance(candidates, list):
            raise ValueError("电影候选队列格式错误")
        entries.extend(candidates)

    history_path = public / "movie-health-history.json"
    history = load_history(history_path)
    report = run_scan(entries, history, ffmpeg, settings, limit=limit,
                      retry_limit=retry_limit)
    write_json(history_path, history)
    write_json(public / "movie-health.json", report)

    counts = report["counts"]
    lines = [
        "# 每日电影频道定向音视频抽检", "",
        "检测时间：" + report["checked_at"], "",
        report["boundary"], "",
        "候选目录 " + str(report["catalog_entries"]) + " 条；本次抽检 "
        + str(report["sampled"]) + " 条；总探测 " + str(report["attempts"]) + " 次。",
        "视频与音频均通过 " + str(counts.get("decoded", 0)) + " 条；失败 "
        + str(counts.get("failed", 0)) + " 条；受限 "
        + str(counts.get("blocked", 0)) + " 条；跳过 "
        + str(counts.get("skipped", 0)) + " 条。", "",
        "| 频道 | 分组 | 视频 | 音频 | 综合状态 | 说明 |",
        "|---|---|---|---|---|---|"
    ]
    for result in report["results"]:
        name = result["name"].replace("|", "／").replace("\n", " ")
        group = result.get("group", "").replace("|", "／")
        reason = result.get("reason", "").replace("|", "／").replace("\n", " ")
        lines.append("| " + name + " | " + group + " | "
                     + result.get("video_status", "not_checked") + " | "
                     + result.get("audio_status", "not_checked") + " | "
                     + result["status"] + " | " + reason + " |")
    (public / "movie-health.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"sampled": report["sampled"], "counts": counts,
                      "family_channels_tested": report["family_channels_tested"],
                      "candidate_channels_tested": report["candidate_channels_tested"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
