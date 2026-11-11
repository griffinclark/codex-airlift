import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codex_airlift.manifest import plan, transition, verify_inventory
from codex_airlift.storage import read_json


def inventory(host="local"):
    return {"threads": [
        {"id": "caller", "kind": "codex", "hostId": host, "status": "active"},
        {"id": "a", "kind": "codex", "hostId": host, "status": "active", "title": "Example"},
        {"id": "b", "kind": "codex", "hostId": host, "status": "idle"},
        {"id": "old", "kind": "codex", "hostId": host, "status": "notLoaded"},
        {"id": "chatgpt", "kind": "chatgpt", "status": "idle"}],
        "pinnedThreads": [{"id": "a", "kind": "codex", "hostId": host, "status": "active", "title": "Example"}]}


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.manifest = plan(inventory(), "local", "remote-example", "caller")

    def test_scope_deduplication_and_caller_preservation(self):
        self.assertEqual([t["id"] for t in self.manifest["threads"]], ["a", "b"])
        self.assertEqual({r["id"] for r in self.manifest["excluded"]}, {"caller", "old"})
        self.assertEqual(self.manifest["threads"][1]["original_status"], "idle")

    def test_uncertain_submit_cannot_be_replayed(self):
        transition(self.manifest, "a", "claim")
        for thread in ("a", "b"):
            with self.assertRaises(ValueError):
                transition(self.manifest, thread, "claim")
        transition(self.manifest, "a", "submit", "operation-1")
        with self.assertRaises(ValueError):
            transition(self.manifest, "a", "submit", "operation-2")
        verify_inventory(self.manifest, "a", inventory("remote-example"))
        transition(self.manifest, "b", "claim")

    def test_wrong_host_or_unavailable_inventory_cannot_verify(self):
        transition(self.manifest, "a", "claim")
        with self.assertRaises(ValueError):
            verify_inventory(self.manifest, "a", inventory())
        unavailable = dict(inventory("remote-example"), unavailableHosts=["remote-example"])
        with self.assertRaises(ValueError):
            verify_inventory(self.manifest, "a", unavailable)
        self.assertEqual(self.manifest["threads"][0]["state"], "claimed")

    def test_readback_loss_reconciles_claim_without_resubmission(self):
        transition(self.manifest, "a", "claim")
        verify_inventory(self.manifest, "a", inventory("remote-example"))
        self.assertEqual(self.manifest["threads"][0]["state"], "verified")

    def test_cli_persists_journal_and_rejects_duplicate_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "inventory.json"
            source.write_text(json.dumps(inventory()))
            target = root / "batch.json"
            command = [sys.executable, "-m", "codex_airlift.cli", "manifest", "plan", "--file", str(target),
                       "--inventory", str(source), "--destination", "remote-example", "--caller", "caller"]
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            saved = copy.deepcopy(read_json(target))
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(saved, read_json(target))
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
