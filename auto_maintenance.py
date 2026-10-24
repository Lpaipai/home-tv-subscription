# 2026/10/23 17:50:26 by liaoweipeng1
"""自动收录线路生命周期：到期优先复检、跨日两次失败才停用、成功自动恢复。"""
from collections import Counter
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import shutil
import tempfile

from health_tracking import line_id, load_history, record_results
from scan_live import scan_entries

UTC = timezone.utc
RECHECK_DAYS = 7
RETRY_GAP_DAYS = 2
SUSPEND_DAYS = 14
SUSPENDED_RETRY_DAYS = 7
MAX_RECHECK = 8
MAX_RETRIES = 2


def moment(text, fallback=None):
    try:
        value = datetime.fromisoformat(text)
        if value.tzinfo is None:
            return None
        return value.astimezone(UTC)
    except (ValueError, TypeError):
        return fallback


def decoded_at(entry):
    # 公开 IPv6 线路未实播，不得把入库时间伪装成解码成功时间。
    if entry.get("admission") == "ipv6_unverified":
        return None
    return moment(entry.get("last_decoded_at")) or moment(entry.get("accepted_at"))


def state_at(entry, now):
    if entry.get("admission") == "ipv6_unverified":
        return "unverified"
    if entry.get("state") == "suspended":
        return "suspended"
    last = decoded_at(entry)
    if last is None or now - last >= timedelta(days=RECHECK_DAYS):
        return "probation" if entry.get("failure_days") else "due"
    return "active"


def due(entries, now, limit=MAX_RECHECK):
    """未实测不会直接淘汰；停用地址定期尝试恢复。"""
    if limit < 0 or limit > MAX_RECHECK:
        raise ValueError("复检预算超限")
    pools = {"live": [], "suspended": []}
    for entry in entries:
        # 标准 GitHub Runner 无 IPv6 连通性：此类线路等待家庭网络验证，
        # 不参与复检、失败计数或暂停判断，也不浪费有限额探测名额。
        if entry.get("admission") == "ipv6_unverified":
            continue
        last_attempt = moment(entry.get("last_attempt_at"))
        last_ok = decoded_at(entry)
        interval = SUSPENDED_RETRY_DAYS if entry.get("state") == "suspended" else RETRY_GAP_DAYS
        if last_attempt and now - last_attempt < timedelta(days=interval):
            continue
        if entry.get("state") == "suspended" or last_ok is None or now - last_ok >= timedelta(days=RECHECK_DAYS):
            pool = "suspended" if entry.get("state") == "suspended" else "live"
            # 池内按最近尝试时间轮换，避免同一批受限/失败地址反复占满名额。
            pools[pool].append((last_attempt.timestamp() if last_attempt else float("-inf"),
                                last_ok.timestamp() if last_ok else float("-inf"),
                                line_id(entry), entry))
    for pool in pools.values():
        pool.sort(key=lambda row: row[:3])
    # 两类各保留一半；奇数名额按 UTC 日交替，空余名额可借给另一类。
    day = int(now.timestamp()) // 86400
    live_limit = limit // 2 + (limit % 2 if day % 2 == 0 else 0)
    quotas = {"live": live_limit, "suspended": limit - live_limit}
    chosen = []
    spare = []
    for name, pool in pools.items():
        quota = quotas[name]
        chosen.extend(pool[:quota])
        spare.extend(pool[quota:])
    spare.sort(key=lambda row: row[:3])
    chosen.extend(spare[:limit - len(chosen)])
    return [row[3] for row in chosen]


def apply_observations(ledger, results, now):
    """blocked/skipped 不扣分；连续失败须为至少两个不同 UTC 日。"""
    updated = []
    for result in results:
        entry = ledger["entries"].get(result["id"])
        if not entry:
            continue
        entry["last_attempt_at"] = now.isoformat(timespec="seconds")
        status = result["status"]
        entry["last_attempt_status"] = status
        if entry.get("admission") == "ipv6_unverified" and status != "decoded":
            entry["state"] = "unverified"
            continue
        if status == "decoded":
            entry["admission"] = "video_decoded"
            entry["last_decoded_at"] = entry["last_attempt_at"]
            entry["failure_days"] = []
            entry["state"] = "active"
        elif status == "failed":
            days = entry.get("failure_days") or []
            today = now.date().isoformat()
            if today not in days:
                days.append(today)
            entry["failure_days"] = days[-5:]
            last_ok = decoded_at(entry)
            if len(days) >= 2 and (last_ok is None or now-last_ok >= timedelta(days=SUSPEND_DAYS)):
                entry["state"] = "suspended"
            else:
                entry["state"] = "probation"
        else:
            # 不可访问并非已经确认的失效；保留已有证据和状态。
            entry["state"] = state_at(entry, now)
        updated.append(entry["state"])
    for entry in ledger["entries"].values():
        entry["state"] = state_at(entry, now)
    return Counter(updated)


