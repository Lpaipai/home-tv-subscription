# 2026/10/23 14:23:05 by liaoweipeng1
"""发现候选来源；未测地址暂存，交由有限额解码检测决定自动收录。"""
import json
import re
from pathlib import Path

from health_tracking import line_id
from network_policy import network_type
from update import ROOT, STATE, fetch, parse_live, write_json, stamp

# 本次扩充的 HTV 缺口；只对定向候选表应用精确别名，不猜测节目身份。
TARGET_ALIASES = {
    "广东深圳都市": "深圳都市",
    "广东深圳电视剧": "深圳电视剧",
    "广东深圳少儿": "深圳少儿",
    "深圳都市频道": "深圳都市",
    "深圳电视剧频道": "深圳电视剧",
    "深圳少儿频道": "深圳少儿",
    "广东大湾区": "大湾区卫视",
    "广东大湾区卫视": "大湾区卫视",
    "CCTV16-4K": "CCTV-16 4K",
    "CCTV-16-4K": "CCTV-16 4K",
    "广西桂林综合": "桂林新闻综合",
    "广西桂林新闻综合": "桂林新闻综合",
    "广西桂林科教": "桂林科教旅游",
    "广西桂林科教旅游": "桂林科教旅游",
    "桂林新闻": "桂林新闻综合",
    "桂林综合": "桂林新闻综合",
    "桂林科教": "桂林科教旅游",
    "灌阳新闻": "灌阳新闻综合",
    "广西灌阳新闻": "灌阳新闻综合",
    "广西灌阳新闻综合": "灌阳新闻综合",
    "广西综艺": "广西综艺旅游",
    "「广西」灌阳新闻综合": "灌阳新闻综合",
}
HTV_GAPS = {
    "CCTV-16 4K", "国学频道", "海南卫视", "亚洲新闻",
    "广东珠江", "广东新闻", "广东影视", "广东民生",
    "广东少儿", "大湾区卫视", "深圳都市", "深圳电视剧", "深圳少儿",
}

FEATURED_LOCAL_CHANNEL = "灌阳新闻综合"
REGIONAL_CHANNELS = {
    "桂林新闻综合", "桂林科教旅游", "广西新闻", "广西都市",
    "广西影视", "广西国际", "广西乐思购", "广西移动数字电视",
    "广西综艺旅游", "广西卫视", "资源电视台",
}


def canonical_target_name(name):
    """仅对指定来源的别名处理清晰度后缀，避免污染其它频道。"""
    name = re.sub(r"\[\d+\s*[xX*×]\s*\d+\]$", "", name.strip()).strip()
    return TARGET_ALIASES.get(name, name)


def candidate_priority(name):
    if name == FEATURED_LOCAL_CHANNEL:
        return "featured_local_channel"
    if name in REGIONAL_CHANNELS:
        return "regional_channel"
    if name in HTV_GAPS:
        return "target_missing_channel"
    if important_name(name):
        return "domestic_channel_candidate"
    return "other"


def important_name(name):
    # 只做人工复核优先级判断，不把猜测当成官方台身份。
    compact = name.upper().replace(" ", "").replace("-", "")
    return compact.startswith("CCTV") or "卫视" in name


