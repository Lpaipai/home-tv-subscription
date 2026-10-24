# 2026/10/23 14:23:05 by liaoweipeng1
"""保存解码通过的候选线路，并原子更新同一个正式订阅。"""
import copy
import json
from pathlib import Path
import shutil
import tempfile

from channel_catalog import REGIONS, organize, sort_entries
from health_tracking import line_id, load_history, record_results
from network_policy import is_public_ipv6_url

MAX_IPV6_PROVISIONAL_PER_SOURCE = 128


def load_additions(public):
    path = public / "auto-additions.json"
    if not path.exists():
        return {"version": 1, "entries": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("entries"), dict):
        raise ValueError("自动收录记录格式不兼容，停止覆盖")
    for key, entry in data["entries"].items():
        if key != line_id(entry):
            raise ValueError("自动收录记录线路标识不匹配")
        admission = entry.get("admission")
        if admission == "video_decoded":
            continue
        if (admission != "ipv6_unverified" or not is_public_ipv6_url(entry.get("url"))
                or entry.get("directives") or entry.get("state") != "unverified"
                or not entry.get("origins")):
            raise ValueError("自动收录记录缺少可信发布依据")
    return data


def active_additions(ledger, config):
    excluded = {rule["url"] for rule in config.get("live_exclusions", [])}
    return [copy.deepcopy(entry) for entry in ledger["entries"].values()
            if entry["url"] not in excluded and entry.get("state") != "suspended"]


def align_regions(entries, existing, settings):
    """混合国家目录中的同名频道沿用正式目录的唯一分类，不猜测歧义频道。"""
    groups = {}
    for entry in existing:
        if entry["group"] != "家庭精选":
            groups.setdefault(entry["name"], set()).add(entry["group"])
    regions = {group: code for code, group in REGIONS.items()}
    aligned = copy.deepcopy(entries)
    for entry in aligned:
        name = organize([entry], settings)[0]["name"]
        matches = groups.get(name, set())
        if len(matches) == 1:
            entry["region"] = regions.get(next(iter(matches)), "cn")
    return aligned


def select_additions(entries, report, ledger, existing, config):
    """只接受本轮最终解码成功的已启用候选，禁止按旧分数或 HTTP 在线标签收录。"""
    by_id = {line_id(entry): entry for entry in entries}
    known = {line_id(entry) for entry in existing} | set(ledger["entries"])
    excluded = {rule["url"] for rule in config.get("live_exclusions", [])}
    additions = []
    for result in report["results"]:
        entry = by_id.get(result["id"])
        if (result["status"] != "decoded" or entry is None
                or entry.get("source_mode") != "decode_then_publish"
                or result["id"] in known or entry["url"] in excluded
                or result.get("visual_status") == "mismatch"
                or result.get("visual_blocked", False)):
            continue
        if entry.get("id") != line_id(entry) or not entry.get("source_url"):
            raise ValueError("候选缺少对应地址标识或来源，不能自动收录")
        item = {"name": entry["name"], "url": entry["url"],
                "directives": copy.deepcopy(entry.get("directives", [])),
                "attrs": copy.deepcopy(entry.get("attrs", {})),
                "group": entry.get("original_group", entry["group"]),
                "source": "candidate-" + entry["source_id"],
                "region": entry.get("region", "cn"),
                "origins": [entry["source_url"]],
                "admission": "video_decoded", "accepted_at": report["checked_at"],
                "content_verified": False,
                "visual_status": result.get("visual_status", "not_checked"),
                "last_decoded_at": report["checked_at"],
                "last_attempt_at": report["checked_at"],
                "last_attempt_status": "decoded", "failure_days": [],
                "state": "active"}
        additions.append(item)
        known.add(result["id"])
    return additions


