# 2026/11/10 22:39:00 by liaoweipeng1
"""直播平台候选的独立音视频准入、分类与已验证目录。"""
import copy
import json
import re
from urllib.parse import urlsplit

from health_tracking import line_id

SOURCE_ID = "jiandantv-platforms"
SOURCE_URL = "https://raw.githubusercontent.com/jiandantv/IPTV2026/main/live_platforms.m3u"
ID_PREFIX = "PLATFORM_"


def normalize(entry, settings=None):
    """同房间只保留一个身份；名称可以变化，频道标识不随昵称变化。"""
    item = copy.deepcopy(entry)
    parsed = urlsplit(item["url"])
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or not parsed.hostname or "更新时间" in item.get("group", "")):
        return None
    # 此来源的平台入口与测试片地址均为公开的固定入口，不发布临时签名 CDN 地址。
    if parsed.hostname != "live.ottiptv.cc" and not (
            parsed.hostname == "cdn.jsdelivr.net" and item.get("group") == "4K频道"):
        return None
    if any(not value.startswith(("#EXTVLCOPT:http-user-agent=", "#EXTVLCOPT:http-referrer="))
           for value in item.get("directives", [])):
        return None
    item.setdefault("directives", [])
    if not any(value.startswith("#EXTVLCOPT:http-user-agent=") for value in item["directives"]):
        item["directives"].append("#EXTVLCOPT:http-user-agent=Mozilla/5.0")
    key = line_id(item)
    aliases = (settings or {}).get("rooms", {})
    alias = aliases.get(item["url"], {})
    item["name"] = alias.get("name", item["name"])
    original_group = item.get("original_group", item["group"])
    item["group"] = alias.get("group", {"zonghe": "综合轮播", "sport": "体育赛事", "car": "汽车"}.get(original_group, original_group))
    if not alias:
        name = item["name"]
        if re.search("解说|说电影|说美剧|速看|影评|影探", name):
            item["group"] = "影视解说"
        elif re.search("喜剧电影|搞笑电影|星爷|周星驰|沈腾", name):
            item["group"] = "电影·喜剧"
        elif re.search("动作电影|动作大片|武侠电影|成龙|李连杰|甄子丹", name):
            item["group"] = "电影·动作"
        elif re.search("科幻电影|科幻大片|漫威电影", name):
            item["group"] = "电影·科幻奇幻"
        elif re.search("电影|影院", name):
            item["group"] = "电影·综合"
        if name == "YY用户":
            item["name"] = "YY轮播 · " + parsed.path.rstrip("/").rsplit("/", 1)[-1]
    item["group"] = (settings or {}).get("group_aliases", {}).get(item["group"], item["group"])
    allowed = (settings or {}).get("included_groups")
    if allowed is not None and item["group"] not in allowed:
        return None
    item["attrs"] = dict(item.get("attrs", {}), **{"tvg-id": ID_PREFIX + key,
                                                 "tvg-name": item["name"], "group-title": item["group"]})
    item["platform_room"] = True
    return item


def is_platform_entry(entry):
    return entry.get("platform_room") is True or entry.get("attrs", {}).get("tvg-id", "").startswith(ID_PREFIX)


def select_published(out, config):
    setting_path = out.parent / "platform_selection.json"
    ledger_path = out / "platform-ledger.json"
    if not ledger_path.exists():
        return []
    if not setting_path.exists():
        raise ValueError("平台台账已存在但分类配置缺失，拒绝丢失已验证目录")
    settings = json.loads(setting_path.read_text(encoding="utf-8"))
    if not settings.get("enabled", False):
        return []
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if ledger.get("version") != 1 or not isinstance(ledger.get("entries"), dict):
        raise ValueError("直播平台台账格式无效")
    excluded = {r["url"] for r in config.get("live_exclusions", [])}
    selected = []
    for key, record in ledger["entries"].items():
        if record.get("state") != "active" or not record.get("last_success_at"):
            continue
        entry = normalize(record["entry"], settings)
        if entry is None or line_id(entry) != key or entry["url"] in excluded:
            continue
        selected.append(entry)
    priority = {name: index for index, name in enumerate(settings.get("group_order", []))}
    selected.sort(key=lambda e: (priority.get(e["group"], 100), e["group"], e["name"], line_id(e)))
    if len(selected) > settings.get("max_channels", 4000):
        raise ValueError("已验证平台目录超过配置容量，拒绝静默截断")
    return selected


def apply_results(ledger, entries, results, checked_at):
    """只允许本轮音视频均通过的候选首次进入目录；两日失败暂时停用。"""
    by_id = {line_id(e): e for e in entries}
    for result in results:
        observed_at = result.get("checked_at", checked_at)
        key = result["id"]
        if key not in by_id:
            continue
        entry = by_id[key]
        record = ledger["entries"].get(key)
        passed = result.get("status") == "decoded" and result.get("audio_status") == "decoded" and result.get("video_status") == "decoded"
        if passed:
            ledger["entries"][key] = {"entry": entry, "source_url": SOURCE_URL,
                                      "last_success_at": observed_at, "last_attempt_at": observed_at,
                                      "last_status": "decoded", "state": "active", "failure_days": []}
        elif record is not None:
            record["last_attempt_at"] = observed_at
            record["last_status"] = result.get("status", "failed")
            if result.get("status") == "failed":
                days = set(record.get("failure_days", []))
                days.add(observed_at[:10])
                record["failure_days"] = sorted(days)[-10:]
                if len(days) >= 2:
                    record["state"] = "suspended"