def sync_successful_formal_checks(ledger, history):
    """正式抽检成功时续期，避免反复复检或误淘汰近期已成功解码的自动源。"""
    renewed = 0
    for key, entry in ledger["entries"].items():
        item = history.get("lines", {}).get(key, {})
        successes = [moment(x.get("at")) for x in item.get("observations", [])
                     if x.get("status") == "decoded"]
        successes = [time for time in successes if time is not None]
        newest = max(successes, default=None)
        if newest and (decoded_at(entry) is None or newest > decoded_at(entry)):
            entry["admission"] = "video_decoded"
            entry["last_decoded_at"] = newest.isoformat(timespec="seconds")
            entry["last_attempt_at"] = newest.isoformat(timespec="seconds")
            entry["last_attempt_status"] = "decoded"
            entry["failure_days"] = []
            entry["state"] = "active"
            renewed += 1
    return renewed


def recheck(ledger, ffmpeg, now=None, limit=MAX_RECHECK, retry_limit=MAX_RETRIES):
    now = now or datetime.now(UTC)
    selected = due(ledger["entries"].values(), now, limit)
    results = scan_entries(selected, ffmpeg, retry_limit) if selected else []
    changed = apply_observations(ledger, results, now)
    return {
        "checked_at": now.isoformat(timespec="seconds"),
        "policy": {"recheck_days": RECHECK_DAYS, "suspend_days": SUSPEND_DAYS,
                   "min_separate_failed_days": 2, "suspended_recheck_days": SUSPENDED_RETRY_DAYS,
                   "max_initial": MAX_RECHECK, "max_retry": MAX_RETRIES,
                   "selection": "live_and_suspended_split_budget_oldest_attempt_first"},
        "sampled": len(selected),
        "attempts": sum(r.get("attempts", 1) for r in results),
        "counts": dict(Counter(r["status"] for r in results)),
        "state_changes": dict(changed),
        "suspended_total": sum(e.get("state") == "suspended" for e in ledger["entries"].values()),
        "rule": "两不同日期失败、距最近成功超过14天才停用；未抽到的源不因过期直接删除；受限或跳过不计失败；停用源可自动恢复。",
        "results": [{key: r[key] for key in ("id", "name", "status", "reason", "attempts") if key in r}
                    for r in results],
    }


def publish_recheck(report, ledger, results):
    """在副本中同时更新来源记录、直播与健康数据；原子切换目录。"""
    import update
    from auto_additions import active_additions, align_regions
    from channel_catalog import organize, sort_entries

    root = update.ROOT
    public = root / "public"
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    settings = json.loads((root / "channel_names.json").read_text(encoding="utf-8"))
    current = [e for e in update.parse_live((public / "live.m3u").read_text(encoding="utf-8"))
               if e["group"] != "家庭精选"]
    # M3U 本身不包含来源溯源；重新渲染时必须从上一份 line-origins 恢复，
    # 否则会无声删除全目录的上游来源记录和 Nexus 独有源统计。
    origins = json.loads((public / "line-origins.json").read_text(encoding="utf-8"))
    for entry in current:
        entry["origins"] = origins.get(line_id(entry), {}).get("sources", [])
    ledger_ids = set(ledger["entries"])
    baseline = [entry for entry in current if line_id(entry) not in ledger_ids]
    auto = align_regions(active_additions(ledger, config), baseline, settings)
    merged = sort_entries(baseline + organize(auto, settings), settings)
    merged = update.exclude_entries(merged, config.get("live_exclusions", []))
    history = load_history(public)
    record_results(history, results, report["checked_at"])
    status = json.loads((public / "status.json").read_text(encoding="utf-8"))
    status["auto_additions"] = len(auto)
    status["auto_additions_ipv6_unverified"] = sum(
        item.get("admission") == "ipv6_unverified" for item in auto)
    status["auto_suspended"] = report["suspended_total"]
    status["auto_recheck_at"] = report["checked_at"]
    with tempfile.TemporaryDirectory(prefix="build-rechecks-", dir=root) as folder:
        out = Path(folder)
        shutil.copytree(public, out, dirs_exist_ok=True)
        update.write_json(out / "auto-additions.json", ledger)
        update.write_json(out / "auto-maintenance.json", report)
        update.write_live_outputs(merged, history, config, out, status)
        update.write_json(out / "status.json", status)
        update.publish_directory(out)


def main():
    import update
    from auto_additions import load_additions
    ledger = load_additions(update.ROOT / "public")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg 不存在")
    now = datetime.now(UTC)
    official_history = load_history(update.ROOT / "public")
    renewed = sync_successful_formal_checks(ledger, official_history)
    report = recheck(ledger, ffmpeg, now)
    report["renewed_by_formal_checks"] = renewed
    # 即使本次没有到期线路，也同步正式抽检续期证据；始终以完整目录副本原子发布。
    results = [{**r} for r in report["results"]]
    publish_recheck(report, ledger, results)
    print(json.dumps({"sampled": report["sampled"], "counts": report["counts"],
                      "suspended": report["suspended_total"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
