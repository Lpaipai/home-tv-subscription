# 2026/10/22 23:40:00 by liaoweipeng1
"""有限额的云端直播抽检与同频道排序；不删源或认定频道内容。"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess

from update import ROOT, parse_live, render_live, stamp, write_json
from health_tracking import line_id, load_history, rank_entries, record_results
from network_policy import is_ipv6_literal, network_type
from curation import build_family_outputs, family_group_entries


def select_entries(entries, limit, rotation, important_ratio=0.5, history=None):
    """按频道轮换抽检：央视/卫视优先，每个频道先选一条，再轮到备用线路。

    相同 URL+请求头只探测一次；同频道按最后尝试时间选择长期未测线路。
    rotation 用天数或周数，保证定时任务连续运行时更换样本。
    """
    import math

    important_groups = {"央视", "卫视"}
    unique = {}
    for entry in entries:
        # 标准云端 Runner 不假设 IPv6 连通；这些留给家庭网络验证，不能记为失败。
        if is_ipv6_literal(entry):
            continue
        key = line_id(entry)
        # 重复地址如同时被命名为央视和其他频道，保留优先级更高的记录。
        if key not in unique or (entry["group"] in important_groups
                                 and unique[key]["group"] not in important_groups):
            unique[key] = entry
    important = [e for e in unique.values() if e["group"] in important_groups]
    others = [e for e in unique.values() if e["group"] not in important_groups]
    seen_at = (history or {}).get("lines", {})

    def last_attempt(entry):
        item = seen_at.get(line_id(entry), {})
        observations = item.get("observations", [])
        # blocked/skipped 也算已尝试，但不修改用于健康排序的 checked_at。
        value = observations[-1].get("at") if observations else item.get("checked_at")
        if not value:
            return 0
        try:
            return datetime.fromisoformat(value).timestamp()
        except (ValueError, TypeError):
            return 0

    def choose(pool, count):
        groups = {}
        for entry in pool:
            groups.setdefault((entry["group"], entry["name"]), []).append(entry)
        keys = sorted(groups)
        if not keys or not count:
            return []
        # 步长与频道数量互质，避免频道数量恰好是配额倍数时一直抽中同一批。
        step = max(1, min(count, len(keys)))
        while math.gcd(step, len(keys)) != 1:
            step += 1
        offset = (rotation * step) % len(keys)
        keys = keys[offset:] + keys[:offset]
        for key in keys:
            groups[key].sort(key=lambda e: (
                last_attempt(e),
                line_id(e)))
        selected = []
        # 第一轮覆盖不同频道，后续轮次才抽同频道备用地址。
        depth = 0
        while len(selected) < count:
            added = False
            for key in keys:
                if depth < len(groups[key]):
                    selected.append(groups[key][depth])
                    added = True
                    if len(selected) == count:
                        break
            if not added:
                break
            depth += 1
        return selected

    target_important = round(limit * important_ratio)
    first = choose(important, target_important)
    second = choose(others, limit - len(first))
    if len(first) + len(second) < limit:
        selected_ids = {line_id(e) for e in first + second}
        remaining = [e for e in important + others if line_id(e) not in selected_ids]
        second += choose(remaining, limit - len(first) - len(second))
    return first + second


def playback_headers(entry):
    headers = {}
    for directive in entry.get("directives", []):
        if directive.startswith("#EXTVLCOPT:http-user-agent="):
            headers["User-Agent"] = directive.split("=", 1)[1]
        elif directive.startswith("#EXTVLCOPT:http-referrer="):
            headers["Referer"] = directive.split("=", 1)[1]
        else:
            raise ValueError("含未支持的播放参数，跳过而非判定失效")
    if any("\r" in value or "\n" in value for value in headers.values()):
        raise ValueError("请求头含换行，跳过")
    return "".join(f"{key}: {value}\r\n" for key, value in headers.items())


def probe(entry, ffmpeg):
    result = {"id": line_id(entry), "name": entry["name"], "group": entry["group"]}
    if is_ipv6_literal(entry):
        return dict(result, status="skipped", reason="IPv6 字面量地址未在本云端验证，须在家庭网络复核")
    if not entry["url"].startswith(("https://", "http://")) or "|" in entry["url"]:
        return dict(result, status="skipped", reason="此协议或地址参数不在抽检范围")
    try:
        headers = playback_headers(entry)
    except ValueError as error:
        return dict(result, status="skipped", reason=str(error))
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-threads", "1", "-rw_timeout", "5000000",
               "-protocol_whitelist", "http,https,tcp,tls,crypto"]
    if headers:
        command += ["-headers", headers]
    command += ["-i", entry["url"], "-map", "0:v:0", "-an", "-t", "4",
                "-vf", "fps=1,scale=32:18", "-threads", "1", "-pix_fmt", "gray",
                "-f", "rawvideo", "pipe:1"]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=16, check=False)
    except subprocess.TimeoutExpired:
        return dict(result, status="failed", reason="云端 16 秒内未完成采样，需家庭网络复核")
    frames = len(completed.stdout) // (32 * 18)
    if completed.returncode != 0 or frames < 3:
        if re.search(rb"(?:HTTP error|Server returned|HTTP/\S+)\s+(?:401|403|429|451)\b", completed.stderr, re.I):
            return dict(result, status="blocked", reason="云端遇到授权、地区限制或限流；不扣健康分")
        return dict(result, status="failed", frames=frames,
                    reason="云端未完成至少 3 帧解码，需家庭网络复核")
    return dict(result, status="decoded", frames=frames,
                reason="仅本次短时视频解码通过；未验证声音、台标、节目及广告")


def scan_entries(selected, ffmpeg, retry_limit=40):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda entry: probe(entry, ffmpeg), selected))
        retry_indices = [i for i, result in enumerate(results) if result["status"] == "failed"][:retry_limit]
        retried = list(pool.map(lambda i: probe(selected[i], ffmpeg), retry_indices))
    for i, result in zip(retry_indices, retried):
        results[i] = dict(result, attempts=2, initial_status="failed")
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("daily", "weekly"), default="weekly")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--retry-limit", type=int, default=None)
    args = parser.parse_args()
    max_limit, max_retries = (24, 8) if args.mode == "daily" else (120, 40)
    limit = args.limit if args.limit is not None else max_limit
    retry_limit = args.retry_limit if args.retry_limit is not None else max_retries
    if not 1 <= limit <= max_limit or not 0 <= retry_limit <= max_retries:
        parser.error(f"{args.mode} 模式首测最多 {max_limit} 条、失败复测最多 {max_retries} 条")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("未找到 FFmpeg，不能生成播放检测报告")
    all_entries = parse_live((ROOT / "public" / "live.m3u").read_text(encoding="utf-8"))
    # 精选只是原线路的另一分组，扫描时剔除复本以免占用抽检预算。
    entries = [e for e in all_entries if e["group"] != "家庭精选"]
    rotation_seconds = 86400 if args.mode == "daily" else 7 * 86400
    rotation = int(datetime.now(timezone.utc).timestamp()) // rotation_seconds
    history = load_history(ROOT / "public")
    selected = select_entries(entries, limit, rotation,
                              important_ratio=0.75 if args.mode == "daily" else 0.5,
                              history=history)
    results = scan_entries(selected, ffmpeg, retry_limit=retry_limit)
    checked_at = stamp()
    record_results(history, results, checked_at)
    write_json(ROOT / "public" / "health-history.json", history)
    ranked = rank_entries(entries, history, datetime.now(timezone.utc))
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    preview = build_family_outputs(ranked, history, config, ROOT / "public",
                                   datetime.now(timezone.utc), render_live)
    render_live(family_group_entries(preview) + ranked, ROOT / "public")
    counts = dict(Counter(result["status"] for result in results))
    report = {"checked_at": checked_at, "mode": args.mode,
              "environment": "GitHub 标准 Linux 云端",
              "catalog_entries": len(entries), "sampled": len(results), "counts": counts,
              "ipv6_literal_deferred": sum(is_ipv6_literal(e) for e in entries),
              "network_policy": "IPv6 字面量跳过云端检测，家庭测试前不作失败判定；主机名地址仍按云端实际结果记录。",
              "attempts": sum(result.get("attempts", 1) for result in results),
              "recovered_on_retry": sum(result.get("attempts") == 2 and result["status"] == "decoded" for result in results),
              "boundary": "轮换抽检；IPv6 待家庭复核；失败不等于家庭网络不可用；不识别广告或错误频道；不自动删源",
              "results": results}
    write_json(ROOT / "public" / "health.json", report)
    period = "每日重点频道" if args.mode == "daily" else "每周轮换"
    lines = [f"# 直播云端抽检（{period}）", "", f"检测时间：{report['checked_at']}", "",
             report["boundary"], "", f"目录 {len(entries)} 条；本次抽检 {len(results)} 条。",
             f"视频解码通过 {counts.get('decoded', 0)}，失败待复核 {counts.get('failed', 0)}，"
             f"受限 {counts.get('blocked', 0)}，跳过 {counts.get('skipped', 0)}。",
             f"总探测 {report['attempts']} 次；复测恢复 {report['recovered_on_retry']} 条。", "",
             "| 频道 | 线路标识 | 状态 | 说明 |", "|---|---|---|---|"]
    for result in results:
        name = result["name"].replace("|", "／").replace("\n", " ")
        lines.append(f"| {name} | {result['id']} | {result['status']} | {result['reason']} |")
    (ROOT / "public" / "health.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
