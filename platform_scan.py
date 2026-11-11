# 2026/11/10 22:39:00 by liaoweipeng1
"""定向轮查直播平台候选；与固定主线路和普通候选配额相互独立。"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile

import update
from health_tracking import line_id, load_history
from movie_scan import scan_entries
from platform_catalog import SOURCE_ID, SOURCE_URL, normalize, apply_results, is_platform_entry


def choose(entries, history, limit):
    unique = {line_id(e): e for e in entries}
    rows = history.get("lines", {})
    return sorted(unique.values(), key=lambda e: (rows.get(line_id(e), {}).get("checked_at", ""),
                                                 0 if "电影" in e["group"] else 1, line_id(e)))[:limit]


def publish(entries, results, checked_at, environment):
    root = update.ROOT
    public = root / "public"
    ledger_path = public / "platform-ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {"version": 1, "entries": {}}
    history_path = public / "platform-health-history.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {"version": 1, "lines": {}}
    apply_results(ledger, entries, results, checked_at)
    for result in results:
        history["lines"][result["id"]] = {"checked_at": result.get("checked_at", checked_at), "status": result["status"]}
    report = {"checked_at": checked_at, "environment": environment, "sampled": len(results),
              "counts": dict(Counter(r["status"] for r in results)),
              "active": sum(r["state"] == "active" for r in ledger["entries"].values()),
              "boundary": "短时音视频解码，不代表节目身份、持续开播或长期稳定；固定主线路不参与此流程。",
              "results": [{k: v for k, v in r.items() if k in {"id", "name", "status", "video_status", "audio_status"}} for r in results]}
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    current = [e for e in update.parse_live((public / "live.m3u").read_text(encoding="utf-8"))
               if e["group"] != "家庭精选" and not is_platform_entry(e)]
    origins = json.loads((public / "line-origins.json").read_text(encoding="utf-8"))
    for entry in current:
        entry["origins"] = origins.get(line_id(entry), {}).get("sources", [])
    status = json.loads((public / "status.json").read_text(encoding="utf-8"))
    status["platform_updated_at"] = checked_at
    with tempfile.TemporaryDirectory(prefix="build-platform-", dir=root) as folder:
        out = Path(folder)
        shutil.copytree(public, out, dirs_exist_ok=True)
        update.write_json(out / "platform-ledger.json", ledger)
        update.write_json(out / "platform-health-history.json", history)
        update.write_json(out / "platform-health.json", report)
        update.write_live_outputs(current, load_history(public), config, out, status)
        update.write_json(out / "status.json", status)
        update.publish_directory(out)
    print(json.dumps({"sampled": len(results), "counts": report["counts"], "active": report["active"]}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=24)
    args = parser.parse_args()
    if not 1 <= args.limit <= 48:
        parser.error("平台单轮检测上限为 48 条")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 FFmpeg")
    root = update.ROOT
    settings = json.loads((root / "platform_selection.json").read_text(encoding="utf-8"))
    if not settings.get("enabled", False):
        return
    candidates_path = root / "state" / "candidate_entries.json"
    candidates = json.loads(candidates_path.read_text(encoding="utf-8")) if candidates_path.exists() else []
    entries = [normalize(e, settings) for e in candidates if e.get("source_id") == SOURCE_ID]
    ledger_path = root / "public" / "platform-ledger.json"
    if ledger_path.exists():
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        entries += [normalize(r["entry"], settings) for r in ledger["entries"].values()]
    entries = [e for e in entries if e is not None]
    history_path = root / "public" / "platform-health-history.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {}
    selected = choose(entries, history, args.limit)
    results = scan_entries(selected, ffmpeg, retry_limit=2)
    publish(selected, results, datetime.now(timezone.utc).isoformat(timespec="seconds"), "GitHub 标准 Linux 云端")


if __name__ == "__main__":
    main()
