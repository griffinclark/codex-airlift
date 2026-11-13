import io
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import os
from types import SimpleNamespace
from unittest.mock import patch
import contextlib

from codex_airlift.git_snapshot import snapshot, restore, state
from codex_airlift.packet import pack, unpack, verify_contents
from codex_airlift.runtime import RuntimeStore, terminal_arguments
from codex_airlift.storage import private_dir, write_json
from codex_airlift.transfer import receive, settings_from_row, resume_parameters, verify_settings
from codex_airlift.desktop_stage import verify_archived_workspace
from codex_airlift.client import cli_command


class PortableWorkTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def git(self, path, *args):
        return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.DEVNULL)

    def test_git_index_worktree_outputs_and_symlink_survive(self):
        source = private_dir(self.root / "source")
        self.git(source, "init", "-q")
        self.git(source, "config", "user.name", "Test")
        self.git(source, "config", "user.email", "test@example.invalid")
        (source / "tracked").write_text("committed\n")
        (source / ".gitignore").write_text("outputs/\n")
        self.git(source, "add", ".")
        self.git(source, "commit", "-qm", "fixture")
        # Host repositories can choose different abbreviation lengths (or Git
        # can choose one automatically from their different object counts).
        self.git(source, "config", "core.abbrev", "12")
        (source / "tracked").write_text("staged\n")
        self.git(source, "add", "tracked")
        (source / "tracked").write_text("working\n")
        private_dir(source / "outputs")
        (source / "outputs/result.bin").write_bytes(bytes(range(256)))
        (source / "untracked").write_text("saved\n")
        (source / "link").symlink_to("untracked")
        before = state(source)
        bundle = self.root / "bundle"
        snapshot(source, bundle)
        cwd, manifest = restore(bundle, self.root / "target")
        self.assertEqual(state(cwd), before)
        self.assertEqual((cwd / "outputs/result.bin").read_bytes(), bytes(range(256)))
        self.assertEqual((cwd / "link").readlink(), Path("untracked"))
        with self.assertRaises(ValueError):
            restore(bundle, cwd)

    def test_packet_rejects_traversal_links_and_duplicates(self):
        for index, (name, kind) in enumerate((("../escape", "file"), ("safe", "link"), ("same", "duplicate"))):
            archive = self.root / (str(index) + ".tar")
            with tarfile.open(archive, "w") as stream:
                entry = tarfile.TarInfo(name)
                if kind == "link":
                    entry.type = tarfile.SYMTYPE
                    entry.linkname = "/tmp/outside"
                    stream.addfile(entry)
                else:
                    entry.size = 1
                    stream.addfile(entry, io.BytesIO(b"x"))
                    if kind == "duplicate":
                        stream.addfile(entry, io.BytesIO(b"y"))
            with self.assertRaises(ValueError):
                unpack(archive, self.root / ("target" + str(index)))
        self.assertFalse((self.root / "escape").exists())

    def test_prearchive_capture_preserves_index_after_managed_checkout_removal(self):
        repo = private_dir(self.root / "repo")
        self.git(repo, "init", "-q")
        self.git(repo, "config", "user.name", "Test")
        self.git(repo, "config", "user.email", "test@example.invalid")
        (repo / "file").write_text("base\n")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "base")
        source = self.root / "managed"
        self.git(repo, "worktree", "add", "--detach", str(source), "HEAD")
        (source / "file").write_text("staged\n")
        self.git(source, "add", "file")
        (source / "file").write_text("working\n")
        (source / "new").write_text("saved\n")
        before = state(source)
        bundle = self.root / "capture"
        snapshot(source, bundle)
        task_index = self.root / "snapshot-index"
        env = dict(os.environ, GIT_INDEX_FILE=str(task_index))
        def isolated(*args, input=None):
            return subprocess.check_output(["git", "-C", str(source), *args], env=env, input=input).strip()
        isolated("read-tree", "HEAD")
        isolated("add", "-A")
        tree = isolated("write-tree").decode()
        sha = isolated("commit-tree", tree, "-p", "HEAD", input=b"Codex worktree snapshot: archive-cleanup\n").decode()
        ref = "refs/codex/snapshots/" + hashlib.sha1(str(source).encode()).hexdigest()
        self.git(repo, "update-ref", ref, sha)
        self.git(repo, "worktree", "remove", "--force", str(source))
        self.assertFalse(source.exists())
        verify_archived_workspace(bundle)
        restored, _ = restore(bundle, self.root / "restored")
        self.assertEqual(state(restored), before)

    def test_packet_roundtrip_and_incomplete_import_are_not_replayed(self):
        folder = private_dir(self.root / "folder")
        (folder / "data").write_bytes(b"private packet")
        archive = self.root / "packet.tar"
        pack(folder, archive)
        target = unpack(archive, self.root / "extracted")
        self.assertEqual((target / "data").read_bytes(), b"private packet")
        verify_contents(archive, target)
        (target / "data").write_bytes(b"altered packet")
        with self.assertRaises(ValueError):
            verify_contents(archive, target)
        store = RuntimeStore(self.root / "store", "/bin/false", self.root / "auth")
        op = "11111111-1111-4111-8111-111111111111"
        receipt = store.base / "operations" / op / "import.json"
        write_json(receipt, {"state": "importing", "packet_sha256": "original"})
        with self.assertRaisesRegex(ValueError, "Incomplete destination"):
            receive(store, op, archive, "original")
        with self.assertRaisesRegex(ValueError, "differs"):
            receive(store, op, archive, "another")

    def test_terminal_uses_explicit_host_socket_and_preserves_policy(self):
        record = {"thread_id": "thread", "cwd": "/remote/work space", "settings": {
            "model": "model-name", "reasoningEffort": "high", "approvalPolicy": "on-request", "sandbox": "read-only"}}
        command = terminal_arguments("/vendor/codex", "/private/host.sock", record)
        self.assertEqual(command[:7], ["/vendor/codex", "resume", "thread", "--remote", "unix:///private/host.sock", "--cd", "/remote/work space"])
        self.assertNotIn("--ask-for-approval", command)
        self.assertNotIn("--sandbox", command)

    def test_reasoning_effort_is_a_config_override_and_effective_settings_are_checked(self):
        settings = {"model": "model-name", "reasoningEffort": "xhigh", "approvalPolicy": "never", "sandbox": "danger-full-access"}
        params = resume_parameters("thread", "/work", settings)
        self.assertEqual(params["config"], {"model_reasoning_effort": "xhigh"})
        self.assertNotIn("reasoningEffort", params)
        response = {"model": "model-name", "reasoningEffort": None, "approvalPolicy": "never", "sandbox": {"type": "dangerFullAccess"}}
        with self.assertRaisesRegex(ValueError, "settings differ"):
            verify_settings(response, settings)
        response["reasoningEffort"] = "xhigh"
        self.assertEqual(verify_settings(response, settings), settings)
        store = RuntimeStore(self.root / "runtime", "/bin/false", self.root / "auth")
        store.configure("11111111-1111-4111-8111-111111111111", settings)
        config = (Path(store.info("11111111-1111-4111-8111-111111111111")["home"]) / "config.toml").read_text()
        self.assertIn('model_reasoning_effort = "xhigh"', config)

    def test_unknown_source_policy_cannot_become_unrestricted(self):
        row = {"sandbox_policy": json.dumps({"type": "unknown"}), "approval_mode": "never", "model": None, "reasoning_effort": None}
        with self.assertRaises(ValueError):
            settings_from_row(row)
        row["sandbox_policy"] = json.dumps({"type": "workspace-write", "writable_roots": ["/other"]})
        with self.assertRaises(ValueError):
            settings_from_row(row)
        row["sandbox_policy"] = json.dumps({"type": "workspace-write", "exclude_slash_tmp": True})
        with self.assertRaises(ValueError):
            settings_from_row(row)

    def test_all_chat_retry_keeps_original_batch_selection_after_ownership_changes(self):
        root = private_dir(self.root / "deployment")
        write_json(root / "deployment.json", {"name": "test-host"})
        batch_id = "11111111-1111-4111-8111-111111111111"
        ids = ["22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333"]
        journal = root / "client/batches" / (batch_id + ".json")
        write_json(journal, {"state": "running", "threads": ids, "destination": "local", "results": {}})
        args = SimpleNamespace(command="batch", state_dir=str(root), source_dir=None, batch=batch_id,
                               inventory=None, to="local", desktop=False, workspace_map=None,
                               desktop_home="/source", desktop_binary="/vendor/codex", reconcile_workspace=False)
        with patch("codex_airlift.client.remote", side_effect=AssertionError("Must not reselect the remaining owned subset")), \
             patch("codex_airlift.client.move", side_effect=lambda root, tid, *a, **kw: {"state": "verified", "thread_id": tid}) as moved, \
             contextlib.redirect_stdout(io.StringIO()):
            cli_command(args)
        self.assertEqual([call.args[1] for call in moved.call_args_list], ids)
