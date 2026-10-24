# 2026/10/22 20:44:00 by liaoweipeng1
"""频道名称、分类与线路归并；不改变播放地址或请求参数。"""
import copy
import re


REGIONS = {"hk": "香港", "tw": "台湾", "us": "美国", "jp": "日本", "kr": "韩国"}

# 地方频道按区域归类；组内展示顺序独立于候选抽检。
# 卫视频道沿用全国卫视分类。
GUANGXI_LOCAL_CHANNELS = {
    "广西新闻", "广西都市", "广西影视", "广西综艺旅游",
    "广西国际", "广西乐思购", "广西移动数字电视",
    "桂林新闻综合", "桂林科教旅游", "灌阳新闻综合",
    "资源电视台",
}
CCTV_TITLES = {
    1: "综合", 2: "财经", 3: "综艺", 4: "中文国际", 5: "体育", 6: "电影",
    7: "国防军事", 8: "电视剧", 9: "纪录", 10: "科教", 11: "戏曲",
    12: "社会与法", 13: "新闻", 14: "少儿", 15: "音乐", 16: "奥林匹克", 17: "农业农村",
}


def clean_name(name):
    name = re.sub(r"\s*\[(?:Geo-blocked|Not 24/7)\]", "", name, flags=re.I)
    name = re.sub(r"\s*\(\d+[pi]\)", "", name, flags=re.I)
    return re.sub(r"\s+(?:HD|SD)$", "", name, flags=re.I).strip()


def chinese_sort(name):
    # GB18030 的常用汉字区域按拼音排列；数字分块，避免 10 排到 2 前。
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold().encode("gb18030"))
                 for part in re.split(r"(\d+)", name) if part)


def organize(entries, settings):
    aliases = settings.get("aliases", {})
    normalized = []
    for original in entries:
        e = copy.deepcopy(original)
        raw = clean_name(e["name"])
        name = aliases.get(raw, raw)
        cctv = re.fullmatch(r"CCTV[- ]?(\d{1,2})(\+)?(?:\s*(综合|财经|综艺|中文国际|体育|电影|国防军事|电视剧|纪录|科教|戏曲|社会与法|新闻|少儿|音乐|奥林匹克|农业农村))?", raw, re.I)
        if cctv and int(cctv[1]) in CCTV_TITLES:
            number = int(cctv[1])
            name = f"CCTV-{number}" + ("+ 体育赛事" if cctv[2] else " " + CCTV_TITLES[number])
        e["original_name"] = e["name"]
        e["original_group"] = e["group"]
        e["name"] = name
        source = e.get("region", e["source"])
        if source in REGIONS:
            group = REGIONS[source]
        elif name.startswith(("CCTV", "央视")):
            group = "央视"
        elif name.startswith(("CGTN", "美国之音", "金砖电视")) or name in {"上海国际频道", "浙江国际"}:
            group = "国际"
        elif name in {"香港卫视", "凤凰香港"}:
            group = "香港"
        elif "卫视" in name:
            group = "卫视"
        elif name in GUANGXI_LOCAL_CHANNELS:
            group = "广西"
        elif name.startswith(("中国教育", "CHC ", "中国天气")) or name in {"发现之旅", "中学生频道"} or not re.search(r"[\u4e00-\u9fff]", name):
            group = "国内其他"
        else:
            group = "地方"
        e["group"] = group
        normalized.append(e)

    # 同一原名可能是不同地区/时区版本。保留上游 ID 的变体，不能仅凭名字合并。
    variants = {}
    for e in normalized:
        identity = e["attrs"].get("tvg-id", "")
        if identity:
            variants.setdefault((e["group"], e["name"]), set()).add(identity)
    for e in normalized:
        ids = variants.get((e["group"], e["name"]), set())
        identity = e["attrs"].get("tvg-id", "")
        # HD/SD 是同一频道清晰度；East/West 等播出版必须分开。
        non_quality = {i.rsplit("@", 1)[-1] for i in ids if "@" in i} - {"HD", "SD"}
        if len(non_quality) > 1 and identity:
            suffix = identity.rsplit("@", 1)[-1]
            e["name"] += "（" + {"East": "东部版", "West": "西部版"}.get(suffix, suffix) + "）"
        e["attrs"]["tvg-name"] = e["name"]

    seen = {}
    result = []
    for e in normalized:
        key = (e["group"], e["name"], e["url"], tuple(e["directives"]))
        if key not in seen:
            seen[key] = e
            result.append(e)
        else:
            target = seen[key]
            target["origins"] = sorted(set(target.get("origins", [])) | set(e.get("origins", [])))

    return sort_entries(result, settings)


def sort_entries(entries, settings):
    """排序已规范化的频道，避免再次规范化播出版名称。"""
    ranks = {g: i for i, g in enumerate(settings["group_order"])}

    def order(e):
        m = re.match(r"CCTV-(\d+)(\+)? ", e["name"])
        channel = (0, int(m[1]), bool(m[2])) if m else (1, 0, False)
        local_order = 0 if e["group"] == "广西" and e["name"] == "灌阳新闻综合" else 1
        return (ranks.get(e["group"], 999), local_order, channel, chinese_sort(e["name"]))

    return sorted(entries, key=order)


def summarize(entries):
    groups = {}
    for e in entries:
        channel = groups.setdefault(e["group"], {}).setdefault(e["name"], {"name": e["name"], "lines": 0, "txt_lines": 0})
        channel["lines"] += 1
        channel["txt_lines"] += not bool(e["directives"])
    return [{"name": group, "channels": list(channels.values())} for group, channels in groups.items()]
