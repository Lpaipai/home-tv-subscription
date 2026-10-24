# 2026/10/24 13:28:44 by liaoweipeng1
"""仅用虚构词验证发布检查；真实策略不得写进测试。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("publication_guard.py")
POLICY = {"version": 1, "terms": ["PrivateExample", "虚构保留名"]}


class PublicationGuardTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "发布检查器尚未实现")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q", "-b", "main")
        (self.root / "README.md").write_text("Public IPTV subscription\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial public files")

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.root), "-c", "core.autocrlf=false",
             "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            stderr=subprocess.PIPE,
        )

    def run_guard(self, policy=POLICY, root=None, extra_args=()):
        env = dict(os.environ)
        env.pop("PUBLICATION_POLICY", None)
        if policy is not None:
            env["PUBLICATION_POLICY"] = json.dumps(policy, ensure_ascii=False)
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--root", str(root or self.root), *extra_args],
            env=env, capture_output=True, encoding="utf-8", timeout=30,
        )

    def assert_blocked(self, result):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        output = result.stdout + result.stderr
        self.assertNotIn("PrivateExample", output)
        self.assertNotIn("虚构保留名", output)

    def test_clean_repository_is_allowed(self):
        self.assertEqual(self.run_guard().returncode, 0)

    def test_missing_empty_or_invalid_policy_fails_closed(self):
        for policy in (None, {}, {"version": 1, "terms": []},
                       {"version": 1, "terms": [""]}, {"version": 2, "terms": ["example"]}):
            with self.subTest(policy=policy):
                self.assertEqual(self.run_guard(policy).returncode, 2)

    def test_worktree_case_separator_and_unicode_variants_are_blocked(self):
        for text in ("private-example", "ＰＲＩＶＡＴＥＥＸＡＭＰＬＥ", "虚构\u200b保留名"):
            with self.subTest(text=text):
                (self.root / "draft.txt").write_text(text, encoding="utf-8")
                self.assert_blocked(self.run_guard())

    def test_escaped_and_encoded_text_is_blocked(self):
        values = (json.dumps("虚构保留名"), "%50rivateExample", "Private&#69;xample")
        for text in values:
            with self.subTest(text=text):
                (self.root / "draft.txt").write_text(text, encoding="utf-8")
                self.assert_blocked(self.run_guard())

    def test_utf16_and_binary_strings_are_blocked(self):
        variants = ["虚构保留名".encode(encoding)
                    for encoding in ("utf-16", "utf-16-le", "utf-16-be", "gb18030")]
        variants.append(b"\x89PNG\x00PrivateExample\xff")
        for data in variants:
            with self.subTest(data=data):
                (self.root / "asset.bin").write_bytes(data)
                self.assert_blocked(self.run_guard())

    def test_sensitive_filename_is_blocked_without_printing_it(self):
        (self.root / "PrivateExample.txt").write_bytes(b"public")
        self.assert_blocked(self.run_guard())

    def test_index_cannot_hide_behind_a_clean_worktree(self):
        path = self.root / "draft.txt"
        path.write_text("PrivateExample", encoding="utf-8")
        self.git("add", "draft.txt")
        path.write_text("public", encoding="utf-8")
        self.assert_blocked(self.run_guard())

    def test_deleted_historical_content_is_blocked(self):
        path = self.root / "draft.txt"
        path.write_text("PrivateExample", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "Add draft")
        self.git("rm", "-q", "draft.txt")
        self.git("commit", "-qm", "Remove draft")
        self.assert_blocked(self.run_guard())

    def test_commit_message_is_blocked(self):
        self.git("commit", "--allow-empty", "-qm", "PrivateExample details")
        self.assert_blocked(self.run_guard())

    def test_proposed_commit_message_is_blocked_before_commit(self):
        message = self.root / ".git/COMMIT_EDITMSG"
        message.write_text("PrivateExample details", encoding="utf-8")
        self.assert_blocked(self.run_guard(extra_args=("--message-file", str(message))))

    def test_other_branch_and_tag_metadata_are_blocked(self):
        self.git("tag", "PrivateExample")
        self.assert_blocked(self.run_guard())
        self.git("tag", "-d", "PrivateExample")
        self.git("checkout", "-qb", "draft")
        (self.root / "draft.txt").write_text("PrivateExample", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "Draft")
        self.git("checkout", "-q", "main")
        self.assert_blocked(self.run_guard())

    def test_invalid_directory_fails_closed(self):
        self.assertEqual(self.run_guard(root=self.root / "missing").returncode, 2)

    def test_detached_history_without_branch_is_blocked(self):
        self.git("commit", "--allow-empty", "-qm", "PrivateExample details")
        self.git("checkout", "-q", "--detach")
        self.git("branch", "-D", "main")
        self.assert_blocked(self.run_guard())

    def test_replace_refs_cannot_hide_original_commit(self):
        self.git("commit", "--allow-empty", "-qm", "PrivateExample details")
        original = self.git("rev-parse", "HEAD").decode().strip()
        tree = self.git("rev-parse", "HEAD^{tree}").decode().strip()
        clean = self.git("commit-tree", tree, "-m", "Public message").decode().strip()
        self.git("replace", original, clean)
        self.assert_blocked(self.run_guard())

    def test_shallow_repository_fails_closed(self):
        (self.root / ".git/shallow").write_bytes(self.git("rev-parse", "HEAD"))
        self.assertEqual(self.run_guard().returncode, 2)


class PublicationWorkflowTests(unittest.TestCase):
    def test_generated_public_is_checked_before_any_public_summary(self):
        workflow = SCRIPT.parent / ".github/workflows/maintain.yml"
        if not workflow.is_file():
            self.skipTest("通用检查器独立归档不携带业务工作流")
        text = workflow.read_text(encoding="utf-8")
        generation = text.index("      - name: 每周轮换播放抽检")
        gate = text.index("      - name: 发布前复核生成文件")
        summary = text.index("      - name: 写入运行摘要")
        publish = text.index("      - name: 发布到固定订阅地址")
        self.assertLess(generation, gate)
        self.assertLess(gate, summary)
        self.assertLess(summary, publish)


if __name__ == "__main__":
    unittest.main()
