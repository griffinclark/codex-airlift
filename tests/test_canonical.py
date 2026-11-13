import json
from pathlib import Path
import tempfile
import unittest

from codex_airlift.canonical import CanonicalExporter, import_canonical, portable_history, import_portable, header
from codex_airlift.storage import private_dir


PARENT = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"


def records(identifier, base=None, start=0):
    meta = {"id": identifier}
    if base:
        meta["history_base"] = base
    values = [{"ordinal": start, "type": "session_meta", "payload": meta},
              {"ordinal": start + 1, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "saved-turn"}}]
    return b"".join((json.dumps(v) + "\n").encode() for v in values)


class CanonicalTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.home = private_dir(self.root / "source")
        self.sessions = private_dir(self.home / "sessions/2026/01/01")
        self.bundle = self.root / "bundle"
        self.target = self.root / "target"

    def path(self, identifier, suffix=""):
        return self.sessions / ("rollout-2026-01-01T00-00-00-" + identifier + suffix + ".jsonl")

    def test_exact_parent_prefix_and_child_identity_survive_import(self):
        prefix = records(PARENT)
        parent = self.path(PARENT)
        parent.write_bytes(prefix + (json.dumps({"ordinal": 2, "type": "event_msg", "payload": {"later": True}}) + "\n").encode())
        child = self.path(CHILD)
        child.write_bytes(records(CHILD, {"thread_id": PARENT, "end_ordinal_exclusive": 2,
                                         "end_byte_offset": len(prefix)}, 2))
        manifest = CanonicalExporter(self.home).export(child, self.bundle)
        self.assertEqual([p["thread_id"] for p in manifest["files"]], [PARENT, CHILD])
        current, imported = import_canonical(self.bundle, self.target)
        self.assertEqual(current.read_bytes(), child.read_bytes())
        self.assertEqual((self.target / imported["files"][0]["path"]).read_bytes(), prefix)

    def test_self_segment_has_a_strictly_earlier_history_base(self):
        prefix = records(PARENT)
        self.path(PARENT).write_bytes(prefix)
        current = self.path(PARENT, "_33333333-3333-4333-8333-333333333333")
        current.write_bytes(records(PARENT, {"thread_id": PARENT, "end_ordinal_exclusive": 2,
                                           "end_byte_offset": len(prefix)}, 2))
        manifest = CanonicalExporter(self.home).export(current, self.bundle)
        self.assertEqual(len(manifest["files"]), 2)
        self.assertEqual(manifest["thread_id"], PARENT)

    def test_missing_base_and_incomplete_tail_cannot_export_partial_history(self):
        child = self.path(CHILD)
        child.write_bytes(records(CHILD, {"thread_id": PARENT, "end_ordinal_exclusive": 2,
                                         "end_byte_offset": 100}, 2))
        with self.assertRaises(ValueError):
            CanonicalExporter(self.home).export(child, self.bundle)
        child.write_bytes(records(CHILD) + b'{"partial":')
        with self.assertRaises(ValueError):
            CanonicalExporter(self.home).export(child, self.root / "partial")

    def test_tampering_and_symlinks_cannot_replace_destination_history(self):
        source = self.path(PARENT)
        source.write_bytes(records(PARENT))
        manifest = CanonicalExporter(self.home).export(source, self.bundle)
        staged = self.bundle / manifest["current"]
        staged.write_bytes(staged.read_bytes().replace(b"saved-turn", b"other-turn"))
        with self.assertRaises(ValueError):
            import_canonical(self.bundle, self.target)
        link = self.path(CHILD)
        link.symlink_to(source)
        with self.assertRaises(ValueError):
            CanonicalExporter(self.home).export(link, self.root / "symlink")

    def test_import_cannot_follow_destination_parent_outside_the_home(self):
        source = self.path(PARENT)
        source.write_bytes(records(PARENT))
        CanonicalExporter(self.home).export(source, self.bundle)
        private_dir(self.target)
        outside = private_dir(self.root / "outside")
        (self.target / "sessions").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            import_canonical(self.bundle, self.target)
        self.assertEqual(list(outside.iterdir()), [])

    def test_flatten_preserves_model_records_and_maps_only_environment_header(self):
        parent = self.path(PARENT)
        prefix = records(PARENT)
        parent.write_bytes(prefix)
        child = self.path(CHILD)
        child.write_bytes(records(CHILD, {"thread_id": PARENT, "end_ordinal_exclusive": 2,
                                         "end_byte_offset": len(prefix)}, 2))
        CanonicalExporter(self.home).export(child, self.bundle)
        portable_history(self.bundle)
        original = (self.bundle / "portable.jsonl").read_bytes()
        target, manifest = import_portable(self.bundle, self.target, cwd="/cloud/work", workspace_root="/cloud")
        values = [json.loads(v) for v in target.read_text().splitlines()]
        self.assertEqual([v["ordinal"] for v in values], [0, 1, 2])
        self.assertEqual([v["payload"]["turn_id"] for v in values[1:]], ["saved-turn", "saved-turn"])
        self.assertEqual(header(target)["payload"], {"id": CHILD, "cwd": "/cloud/work", "runtime_workspace_roots": ["/cloud"]})
        self.assertEqual((self.bundle / "portable.jsonl").read_bytes(), original)
        import_portable(self.bundle, self.target, cwd="/cloud/work", workspace_root="/cloud")
        target.write_bytes(target.read_bytes().replace(b"saved-turn", b"other-turn"))
        with self.assertRaises(ValueError):
            import_portable(self.bundle, self.target, cwd="/cloud/work", workspace_root="/cloud")
