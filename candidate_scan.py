# 2026/10/23 14:23:05 by liaoweipeng1
"""有限额检测候选；启用的地址短时视频解码通过后自动加入原订阅。"""
from collections import Counter
from datetime import datetime, timezone
import ipaddress
import json
from urllib.parse import urlsplit
import shutil

from health_tracking import record_results
from network_policy import is_ipv6_literal
from scan_live import scan_entries
from update import ROOT
from auto_additions import publish_scan
from visual_check import check as check_visual, target as logo_target


def allowed_candidate(entry):
    """限制自动探测为公网 HTTP(S)，避免把公开列表用作内网探测器。"""
    url = entry.get("url", "")
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username or parsed.password or "|" in url or any(c in url for c in "\r\n"):
            return False
        host = parsed.hostname.lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(
                (".localhost", ".local", ".internal")):
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            # 公共域名也可能 DNS 解析到私网：应仅在可信隔离 Runner 上运行。
            return True
        return ip.is_global and ip.version == 4
    except ValueError:
        return False


def choose_candidates(entries, history, limit, day):
    """有限额轮换候选，不为单个地方频道固定预留检测名额。"""
    candidates = {}
    for e in entries:
        if not allowed_candidate(e) or is_ipv6_literal(e):
            continue
        candidates.setdefault(e["id"], e)

    # 补缺频道使用一半目标池；地方频道参与普通轮换。
    # 正式订阅的展示顺序不影响探测名额。
    target_pool = [e for e in candidates.values()
                   if e.get("priority") == "target_missing_channel"]
    important_pool = [e for e in candidates.values()
                      if e.get("priority") != "target_missing_channel"
                      and e["group"] in {"央视", "卫视"}]
    other_pool = [e for e in candidates.values()
                  if e.get("priority") != "target_missing_channel"
                  and e["group"] not in {"央视", "卫视"}]

    def oldest(e):
        observations = history.get("lines", {}).get(e["id"], {}).get("observations", [])
        return observations[-1].get("at", "") if observations else ""

    def pick(items, count):
        ordered = sorted(items, key=lambda e: (oldest(e), e["id"]))
        if ordered:
            missing = [e for e in ordered if not oldest(e)]
            known = [e for e in ordered if oldest(e)]
            if missing:
                index = day % len(missing)
                missing = missing[index:] + missing[:index]
            ordered = missing + known
        return ordered[:count]

    targeted = pick(target_pool, round(limit * 0.5))
    remaining = limit - len(targeted)
    important = pick(important_pool, round(remaining * 0.75))
    others = pick(other_pool, remaining - len(important))
    chosen = {e["id"] for e in targeted + important + others}
    if len(chosen) < limit:
        rest = [e for e in candidates.values() if e["id"] not in chosen]
        others += pick(rest, limit - len(chosen))
    return targeted + important + others


def run_scan(entries, history, ffmpeg, limit=12, retry_limit=4, now=None, visual_limit=3):
    if not 1 <= limit <= 24 or not 0 <= retry_limit <= 8 or not 0 <= visual_limit <= 3:
        raise ValueError("候选探测必须有限额")
    now = now or datetime.now(timezone.utc)
    checked_at = now.isoformat(timespec="seconds")
    # 错台证据独立于最多 2400 条的滚动健康历史保存，未测和未知不能解除拦截。
    holds = history.setdefault("visual_holds", {})
    day = int(now.timestamp()) // 86400
    selected = choose_candidates(entries, history, limit, day)
    results = scan_entries(selected, ffmpeg, retry_limit=retry_limit)
    # 每次最多三条成功候选进行左上台标双帧 OCR；截图只在临时目录存在。
    visual_candidates = []
    for index, (entry, result) in enumerate(zip(selected, results)):
        if result["status"] != "decoded":
            continue
        parsed = logo_target(entry["name"])
        result["visual_status"] = "not_checked_budget" if parsed else "unsupported"
        if parsed:
            # CCTV 可直接识别数字编号；卫视常为抽象图形 Logo，文字 OCR 识别率较低。
            priority = 1 if parsed[0] == "cctv" else 2
            if entry["id"] in holds:
                priority = 0
            visual_candidates.append((priority, index, entry, result))
    visual_candidates.sort(key=lambda row: (row[0], row[1]))
    for _, _, entry, result in visual_candidates[:visual_limit]:
        result.update(check_visual(entry, ffmpeg))
    for result in results:
        key = result["id"]
        if result.get("visual_status") == "mismatch":
            holds[key] = {"name": result["name"], "checked_at": checked_at}
        elif result["status"] == "decoded" and result.get("visual_status") == "match":
            holds.pop(key, None)
        result["visual_blocked"] = key in holds
    visual_checked = min(len(visual_candidates), visual_limit)
    visual_counts = Counter(r["visual_status"] for r in results
                            if r["status"] == "decoded")
    record_results(history, results, checked_at)
    # 限制长期积累的数据规模；候选源的失效与变动不影响正式源历史。
    sorted_items = sorted(history["lines"].items(), key=lambda pair:
                          pair[1].get("observations", [{}])[-1].get("at", ""))
    history["lines"] = dict(sorted_items[-2400:])
    counts = dict(Counter(result["status"] for result in results))
    enabled = {entry["id"] for entry in selected if entry.get("source_mode") == "decode_then_publish"}
    return {
        "checked_at": checked_at,
        "mode": "candidate_decode_then_publish",
        "sampled": len(selected),
        "attempts": sum(r.get("attempts", 1) for r in results),
        "counts": counts,
        "visual_checked": visual_checked,
        "visual_counts": dict(visual_counts),
        "visual_holds_total": len(holds),
        "visual_note": "每轮最多3条候选做 OCR；疑似错台跨轮拦截，直到重新识别匹配。未知或未测不能解除旧拦截；截图不上传。",
        "eligible_for_auto_publish": sum(
            result["status"] == "decoded" and result["id"] in enabled
            and not result["visual_blocked"] for result in results),
        "rule": "启用的候选本次最终解码通过后自动加入原订阅；排除名单优先；未核对节目身份，错台由用户反馈后排除。",
        "results": [{k: result[k] for k in ("id", "name", "group", "status", "reason", "visual_status", "visual_blocked", "frame_count")
                     if k in result} for result in results],
    }


def main():
    path = ROOT / "state" / "candidate_entries.json"
    if not path.is_file():
        raise RuntimeError("没有本次成功发现的候选数据，跳过探测；未重新拉取或猜测地址")
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise RuntimeError("候选队列格式错误")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("缺少 ffmpeg")
    history_path = ROOT / "public" / "candidate-health-history.json"
    if history_path.exists():
        history = json.loads(history_path.read_text(encoding="utf-8"))
        if history.get("version") != 1 or not isinstance(history.get("lines"), dict):
            raise ValueError("候选健康历史格式不兼容")
    else:
        history = {"version": 1, "lines": {}}
    report = run_scan(entries, history, ffmpeg)
    report = publish_scan(entries, report, history)
    print(json.dumps({k: report[k] for k in ("sampled", "attempts", "counts", "promoted")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
