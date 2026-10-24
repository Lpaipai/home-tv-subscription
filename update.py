# 2026/10/22 19:52:00 by liaoweipeng1
"""更新家庭直播与点播订阅；仅下载文本，不下载或执行远程插件。"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from channel_catalog import organize, summarize
from health_tracking import line_id, load_history, rank_entries
from network_policy import network_type
from curation import build_family_outputs, family_group_entries
from nexus_sources import align_names, merge_origins, parse_nexus
from auto_additions import active_additions, align_regions, load_additions

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
STATE = ROOT / "state"
BEIJING = timezone(timedelta(hours=8))
CACHE_ONLY = False
PATH_KEYS = {"spider", "jar", "api", "ext", "url", "logo", "wallpaper", "danmaku"}


def stamp():
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def canonical(url):
    """对已知 GitHub 代理前缀取原始地址，不改动其它站点。"""
    for prefix in ("https://gh-proxy.org/", "https://gh-proxy.com/",
                   "https://gh.jasonzeng.dev/"):
        if url.startswith(prefix + "https://raw.githubusercontent.com/"):
            return url[len(prefix):]
    return url


def transport_url(url):
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname:
        raise ValueError("只接受 HTTP(S) 配置地址")
    if p.username or p.password:
        raise ValueError("配置地址不得内嵌账号密码")
    host = p.hostname.encode("idna").decode("ascii")
    if ":" in host:
        host = "[" + host + "]"
    netloc = host + (":" + str(p.port) if p.port else "")
    return urlunsplit((p.scheme, netloc, quote(p.path, safe="/%:@!$&'()*+,;=-._~"),
                       quote(p.query, safe="%=&/:?+;,@[]!$'()*-._~"), p.fragment))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_json(text):
    # 上游镜像已是标准 JSON；拒绝执行 JS 或猜测加密格式。
    value = json.loads(text.lstrip("\ufeff"))
    if not isinstance(value, dict):
        raise ValueError("配置顶层不是对象")
    return value


def parse_vod(text):
    value = parse_json(text)
    if not ((isinstance(value.get("sites"), list) and value["sites"])
            or (isinstance(value.get("urls"), list) and value["urls"])):
        raise ValueError("点播配置缺少非空 sites/urls，拒绝替换缓存")
    return value


def fetch(url, validator):
    url = canonical(url)
    key = hashlib.sha256(url.encode()).hexdigest()
    path = CACHE / (key + ".txt")
    meta_path = CACHE / (key + ".json")
    if CACHE_ONLY:
        if not (path.exists() and meta_path.exists()):
            raise RuntimeError(f"{url}: 没有可用缓存，保留原入口")
        value = validator(path.read_text(encoding="utf-8"))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return value, dict(meta, cached=True)
    try:
        req = Request(transport_url(url), headers={"User-Agent": "LWP-Subscription/1.0"})
        with urlopen(req, timeout=18) as response:
            data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise ValueError("配置超过 8 MiB 限额")
            final_url = response.geturl()
        text = data.decode("utf-8-sig")
        value = validator(text)
        meta = {"url": url, "resolved_url": final_url, "fetched_at": stamp(),
                "sha256": hashlib.sha256(data).hexdigest(), "cached": False}
        CACHE.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".new")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
        write_json(meta_path, meta)
        return value, meta
    except Exception as error:
        if path.exists() and meta_path.exists():
            value = validator(path.read_text(encoding="utf-8"))
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return value, dict(meta, cached=True, error=str(error))
        raise RuntimeError(f"{url}: {error}") from error


def parse_live(text, label=""):
    entries = []
    group = label
    pending = None
    directives = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            # 分隔符为引号外的第一个逗号，台标地址本身可含逗号。
            quoted = False
            cut = -1
            for i, char in enumerate(line):
                if char == '"':
                    quoted = not quoted
                elif char == ',' and not quoted:
                    cut = i
                    break
            if cut < 0:
                raise ValueError("EXTINF 缺少频道名分隔符")
            attrs = dict(re.findall(r'([\w-]+)="([^\"]*)"', line[:cut]))
            pending = {"name": line[cut + 1:].strip(), "attrs": attrs,
                       "group": attrs.get("group-title", label)}
            directives = []
        elif line.startswith(("#EXTVLCOPT:", "#KODIPROP:", "#EXTHTTP:")):
            directives.append(line)
        elif line.startswith("#"):
            continue
        elif pending:
            if not line.startswith(("http://", "https://", "rtsp://", "rtmp://", "udp://", "rtp://")):
                raise ValueError("直播地址协议不支持")
            entries.append(dict(pending, url=line, directives=directives[:]))
            pending = None
        elif "," in line:
            name, address = line.split(",", 1)
            if address == "#genre#":
                group = name
                continue
            if "更新时间" in group or re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", name):
                continue
            # 不拆分 URL 中可能存在的 $ 参数。
            if address.startswith(("http://", "https://", "rtsp://", "rtmp://", "udp://", "rtp://")):
                entries.append({"name": name, "group": group, "url": address,
                                "attrs": {}, "directives": []})
    if not entries:
        raise ValueError("未解析到直播条目，拒绝覆盖旧缓存")
    return entries


def deduplicate(entries):
    result = []
    seen = {}
    for entry in entries:
        key = (entry["name"].strip().casefold(), entry["url"], tuple(entry["directives"]))
        if key not in seen:
            seen[key] = entry
            result.append(entry)
        else:
            merge_origins(seen[key], entry)
    return result


def exclude_entries(entries, rules):
    """人工确认的错标地址跨来源、跨别名排除。"""
    urls = {rule["url"] for rule in rules}
    return [entry for entry in entries if entry["url"] not in urls]


def absolutize(value, origin, key=""):
    if isinstance(value, dict):
        return {k: absolutize(v, origin, k) for k, v in value.items()}
    if isinstance(value, list):
        return [absolutize(v, origin, key) for v in value]
    if isinstance(value, str) and key in PATH_KEYS:
        relative = value.startswith(("./", "../", "/"))
        bare_file = key in {"spider", "jar"} and not urlsplit(value).scheme
        if relative or bare_file:
            address, separator, checksum = value.partition(";md5;")
            return urljoin(origin, address) + (separator + checksum if separator else "")
    return value


def live_definition(base):
    return [{"name": "LWP 家庭直播", "type": 0, "url": base + "/live.txt",
             "playerType": 1, "ua": "okhttp/3.15"}]


def render_live(entries, out, stem="live"):
    m3u = ["#EXTM3U"]
    txt = []
    last_group = None
    omissions = 0
    groups = {}
    for entry in entries:
        groups.setdefault(entry["group"], []).append(entry)
    for e in (entry for group in groups.values() for entry in group):
        attrs = dict(e["attrs"], **{"group-title": e["group"]})
        fields = " ".join(f'{k}="{str(v).replace(chr(34), chr(39))}"' for k, v in attrs.items())
        m3u += [f'#EXTINF:-1 {fields},{e["name"]}', *e["directives"], e["url"]]
        # TXT 无法无损携带特殊请求头/DRM参数；单独计数，M3U 保留全部。
        if e["directives"]:
            omissions += 1
            continue
        if e["group"] != last_group:
            txt.append(e["group"] + ",#genre#")
            last_group = e["group"]
        txt.append(e["name"].replace(",", "，") + "," + e["url"])
    (out / (stem + ".m3u")).write_text("\n".join(m3u) + "\n", encoding="utf-8")
    (out / (stem + ".txt")).write_text("\n".join(txt) + "\n", encoding="utf-8")
    return omissions


def validate_live_update(report, previous):
    """自动发布门槛：来源不能丢失，目录不能突然大幅缩水。"""
    if any("entries" not in source for source in report["live"]):
        raise RuntimeError("有直播来源失败且无缓存，停止自动发布")
    if all(source.get("cached") for source in report["live"]):
        raise RuntimeError("全部来源使用旧缓存，停止自动发布")
    if report["live_entries"] < previous.get("live_entries", 0) * 0.8:
        raise RuntimeError("直播线路减少超过 20%，停止自动发布")
    old_sources = {source["id"]: source for source in previous.get("live", [])}
    for source in report["live"]:
        old_count = old_sources.get(source["id"], {}).get("entries", 0)
        if source["entries"] < old_count * 0.6:
            raise RuntimeError(f"来源 {source['id']} 条目减少超过 40%，停止自动发布")


def publish_directory(out):
    """完整生成后切换目录，异常时恢复旧目录。"""
    public = ROOT / "public"
    backup = None
    if public.exists():
        backup = ROOT / "backups" / datetime.now(BEIJING).strftime("%Y%m%d-%H%M%S-%f")
        backup.parent.mkdir(exist_ok=True)
        public.rename(backup)
    try:
        out.rename(public)
    except OSError:
        if backup is not None:
            backup.rename(public)
        raise


def write_live_outputs(entries, history, config, out, report):
    """更新统一直播、精选、来源与数量；调用方负责暂存和原子发布。"""
    entries = rank_entries(entries, history, datetime.now(BEIJING))
    write_json(out / "health-history.json", history)
    origins = {}
    for entry in entries:
        item = origins.setdefault(line_id(entry), {"url": entry["url"], "sources": []})
        item["sources"] = sorted(set(item["sources"]) | set(entry.get("origins", [])))
    write_json(out / "line-origins.json", origins)
    report["nexus_unique_lines"] = sum(
        any("dearbulut.github.io" in url for url in item["sources"])
        and all("dearbulut.github.io" in url for url in item["sources"])
        for item in origins.values()
    )
    channel_groups = summarize(entries)
    channels = [c for group in channel_groups for c in group["channels"]]
    report["live_groups"] = len(channel_groups)
    report["live_channels"] = len(channels)
    report["live_multi_line_channels"] = sum(c["lines"] > 1 for c in channels)
    write_json(out / "channels.json", channel_groups)
    report["live_entries"] = len(entries)
    report["live_network_types"] = {
        network: sum(network_type(e["url"]) == network for e in entries)
        for network in ("ipv4_literal", "ipv6_literal", "hostname_unresolved", "other_protocol", "invalid")
    }
    preview = build_family_outputs(entries, history, config, out, datetime.now(BEIJING), render_live)
    family = family_group_entries(preview)
    # 家庭精选来自固定清单或健康排序；原分组和完整目录均保留。
    report["family_lines"] = len(family)
    report["family_channels"] = len({entry["name"] for entry in family})
    report["published_live_groups"] = report["live_groups"] + bool(family)
    report["published_live_entries"] = len(family) + len(entries)
    report["txt_omitted_special_headers"] = render_live(family + entries, out)
    return entries


def main():
    global CACHE_ONLY
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url")
    parser.add_argument("--cached", action="store_true", help="用现有缓存重新生成，不访问上游")
    parser.add_argument("--live-only", action="store_true", help="只更新直播，保留已发布点播配置")
    parser.add_argument("--guarded", action="store_true", help="启用自动发布缩水与缺失来源保护")
    args = parser.parse_args()
    CACHE_ONLY = args.cached
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    base = (args.base_url or config["base_url"]).rstrip("/")
    transport_url(base)
    out = ROOT / "build"
    if out.exists():
        previous_build = ROOT / "backups" / ("build-" + datetime.now(BEIJING).strftime("%Y%m%d-%H%M%S-%f"))
        previous_build.parent.mkdir(exist_ok=True)
        out.rename(previous_build)
    out.mkdir(exist_ok=True)
    report = {"updated_at": stamp(), "base_url": base, "live": [], "vod": [],
              "verification": "目录解析与依赖路径处理；不代表频道或点播实播通过"}
    entries = []

    def get_live(source):
        validator = parse_nexus if source.get("format") == "nexus" else lambda text: parse_live(text, source["name"])
        data, meta = fetch(source["url"], validator)
        for e in data:
            e["source"] = source["id"]
            e["region"] = source.get("region", source["id"])
            e["origins"] = [source["url"]]
            e["group"] = source["name"] + (" / " + e["group"] if e["group"] != source["name"] else "")
        return data, dict(meta, id=source["id"], name=source["name"], entries=len(data))

    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        futures = [pool.submit(get_live, s) for s in config["live_sources"]]
        for source, future in zip(config["live_sources"], futures):
            try:
                items, meta = future.result()
                entries.extend(items)
                report["live"].append(meta)
            except Exception as error:
                report["live"].append({"id": source["id"], "error": str(error)})
    if not entries:
        raise RuntimeError("所有直播源失败，现有订阅保持不变")
    # 人工核验的补充线路独立维护，避免上游刷新重新引入错标地址。
    for addition in config.get("live_additions", []):
        transport_url(addition["url"])
        entries.append({"name": addition["name"], "url": addition["url"],
                        "group": "人工核验", "source": "manual",
                        "attrs": {}, "directives": [],
                        "provenance": addition.get("provenance", ""),
                        "origins": [addition["provenance"]] if addition.get("provenance") else [],
                        "verified_at": addition.get("verified_at", "")})
    ledger = load_additions(ROOT / "public")
    settings = json.loads((ROOT / "channel_names.json").read_text(encoding="utf-8"))
    automatic = active_additions(ledger, config)
    automatic = align_regions(automatic, organize(entries, settings), settings)
    for addition in automatic:
        ledger["entries"][line_id(addition)] = addition
    entries.extend(automatic)
    report["auto_additions"] = len(automatic)
    report["auto_additions_ipv6_unverified"] = sum(
        item.get("admission") == "ipv6_unverified" for item in automatic)
    write_json(out / "auto-additions.json", ledger)
    align_names(entries)
    entries = deduplicate(entries)
    exclusions = config.get("live_exclusions", [])
    before_exclusions = len(entries)
    entries = exclude_entries(entries, exclusions)
    report["live_exclusions"] = exclusions
    report["live_excluded_entries"] = before_exclusions - len(entries)
    entries = organize(entries, settings)
    history = load_history(ROOT / "public")
    active_ids = {line_id(entry) for entry in entries}
    history["lines"] = {key: value for key, value in history["lines"].items() if key in active_ids}
    entries = write_live_outputs(entries, history, config, out, report)
    if args.guarded:
        previous = json.loads((ROOT / "public" / "status.json").read_text(encoding="utf-8"))
        validate_live_update(report, previous)
    STATE.mkdir(exist_ok=True)
    write_json(STATE / "live_entries.json", entries)
    for filename in ("health.json", "health.md", "source-review.json",
                     "candidate-health.json", "candidate-health-history.json"):
        if (ROOT / "public" / filename).exists():
            shutil.copy2(ROOT / "public" / filename, out / filename)

    if args.live_only:
        public = ROOT / "public"
        previous = json.loads((public / "status.json").read_text(encoding="utf-8"))
        report["vod"] = previous["vod"]
        report["vod_updated_at"] = previous.get("vod_updated_at", previous["updated_at"])
        report["update_scope"] = "仅直播；点播沿用此前发布版本"
        for filename in ("subscription.json", "live-config.json"):
            shutil.copy2(public / filename, out / filename)
        shutil.copytree(public / "vod", out / "vod")
        sources = json.loads((public / "sources.json").read_text(encoding="utf-8"))
        sources["live"] = config["live_sources"]
        write_json(out / "sources.json", sources)
        write_json(out / "status.json", report)
        publish_directory(out)
        print(json.dumps({"live_entries": len(entries), "scope": "live-only"}), flush=True)
        return

    catalog, _ = fetch(config["vod_catalog"], parse_json)
    origins, _ = fetch(config["vod_origins"], parse_json)
    status, _ = fetch(config["vod_status"], parse_json)
    origin_map = {s["id"]: s["urls"][0] for s in origins["sources"]}
    for s in status["sources"]:
        if str(s.get("used_url", "")).startswith(("http://", "https://")):
            origin_map[s["id"]] = s["used_url"]
    completed = {}
    active = set()

    def build_vod(name, url, origin=None, depth=0):
        url = canonical(url)
        if url in active or depth > 3 or len(completed) > 60:
            raise ValueError("多仓循环或超过展开限制")
        if url in completed:
            return completed[url]
        key = hashlib.sha256(url.encode()).hexdigest()[:12]
        target = base + "/vod/" + key + ".json"
        active.add(url)
        try:
            print("检查点播：" + name, flush=True)
            data, meta = fetch(url, parse_vod)
            effective_origin = origin or meta["resolved_url"]
            if isinstance(data.get("sites"), list) and data["sites"]:
                result = absolutize(copy.deepcopy(data), effective_origin)
                preferred = config.get("vod_home_api", {}).get(Path(urlsplit(url).path).stem)
                if preferred:
                    result["sites"].sort(key=lambda site: site.get("api") != preferred)
                result["lives"] = live_definition(base)
                count = len(result["sites"])
                kind = "single"
            elif isinstance(data.get("urls"), list) and data["urls"]:
                links = []
                for item in data["urls"]:
                    child_url = urljoin(effective_origin, item["url"])
                    child_name = item.get("name", child_url)
                    try:
                        destination = build_vod(name + " / " + child_name, child_url, depth=depth + 1)
                    except Exception as error:
                        destination = child_url
                        report["vod"].append({"name": child_name, "url": child_url,
                                              "error": str(error), "external_fallback": True})
                    links.append(dict(item, url=destination))
                result = dict(data, urls=links)
                count = len(links)
                kind = "multi"
            else:
                raise ValueError("配置缺少非空 sites/urls")
            write_json(out / "vod" / (key + ".json"), result)
            report["vod"].append(dict(meta, name=name, kind=kind, count=count,
                                      origin=effective_origin, output="vod/" + key + ".json"))
            completed[url] = target
            return target
        finally:
            active.remove(url)

    top = []
    preferred_order = config.get("preferred_vod", [])
    def priority(item):
        name = item["name"]
        return preferred_order.index(name) if name in preferred_order else len(preferred_order)

    ordered_catalog = sorted(catalog["urls"], key=priority)
    for item in ordered_catalog:
        source_url = canonical(item["url"])
        source_id = Path(urlsplit(source_url).path).stem
        try:
            result_url = build_vod(item["name"], source_url, origin_map.get(source_id))
        except Exception as error:
            result_url = source_url
            report["vod"].append({"name": item["name"], "url": source_url,
                                  "error": str(error), "external_fallback": True})
        top.append({"name": item["name"], "url": result_url})
    write_json(out / "subscription.json", {"urls": top})
    write_json(out / "live-config.json", {"sites": [], "lives": live_definition(base)})
    write_json(out / "status.json", report)
    write_json(out / "sources.json", {"live": config["live_sources"], "vod": catalog})
    # 完整生成后按目录切换，旧版本留存，避免已移除来源的旧文件混入发布。
    publish_directory(out)
    print(json.dumps({"live_entries": len(entries), "vod_entries": len(top),
                      "vod_configs": len(completed), "failures": sum("error" in v for v in report["vod"]),
                      "output": str(ROOT / "public")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
