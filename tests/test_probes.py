from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_airlift.probes import disconnect_probe
from codex_airlift.storage import read_json, write_json


class ProbeTest(unittest.TestCase):
    def test_reconciliation_reuses_receipt_without_starting_a_model_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = {"thread_id": "fixture", "turn_id": "turn", "cwd": "/home/codex/projects/test",
                       "marker": ".fixture", "disconnected_at": "2026-01-01T00:00:00Z"}
            write_json(root / "disconnect-probe.json", receipt)
            def read_only(method, params):
                self.assertEqual(params["threadId"], "fixture")
                if method == "thread/read":
                    return {"thread": {"id": "fixture"}}
                if method == "thread/turns/list":
                    return {"data": [{"id": "turn", "status": "completed"}]}
                raise AssertionError("Reconciliation may only read the recorded thread")

            with patch("codex_airlift.probes.remote_rpc") as rpc, \
                 patch("codex_airlift.probes.subprocess.run", return_value=SimpleNamespace(stdout="airlift-complete")) as run, \
                 redirect_stdout(StringIO()):
                rpc.return_value.__enter__.return_value.request.side_effect = read_only
                disconnect_probe(root, {"name": "airlift"}, None, reconcile=True)
            self.assertEqual(read_json(root / "disconnect-probe.json")["saved_result"], "airlift-complete")
            self.assertEqual(run.call_args.args[0][-1], "cat -- /home/codex/projects/test/.fixture")

    def test_incomplete_receipt_is_preserved_without_a_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "disconnect-probe.json", {"thread_id": "fixture"})
            with patch("codex_airlift.probes.remote_rpc", side_effect=AssertionError("No model retry")):
                with self.assertRaises(ValueError):
                    disconnect_probe(root, {}, None, reconcile=True)