def summarize_candidates(catalog, existing_entries, downloader, with_entries=False, excluded_urls=()):
    existing = {line_id(e) for e in existing_entries}
    found, sources, failures, candidate_by_id = {}, [], [], {}
    for source in catalog["sources"]:
        if source.get("mode") not in {"review_only", "decode_then_publish"} or source.get("type") != "community":
            raise ValueError("候选目录只允许先发现再检测，不能无条件发布")
        try:
            data, meta = downloader(source["url"], lambda s: parse_live(s, source["name"]))
        except Exception as exc:
            failures.append({"id": source["id"], "error": str(exc)[:350]})
            continue
        sources.append({
            "id": source["id"], "items": len(data),
            "cached": meta.get("cached", False),
            "rights": source["rights"], "mode": source["mode"],
            "homepage": source["homepage"]})
        include = set(source.get("include_channels", []))
        for e in data:
            if e["url"] in excluded_urls:
                continue
            if include:
                canonical = canonical_target_name(e["name"])
                if canonical not in include:
                    continue
                e = dict(e, name=canonical)
            key = line_id(e)
            row = found.setdefault(key, {
                "line_id": key, "name": e["name"],
                "network": network_type(e["url"]), "already_in_main": key in existing,
                "priority": candidate_priority(e["name"]),
                "review": "pending_video_decode_content_unverified",
                "sources": []})
            if source["id"] not in row["sources"]:
                row["sources"].append(source["id"])
            if key not in existing and key not in candidate_by_id:
                # 地址仅存放在被 gitignore 排除的 state/，不写入公开候选报告。
                candidate_by_id[key] = {
                    "id": key, "name": e["name"], "url": e["url"],
                    "attrs": e.get("attrs", {}), "original_group": e["group"],
                    "directives": e.get("directives", []),
                    "group": "央视" if e["name"].upper().replace("-", "").startswith("CCTV")
                             else "卫视" if "卫视" in e["name"] else
                             "地方" if e["name"] in HTV_GAPS
                             or e["name"] in REGIONAL_CHANNELS
                             or e["name"] == FEATURED_LOCAL_CHANNEL else "其他",
                    "source_id": source["id"], "source_url": source["url"],
                    "source_mode": source["mode"], "region": source.get("region", "cn"),
                    "ipv6_direct_publish": source.get("ipv6_direct_publish") is True,
                    "priority": row["priority"]
                }
            # 同一 URL 同时出现于普通来源与经明确许可的 IPv6 来源时，
            # 合并发布资格并记录真正授权进入待验证池的来源。
            if (key in candidate_by_id and source.get("ipv6_direct_publish") is True
                    and network_type(e["url"]) == "ipv6_literal"):
                candidate_by_id[key]["ipv6_direct_publish"] = True
                candidate_by_id[key]["source_id"] = source["id"]
                candidate_by_id[key]["source_url"] = source["url"]
    values = sorted(found.values(), key=lambda a: (
        a["already_in_main"],
        {"featured_local_channel": 0, "regional_channel": 1,
         "target_missing_channel": 2, "domestic_channel_candidate": 3}.get(a["priority"], 4),
        a["network"] == "ipv6_literal", a["name"], a["line_id"]))
    report = {
        "fetched_at": stamp(),
        "mode": "discovery_before_decode",
        "disclaimer": "未测候选仅公开指纹；启用自动收录的地址解码通过后加入原订阅，不代表节目身份已核对。",
        "source_summaries": sources,
        "failures": failures,
        "total_unique": len(values),
        "not_in_main": sum(not e["already_in_main"] for e in values),
        "ipv6_literal": sum(e["network"] == "ipv6_literal" for e in values),
        "priority_unlisted": sum(e["priority"] in {"domestic_channel_candidate",
                                "target_missing_channel", "regional_channel",
                                "featured_local_channel"} and not e["already_in_main"] for e in values),
        "preview": values[:160],
    }
    return (report, list(candidate_by_id.values())) if with_entries else report


def main():
    catalog = json.loads((ROOT / "candidate_sources.json").read_text(encoding="utf-8"))
    from update import parse_live
    existing = parse_live((ROOT / "public" / "live.m3u").read_text(encoding="utf-8"))
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    excluded = {rule["url"] for rule in config.get("live_exclusions", [])}
    report, entries = summarize_candidates(catalog, existing, fetch, with_entries=True, excluded_urls=excluded)
    if not report["source_summaries"]:
        raise RuntimeError("候选源全部不可用，不覆盖已有候选报告")
    write_json(STATE / "candidate_entries.json", entries)
    write_json(ROOT / "public" / "source-review.json", report)
    print(json.dumps({k: report[k] for k in
                     ("total_unique", "not_in_main", "ipv6_literal", "priority_unlisted")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
