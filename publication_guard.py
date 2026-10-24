# 2026/10/24 13:28:44 by liaoweipeng1
"""使用私有策略检查可公开文件、暂存区与可达 Git 历史。"""
import argparse
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata
from urllib.parse import unquote


class GuardError(Exception):
    """检查未能完成，必须拒绝发布。"""


def normalize(text):
    for _ in range(2):
        text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), text)
        text = html.unescape(unquote(text))
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in text if unicodedata.category(char)[0] in "LN")


def load_terms(policy_file):
    raw = Path(policy_file).read_text(encoding="utf-8") if policy_file else os.environ.get("PUBLICATION_POLICY", "")
    policy = json.loads(raw)
    if not isinstance(policy, dict) or policy.get("version") != 1:
        raise GuardError()
    terms = policy.get("terms")
    if not isinstance(terms, list) or not 1 <= len(terms) <= 256:
        raise GuardError()
    if any(not isinstance(term, str) or not 2 <= len(normalize(term)) <= 200 for term in terms):
        raise GuardError()
    return {normalize(term) for term in terms}


def contains_private(data, terms):
    # 中文 UTF-16 可以既无 BOM 也无 NUL，不能根据这两项猜测编码。
    texts = [data.decode(encoding, errors="replace")
             for encoding in ("utf-8-sig", "utf-16-le", "utf-16-be", "gb18030")]
    for text in texts:
        normalized = normalize(text)
        if any(term in normalized for term in terms):
            return True
    return False


def git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True,
        env=dict(os.environ, GIT_NO_REPLACE_OBJECTS="1"),
    )
    if result.returncode:
        raise GuardError()
    return result.stdout


def scan_objects(root, objects, terms):
    """逐一读取对象；所有外部错误仅返回固定诊断，避免泄露内容。"""
    process = subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=dict(os.environ, GIT_NO_REPLACE_OBJECTS="1"),
    )
    found = False
    try:
        for oid in sorted(objects):
            if not re.fullmatch(rb"[0-9a-f]{40,64}", oid):
                raise GuardError()
            process.stdin.write(oid + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3 or not header[2].isdigit():
                raise GuardError()
            size = int(header[2])
            data = process.stdout.read(size)
            if len(data) != size or process.stdout.read(1) != b"\n":
                raise GuardError()
            if header[1] in (b"blob", b"commit", b"tag", b"tree"):
                found = contains_private(data, terms) or found
    finally:
        process.stdin.close()
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
        process.wait()
    return found


def check(root, terms):
    git(root, "rev-parse", "--is-inside-work-tree")
    if git(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise GuardError()
    found = False
    paths = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")
    for name in set(paths) - {b""}:
        found = contains_private(name, terms) or found
        path = root / os.fsdecode(name)
        if path.is_symlink():
            raise GuardError()
        if path.exists():
            found = contains_private(path.read_bytes(), terms) or found

    objects = set()
    for record in git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if record:
            meta, name = record.split(b"\t", 1)
            mode, oid, stage = meta.split()
            if mode in (b"160000", b"120000") or stage != b"0":
                raise GuardError()
            objects.add(oid)

    refs = git(root, "for-each-ref", "--format=%(refname)")
    found = contains_private(refs, terms) or found
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", "HEAD"],
        capture_output=True, env=dict(os.environ, GIT_NO_REPLACE_OBJECTS="1"),
    )
    revisions = ["--all"] + (["HEAD"] if head.returncode == 0 else [])
    if refs.strip() or head.returncode == 0:
        # 默认分支以外的分支、标签、已删除文件和提交元数据同样检查。
        for line in git(root, "rev-list", "--objects", *revisions).splitlines():
            oid, _, name = line.partition(b" ")
            objects.add(oid)
            found = contains_private(name, terms) or found
    return not (scan_objects(root, objects, terms) or found)


def main():
    parser = argparse.ArgumentParser(description="检查公开发布策略，不输出敏感命中内容。")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--policy-file", help="仅限本机的私有 JSON 文件；不得位于公开仓库中。")
    parser.add_argument("--message-file", type=Path, help="提交前额外检查拟用的提交说明。")
    args = parser.parse_args()
    try:
        terms = load_terms(args.policy_file)
        allowed = check(args.root.resolve(), terms)
        if args.message_file:
            allowed = not contains_private(args.message_file.read_bytes(), terms) and allowed
    except (GuardError, OSError, ValueError, TypeError, UnicodeError):
        print("Publication check unavailable; publishing is blocked.", file=sys.stderr)
        return 2
    if not allowed:
        print("Publication check blocked.", file=sys.stderr)
        return 1
    print("Publication check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
