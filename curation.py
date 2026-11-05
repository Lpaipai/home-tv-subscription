# 2026/10/24 02:23:00 by liaoweipeng1
"""家庭小列表与内容人工核验报告。云端能解码并不证明节目身份正确。"""
from datetime import datetime, date
import json
from pathlib import Path
from urllib.parse import urlsplit

from health_tracking import line_id
from network_policy import network_type


def recently_decoded(entry, history, now, days=21):
    item = history.get("lines", {}).get(line_id(entry), {})
    try:
        age = (now - datetime.fromisoformat(item["checked_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= age <= days * 86400 and item.get("status") == "decoded"


def is_recent_content_check(url, additions, today, days=21):
    for item in additions:
        if item.get("url") != url or not item.get("verification"):
            continue
        try:
            age = (today.date() - date.fromisoformat(item["verified_at"])).days
        except (ValueError, TypeError, KeyError):
            continue
        if 0 <= age <= days:
            return True
    return False


def family_group_entries(selected):
    """为选定线路增加家庭精选分类，不触碰原分类和播放参数。"""
    import copy

    result = []
    seen = set()
    for entry in selected:
        key = (entry["name"], line_id(entry))
        if key in seen:
            continue
        seen.add(key)
        item = copy.deepcopy(entry)
        group = "电影" if entry.get("group") == "电影" else "家庭精选"
        item["group"] = group
        item["attrs"] = dict(item.get("attrs", {}), **{"group-title": group})
        result.append(item)
    return result


def build_family_outputs(entries, history, config, out, now, render):
    """按显式固定清单或旧有健康排序生成精选，固定清单不被上游覆盖。"""
    selections = json.loads((out.parent / "family_selection.json").read_text(encoding="utf-8"))
    names = selections["channels"]
    limit = selections["max_lines_per_channel"]
    if not isinstance(limit, int) or not 1 <= limit <= 5:
        raise ValueError("家庭单频道最多保留 1 至 5 条地址")
    mode = selections.get("mode", "health_ranked")
    if mode not in {"fixed", "health_ranked"}:
        raise ValueError("未知的家庭精选选择模式")
    fixed = mode == "fixed"
    if fixed:
        entries = selections.get("fixed_entries", [])
        if not isinstance(entries, list) or len(entries) > 1000:
            raise ValueError("固定精选必须为最多 1000 条记录的列表")
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("name") not in names:
                raise ValueError("固定精选频道必须在 channels 中声明")
            url = urlsplit(entry.get("url", ""))
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError("固定精选只接受不带账号密码的 HTTP(S) 地址")
        if set(names) != {entry["name"] for entry in entries}:
            raise ValueError("固定精选缺少声明的频道，拒绝生成不完整目录")
    grouped = {}
    for entry in entries:
        if entry["name"] in names and (fixed or entry["group"] in {"央视", "卫视", "地方", "国内其他", "国际", "香港"}):
            grouped.setdefault(entry["name"], []).append(entry)
    preview, checked, review = [], [], []
    additions = config.get("live_additions", [])
    for name in names:
        options = grouped.get(name, [])
        # 优先实播通过、再按既有健康排序；其他线路仍可能误标或失效。
        if not fixed:
            options = sorted(options, key=lambda e: not recently_decoded(e, history, now))
        taken = options[:limit]
        preview.extend(taken)
        for entry in taken:
            verified = is_recent_content_check(entry["url"], additions, now)
            if verified:
                checked.append(entry)
            review.append({
                "name": name, "line_id": line_id(entry),
                "network": network_type(entry["url"]),
                "cloud_decoded_recently": recently_decoded(entry, history, now),
                "selection_mode": "fixed" if fixed else "health_ranked",
                "device_verification": entry.get("device_verification"),
                "content_status": "manual_image_checked" if verified else "pending_manual_review",
                "rights_status": "not_verified",
            })
    movie_selection_path = out.parent / "movie_selection.json"
    if movie_selection_path.exists():
        movie_selection = json.loads(movie_selection_path.read_text(encoding="utf-8"))
        movie_channels = movie_selection.get("channels", [])
        movie_group = movie_selection.get("group", "电影")
        movie_limit = movie_selection.get("max_lines_per_channel", 1)
        movie_entries = movie_selection.get("fixed_entries", [])
        if movie_group != "电影":
            raise ValueError("电影分组名称必须为电影")
        if (not isinstance(movie_channels, list) or not movie_channels
                or len(movie_channels) > 1000
                or any(not isinstance(name, str) or not name.strip() for name in movie_channels)):
            raise ValueError("电影频道必须声明唯一且非空的频道列表")
        if len(set(movie_channels)) != len(movie_channels):
            raise ValueError("电影频道不能重复声明频道")
        if not isinstance(movie_limit, int) or not 1 <= movie_limit <= 5:
            raise ValueError("电影频道单频道最多保留 1 至 5 条地址")
        if set(movie_channels) & set(names):
            raise ValueError("电影频道不能与普通家庭精选重复")
        if not isinstance(movie_entries, list) or len(movie_entries) > 1000:
            raise ValueError("电影频道固定线路必须为最多 1000 条记录的列表")
        for entry in movie_entries:
            if (not isinstance(entry, dict) or entry.get("name") not in movie_channels
                    or entry.get("group") != movie_group):
                raise ValueError("电影频道固定线路必须属于已声明的电影频道分组")
            url = urlsplit(entry.get("url", ""))
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError("电影频道固定精选只接受不带账号密码的 HTTP(S) 地址")
        if set(movie_channels) != {entry["name"] for entry in movie_entries}:
            raise ValueError("电影频道固定精选缺少声明的频道，拒绝生成不完整目录")
        for name in movie_channels:
            options = [entry for entry in movie_entries if entry["name"] == name][:movie_limit]
            preview.extend(options)
            for entry in options:
                review.append({
                    "name": name, "line_id": line_id(entry),
                    "network": network_type(entry["url"]),
                    "cloud_decoded_recently": False,
                    "selection_mode": "fixed_movie_trial",
                    "device_verification": entry.get("device_verification"),
                    "content_status": "pending_manual_review",
                    "rights_status": "not_verified",
                })

    render(preview, out, "family-preview")
    render(checked, out, "family-content-checked")
    (out / "family-review.json").write_text(json.dumps({
        "generated_at": now.isoformat(timespec="seconds"),
        "disclaimer": "试用列表未逐条核对节目；人工画面核对不代表播放许可、家庭网络可用或长期稳定。",
        "preview_lines": len(preview), "manual_content_checked_lines": len(checked),
        "missing_channels": [n for n in names if n not in grouped],
        "entries": review,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return preview
