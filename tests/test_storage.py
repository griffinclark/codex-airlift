from pathlib import Path
import tempfile
import unittest

from codex_airlift.storage import private_dir, read_json, write_json


class StorageTest(unittest.TestCase):
    def test_atomic_readback_and_no_permission_changes_to_shared_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder)
            path = parent / "state.json"
            write_json(path, {"version": 1})
            write_json(path, {"version": 2})
            self.assertEqual(read_json(path), {"version": 2})
            shared = parent / "shared"
            shared.mkdir(mode=0o755)
            with self.assertRaises(ValueError):
                private_dir(shared)
            self.assertEqual(shared.stat().st_mode & 0o777, 0o755)
