# 2026/10/23 00:26:16 by liaoweipeng1
"""导入 Nexus 公开国家 API；上游健康标签仅作来源记录，不当作实播证据。"""
import json

from channel_catalog import clean_name


def single_line(value):
    if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
        raise ValueError("Nexus 字段不是安全的单行文本")
    return value


def parse_nexus(text):
    channels = json.loads(text)
    if not isinstance(channels, list):
        raise ValueError("Nexus 国家目录必须是数组")
    entries = []
    for channel in channels:
        identity = single_line(channel["id"])
        name = single_line(channel["name"])
        for stream in channel.get("streams", []):
            url = single_line(stream["url"])
            if not url.startswith(("http://", "https://", "rtmp://", "rtsp://", "udp://", "rtp://")):
                continue
            feed = single_line(stream.get("feed") or "")
            directives = []
            for field, option in (("user_agent", "http-user-agent"), ("referrer", "http-referrer")):
                if stream.get(field):
                    directives.append(f"#EXTVLCOPT:{option}={single_line(stream[field])}")
            attrs = {"tvg-id": identity + ("@" + feed if feed else "")}
            if channel.get("logo"):
                attrs["tvg-logo"] = single_line(channel["logo"])
            entries.append({"name": name, "url": url, "group": "Nexus",
                            "attrs": attrs, "directives": directives,
                            "upstream_health": stream.get("health", {}).get("status", "unknown")
                            if isinstance(stream.get("health"), dict) else "unknown"})
    if not entries:
        raise ValueError("Nexus 没有可导入线路，拒绝替换旧缓存")
    return entries


def align_names(entries):
    """已有台标 ID 对应的名称优先；保留 East/West 等 feed 标识。"""
    known = {}
    for entry in entries:
        identity = entry["attrs"].get("tvg-id")
        if identity and not entry.get("source", "").startswith("nexus-"):
            known.setdefault(identity, clean_name(entry["name"]))
    for entry in entries:
        identity = entry["attrs"].get("tvg-id")
        if entry.get("source", "").startswith("nexus-") and identity in known:
            entry["name"] = known[identity]


def merge_origins(target, extra):
    target["origins"] = sorted(set(target.get("origins", [])) | set(extra.get("origins", [])))
