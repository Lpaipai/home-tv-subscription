"""有限额临时取帧 + 本地 Tesseract OCR 台标辅助核验，不保存或上传截图。"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from scan_live import playback_headers


def target(name):
    cctv = re.search(r"CCTV[\s_-]*(1[0-7]|[1-9])(\+|PLUS)?(?![0-9])", name, re.I)
    if cctv:
        return ("cctv", cctv.group(1) + ("+" if cctv.group(2) else ""))
    satellite = re.search(r"(湖南|浙江|江苏|广东|深圳|广西|北京|东方|安徽|山东|河南|河北|湖北|江西|福建|四川|重庆|天津|黑龙江|吉林|辽宁|陕西|山西|贵州|云南|新疆|西藏|青海|甘肃|宁夏|内蒙古)卫视", name)
    if satellite:
        return ("satellite", satellite.group(1))
    return None


def identified(text, kind):
    cleaned = re.sub(r"[\s:\-_.]", "", text.upper())
    if kind == "cctv":
        # 仅在 CCTV 台标完整可见时识别；无法识别 '+', 不假设它不存在。
        found = re.findall(r"CCTV(1[0-7]|[1-9])(\+|PLUS)?(?![0-9])", cleaned)
        return [number + ("+" if plus else "") for number, plus in found]
    matches = re.findall(r"(湖南|浙江|江苏|广东|深圳|广西|北京|东方|安徽|山东|河南|河北|湖北|江西|福建|四川|重庆|天津|黑龙江|吉林|辽宁|陕西|山西|贵州|云南|新疆|西藏|青海|甘肃|宁夏|内蒙古)卫视", text)
    return matches


def classify(name, text_frames):
    expectation = target(name)
    if not expectation:
        return "unsupported"
    kind, expected = expectation
    frames = [identified(text, kind) for text in text_frames]
    if len(frames) < 2 or any(not choices for choices in frames):
        return "unknown"
    # 每帧所有可识别结果的交集只含同一个不同频道，才能标记为疑似错台。
    joint = set(frames[0]).intersection(*map(set, frames[1:]))
    if expected in joint:
        return "match"
    if len(joint) == 1:
        other = next(iter(joint))
        # CCTV-5 与 CCTV-5+ 在 OCR 常遗漏 '+'，不做错台判定。
        if kind == "cctv" and {expected, other} == {"5", "5+"}:
            return "unknown"
        return "mismatch"
    return "unknown"


def check(entry, ffmpeg, ocr="tesseract", timeout=18):
    """只识别画面左上台标区；模糊或缺失则 unknown，不猜测频道。"""
    if not target(entry["name"]):
        return {"visual_status": "unsupported", "frame_count": 0}
    if not shutil.which(ocr):
        return {"visual_status": "tool_missing", "frame_count": 0}
    try:
        headers = playback_headers(entry)
    except ValueError:
        return {"visual_status": "unsupported_headers", "frame_count": 0}
    with tempfile.TemporaryDirectory(prefix="iptv-logo-") as folder:
        path = Path(folder)
        pattern = str(path / "frame_%02d.png")
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                   "-rw_timeout", "5000000", "-protocol_whitelist", "http,https,tcp,tls,crypto"]
        if headers:
            command += ["-headers", headers]
        command += ["-i", entry["url"], "-map", "0:v:0", "-an", "-t", "4",
                    "-vf", "fps=1,scale=960:-2,crop=480:160:0:0",
                    "-frames:v", "2", pattern]
        try:
            capture = subprocess.run(command, capture_output=True, timeout=timeout,
                                     check=False)
        except subprocess.TimeoutExpired:
            return {"visual_status": "capture_timeout", "frame_count": 0}
        images = sorted(path.glob("frame_*.png"))
        if capture.returncode != 0 or len(images) < 2:
            return {"visual_status": "no_frames", "frame_count": len(images)}
        texts = []
        for frame in images[:2]:
            command = [ocr, str(frame), "stdout", "--psm", "11", "-l", "eng+chi_sim"]
            try:
                completed = subprocess.run(command, capture_output=True, timeout=5,
                                           check=False)
            except subprocess.TimeoutExpired:
                return {"visual_status": "ocr_timeout", "frame_count": len(images)}
            if completed.returncode:
                return {"visual_status": "ocr_unavailable", "frame_count": len(images)}
            texts.append(completed.stdout.decode("utf-8", errors="replace"))
        return {"visual_status": classify(entry["name"], texts),
                "frame_count": len(images)}
