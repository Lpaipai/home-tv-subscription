# 2026/10/24 02:23:00 by liaoweipeng1
"""家庭小列表与内容人工核验报告。云端能解码并不证明节目身份正确。"""
from datetime import datetime, date, timezone
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
    if mode not in {"fixed", "health_ranked", "fixed_plus_auto"}:
        raise ValueError("未知的家庭精选选择模式")
    catalog_entries = entries
    fixed = mode in {"fixed", "fixed_plus_auto"}
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
            cloud_decoded = recently_decoded(entry, history, now)
            if not cloud_decoded and entry.get("cloud_checked_at"):
                try:
                    checked_at = datetime.fromisoformat(entry["cloud_checked_at"])
                    age_seconds = (now - checked_at).total_seconds()
                    cloud_decoded = 0 <= age_seconds <= 30 * 86400
                except (TypeError, ValueError):
                    pass
            review.append({
                "name": name, "line_id": line_id(entry),
                "network": network_type(entry["url"]),
                "cloud_decoded_recently": cloud_decoded,
                "selection_mode": "fixed" if fixed else "health_ranked",
                "line_role": entry.get("line_role", "primary"),
                "source_note": entry.get("source_note"),
                "cloud_checked_at": entry.get("cloud_checked_at"),
                "device_verification": entry.get("device_verification"),
                "content_status": "manual_image_checked" if verified else "pending_manual_review",
                "rights_status": "not_verified",
            })
    if mode == "fixed_plus_auto":
        # 固定33条主线绝不被候选替换，只对尚未拥有固定主线的频道自动补齐。
        auto_targets = selections.get("auto_targets", [])
        allowed_hosts = set(selections.get("auto_trusted_hosts", []))
        auto_limit = selections.get("auto_max_lines_per_channel", 1)
        required_days = selections.get("auto_min_distinct_success_days", 2)
        max_age = selections.get("auto_success_max_age_days", 30)
        if (not isinstance(auto_targets, list) or len(set(auto_targets)) != len(auto_targets)
                or not isinstance(auto_limit, int) or not 1 <= auto_limit <= 2
                or not isinstance(required_days, int) or required_days < 2
                or not isinstance(max_age, int) or not 1 <= max_age <= 90):
            raise ValueError("自动精选补齐配置无效")
        for name in auto_targets:
            if name in names:
                continue
            eligible = []
            for entry in catalog_entries:
                if entry.get("name") != name or entry.get("group") not in {"央视", "卫视", "国际"}:
                    continue
                url = urlsplit(entry.get("url", ""))
                if url.scheme not in {"http", "https"} or url.hostname not in allowed_hosts:
                    continue
                # 同源规则：只信任当前固定频道所在的两类服务器及已知路径。
                if url.hostname == "101.35.240.114" and url.path != "/live.php":
                    continue
                if url.hostname == "0472.org" and not url.path.startswith("/hls/cgtn"):
                    continue
                if entry.get("admission") == "ipv6_unverified" or entry.get("visual_status") == "mismatch":
                    continue
                item = history.get("lines", {}).get(line_id(entry), {})
                if item.get("status") != "decoded":
                    continue
                days = set()
                newest = None
                for observation in item.get("observations", []):
                    if observation.get("status") != "decoded":
                        continue
                    try:
                        at = datetime.fromisoformat(observation["at"]).astimezone(timezone.utc)
                    except (KeyError, ValueError, TypeError):
                        continue
                    days.add(at.date())
                    if newest is None or at > newest:
                        newest = at
                if len(days) < required_days or newest is None:
                    continue
                age_seconds = (now.astimezone(timezone.utc) - newest).total_seconds()
                if age_seconds < 0 or age_seconds > max_age * 86400:
                    continue
                eligible.append((newest, entry))
            eligible.sort(key=lambda row: row[0], reverse=True)
            for _, entry in eligible[:auto_limit]:
                preview.append(entry)
                review.append({
                    "name": name, "line_id": line_id(entry),
                    "network": network_type(entry["url"]),
                    "cloud_decoded_recently": True,
                    "selection_mode": "auto_two_distinct_days_same_source",
                    "line_role": "auto_added",
                    "source_note": None,
                    "cloud_checked_at": newest.isoformat(),
                    "device_verification": None,
                    "content_status": "pending_manual_review",
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
        if (not isinstance(movie_channels, list)
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
        "auto_missing_channels": [n for n in selections.get("auto_targets", [])
                                  if n not in {e["name"] for e in preview}],
        "entries": review,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return preview
