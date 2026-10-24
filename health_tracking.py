# 2026/10/23 00:26:16 by liaoweipeng1
"""保存本订阅云端解码历史，并在同频道内调整备用线路顺序。"""
from datetime import datetime
import hashlib
import json


def line_id(entry):
    identity = [entry["url"], entry.get("directives", [])]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16]


def load_history(public):
    path = public / "health-history.json"
    if path.exists():
        history = json.loads(path.read_text(encoding="utf-8"))
        if history.get("version") != 1 or not isinstance(history.get("lines"), dict):
            raise ValueError("健康历史格式不兼容，停止覆盖")
        return history
    history = {"version": 1, "lines": {}}
    legacy = public / "health.json"
    if legacy.exists():
        report = json.loads(legacy.read_text(encoding="utf-8"))
        if report.get("checked_at"):
            record_results(history, report.get("results", []), report["checked_at"])
    return history


def record_results(history, results, checked_at):
    for result in results:
        item = history["lines"].setdefault(result["id"], {"score": 50.0, "failures": 0, "observations": []})
        status = result["status"]
        # 授权/地区/限流与不支持的参数不扣分，也不覆盖最近一次解码证据。
        if status in {"decoded", "failed"}:
            item["score"] = round(item["score"] * 0.7 + (100 if status == "decoded" else 0) * 0.3, 2)
            item["failures"] = 0 if status == "decoded" else item["failures"] + 1
            item["status"] = status
            item["checked_at"] = checked_at
        observation = {"at": checked_at, "status": status, "attempts": result.get("attempts", 1)}
        item["observations"] = (item["observations"] + [observation])[-30:]
    history["updated_at"] = checked_at


def rank_entries(entries, history, now):
    def priority(entry):
        item = history["lines"].get(line_id(entry), {})
        checked = item.get("checked_at")
        if checked:
            age = (now - datetime.fromisoformat(checked)).total_seconds()
            if 0 <= age <= 21 * 86400:
                if item.get("status") == "decoded":
                    return (0, -item["score"])
                if item.get("failures", 0) >= 2:
                    return (2, -item["score"])
        return (1, 0)

    channels = {}
    for entry in entries:
        channels.setdefault((entry["group"], entry["name"]), []).append(entry)
    return [entry for lines in channels.values() for entry in sorted(lines, key=priority)]