def select_ipv6_provisional(entries, ledger, existing, config, approved_sources=(), held_ids=()):
    """仅对配置显式授权的公开 IPv6 字面量免云端检测发布。

    这是家庭网络待验证的备用源，不伪造 FFmpeg 成功记录，也不占探测预算。
    每个授权来源单轮最多 128 条，避免外部列表异常膨胀。
    """
    approved = set(approved_sources)
    blocked = set(held_ids)
    excluded = {rule["url"] for rule in config.get("live_exclusions", [])}
    known = {line_id(entry) for entry in existing} | set(ledger["entries"])
    selected = []
    counts = {}
    for entry in entries:
        source = (entry.get("source_id"), entry.get("source_url"))
        if (source not in approved or entry.get("ipv6_direct_publish") is not True
                or entry.get("source_mode") != "decode_then_publish"
                or not is_public_ipv6_url(entry.get("url"))
                or entry.get("directives") or entry.get("id") != line_id(entry)
                or entry["id"] in known or entry["id"] in blocked
                or entry["url"] in excluded):
            continue
        if counts.get(source, 0) >= MAX_IPV6_PROVISIONAL_PER_SOURCE:
            continue
        item = {"name": entry["name"], "url": entry["url"],
                "directives": [], "attrs": copy.deepcopy(entry.get("attrs", {})),
                "group": entry.get("original_group", entry["group"]),
                "source": "candidate-" + entry["source_id"],
                "region": entry.get("region", "cn"),
                "origins": [entry["source_url"]],
                "admission": "ipv6_unverified", "content_verified": False,
                "visual_status": "not_checked_ipv6",
                "state": "unverified", "failure_days": []}
        selected.append(item)
        known.add(entry["id"])
        counts[source] = counts.get(source, 0) + 1
    return selected


def publish_scan(entries, report, candidate_history):
    # 延迟导入：update 只依赖本模块的记录读取函数。
    import update

    root = update.ROOT
    public = root / "public"
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    settings = json.loads((root / "channel_names.json").read_text(encoding="utf-8"))
    ledger = load_additions(public)
    current = [entry for entry in update.parse_live((public / "live.m3u").read_text(encoding="utf-8"))
               if entry["group"] != "家庭精选"]
    decoded = select_additions(entries, report, ledger, current, config)
    catalog = json.loads((root / "candidate_sources.json").read_text(encoding="utf-8"))
    approved_ipv6 = {(src["id"], src["url"]) for src in catalog["sources"]
                     if src.get("ipv6_direct_publish") is True
                     and src.get("mode") == "decode_then_publish"}
    ipv6_provisional = select_ipv6_provisional(
        entries, ledger, current + decoded, config, approved_ipv6,
        candidate_history.get("visual_holds", {}))
    additions = decoded + ipv6_provisional
    baseline = [entry for entry in current if line_id(entry) not in ledger["entries"]]
    additions = align_regions(additions, baseline, settings)
    for entry in additions:
        ledger["entries"][line_id(entry)] = entry
    report = dict(report, eligible_for_auto_publish=len(decoded), promoted=len(additions),
                  promoted_decoded=len(decoded),
                  promoted_ipv6_unverified=len(ipv6_provisional),
                  promoted_ids=[line_id(entry) for entry in additions],
                  visual_mismatch_blocked=sum(r.get("visual_status") == "mismatch"
                                              or r.get("visual_blocked", False)
                                              for r in report["results"]))

    # 在完整副本中完成所有产物，失败时不留下“记录已加入、订阅未更新”的半成品。
    with tempfile.TemporaryDirectory(prefix="build-candidates-", dir=root) as folder:
        out = Path(folder)
        shutil.copytree(public, out, dirs_exist_ok=True)
        update.write_json(out / "candidate-health-history.json", candidate_history)
        update.write_json(out / "candidate-health.json", report)
        update.write_json(out / "auto-additions.json", ledger)
        if additions:
            origins = json.loads((public / "line-origins.json").read_text(encoding="utf-8"))
            for entry in current:
                entry["origins"] = origins.get(line_id(entry), {}).get("sources", [])
            normalized = organize(additions, settings)
            # 新地址已按线路标识过滤；保留原目录同址跨分类展示的每一项。
            merged = sort_entries(current + normalized, settings)
            merged = update.exclude_entries(merged, config.get("live_exclusions", []))
            history = load_history(public)
            promoted_ids = set(report["promoted_ids"])
            record_results(history, [result for result in report["results"]
                                     if result["status"] == "decoded"
                                     and result["id"] in promoted_ids], report["checked_at"])
            status = json.loads((public / "status.json").read_text(encoding="utf-8"))
            active = active_additions(ledger, config)
            status["auto_additions"] = len(active)
            status["auto_additions_ipv6_unverified"] = sum(
                item.get("admission") == "ipv6_unverified" for item in active)
            status["auto_additions_updated_at"] = report["checked_at"]
            update.write_live_outputs(merged, history, config, out, status)
            update.write_json(out / "status.json", status)
        update.publish_directory(out)
    return report
