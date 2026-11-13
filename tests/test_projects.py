import copy
from contextlib import nullcontext
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from codex_airlift.cli import desktop_project_status, setup_project
from codex_airlift.projects import project_key, register_project, verify_desktop_project
from codex_airlift.storage import read_json, write_json


LEASE = {"project": "example-project", "name": "airlift"}
REMOTE = "/home/codex/projects/example"


class Server:
    def __init__(self):
        self.projects = []
        self.creates = []
        self.lose_response = False
        self.wrong_readback = False

    def request(self, method, params):
        if method == "project/list":
            return {"data": copy.deepcopy(self.projects), "nextCursor": None}
        if method == "project/create":
            self.creates.append(copy.deepcopy(params))
            project = {"id": "server-project", "name": params["name"], "roots": params["roots"]}
            self.projects.append(copy.deepcopy(project))
            if self.lose_response:
                self.lose_response = False
                raise EOFError("Response lost after commit")
            return {"project": project}
        if method == "project/read":
            result = copy.deepcopy(next(p for p in self.projects if p["id"] == params["projectId"]))
            if self.wrong_readback:
                result["roots"] = [{"path": "/home/codex/projects/other"}]
            return {"project": result}
        raise AssertionError("Unexpected mutation or model turn: " + method)


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.journal = self.root / "project.json"
        self.server = Server()

    def register(self):
        return register_project(self.server, self.journal, LEASE, REMOTE, "Example")

    def test_lost_create_response_reconciles_without_duplicate_or_rename(self):
        self.server.lose_response = True
        with self.assertRaises(EOFError):
            self.register()
        pending = read_json(self.journal)
        self.assertEqual(pending["status"], "create-pending")
        recovered = self.register()
        self.assertEqual(recovered["project_id"], "server-project")
        self.assertEqual(recovered["status"], "server-registered")
        self.assertEqual(recovered["desktop_status"], "unverified")
        self.assertEqual(pending["idempotency_key"], recovered["idempotency_key"])
        self.assertEqual(len(self.server.creates), 1)
        self.register()
        self.assertEqual(len(self.server.creates), 1)

    def test_existing_root_is_adopted_without_creating_a_second_project(self):
        self.server.projects = [{"id": "existing", "name": "Human name", "roots": [{"path": REMOTE}]}]
        result = self.register()
        self.assertEqual(result["project_id"], "existing")
        self.assertEqual(result["project"]["name"], "Human name")
        self.assertFalse(self.server.creates)

    def test_lost_request_reuses_the_original_idempotency_key(self):
        class BeforeCommitFailure(Server):
            first = True

            def request(self, method, params):
                if method == "project/create" and self.first:
                    self.first = False
                    self.creates.append(copy.deepcopy(params))
                    raise EOFError("Transport closed before server commit")
                return super().request(method, params)

        self.server = BeforeCommitFailure()
        with self.assertRaises(EOFError):
            self.register()
        self.register()
        self.assertEqual(len(self.server.creates), 2)
        self.assertEqual(self.server.creates[0], self.server.creates[1])
        self.assertEqual(len(self.server.projects), 1)

    def test_existing_project_on_a_later_page_is_not_duplicated(self):
        class PagedServer(Server):
            def request(self, method, params):
                if method == "project/list" and "cursor" not in params:
                    return {"data": [], "nextCursor": "next"}
                return super().request(method, params)

        self.server = PagedServer()
        self.server.projects = [{"id": "later", "roots": [{"path": REMOTE}]}]
        self.assertEqual(self.register()["project_id"], "later")
        self.assertFalse(self.server.creates)

    def test_deleted_or_ambiguous_project_is_not_recreated(self):
        self.register()
        self.server.projects.clear()
        with self.assertRaises(ValueError):
            self.register()
        self.assertEqual(len(self.server.creates), 1)
        self.server.projects = [{"id": str(i), "roots": [{"path": REMOTE}]} for i in range(2)]
        with self.assertRaises(ValueError):
            self.register()
        self.assertEqual(len(self.server.creates), 1)

    def test_wrong_readback_never_marks_registration_complete(self):
        self.server.wrong_readback = True
        with self.assertRaises(ValueError):
            self.register()
        self.assertEqual(read_json(self.journal)["status"], "readback-pending")

    def test_parameter_change_and_path_traversal_are_rejected(self):
        self.register()
        with self.assertRaises(ValueError):
            register_project(self.server, self.journal, LEASE, REMOTE, "Different")
        for path in ("/tmp/example", "/home/codex/projects/../other", REMOTE + "/", REMOTE + "\n"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                project_key(LEASE, path)

    def test_pagination_loop_stops_before_create(self):
        class Loop:
            def request(self, method, params):
                if method != "project/list":
                    raise AssertionError("Must not create from an incomplete inventory")
                return {"data": [], "nextCursor": "same"}
        with self.assertRaises(ValueError):
            register_project(Loop(), self.journal, LEASE, REMOTE, "Example")

    def test_desktop_verification_requires_exact_host_root_and_git_identity(self):
        record = self.register()
        correct = {"projectId": "desktop-id", "hostId": "remote-example", "path": REMOTE,
                   "isGitRepository": True}
        for wrong in (dict(correct, hostId="local"), dict(correct, path=REMOTE + "/different"),
                      dict(correct, isGitRepository=False)):
            with self.assertRaises(ValueError):
                verify_desktop_project(record, {"projects": [wrong]}, "remote-example")
        with self.assertRaises(ValueError):
            verify_desktop_project(record, {"projects": [correct, correct]}, "remote-example")
        wrapped = {"content": [{"type": "text", "text": json.dumps({"projects": [correct]})}]}
        verified = verify_desktop_project(record, wrapped, "remote-example")
        self.assertEqual(verified["desktop_status"], "registered")
        self.assertEqual(verified["desktop_project_id"], "desktop-id")
        self.assertEqual(record["desktop_status"], "unverified")

    def test_setup_does_not_succeed_after_server_registration_alone(self):
        checkout = self.root / "example"
        checkout.mkdir()
        args = SimpleNamespace(path=str(checkout), folder=None, label=None, desktop_inventory=None)
        with patch("codex_airlift.cli.capture", return_value=str(checkout)), \
             patch("codex_airlift.cli.deployment", return_value=(self.root, LEASE)), \
             patch("codex_airlift.cli.configure_ssh"), patch("codex_airlift.cli.doctor"), \
             patch("codex_airlift.cli.prepare_repo"), patch("codex_airlift.cli.install_skills"), \
             patch("codex_airlift.cli.register_project", return_value={"project_id": "server-project"}), \
             redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit) as error:
                setup_project(args)
        self.assertEqual(error.exception.code, 3)
        result = read_json(self.root / ("setup-" + project_key(LEASE, REMOTE) + ".json"))
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["desktop_registered"])
        self.assertEqual(result["chats_moved"], 0)

    def test_stale_desktop_inventory_invalidates_previous_success(self):
        record = self.register()
        record.update(desktop_status="registered", desktop_verified_at=1, desktop_project_id="old")
        journal = self.root / "projects" / (project_key(LEASE, REMOTE) + ".json")
        write_json(journal, record)
        inventory = self.root / "desktop.json"
        write_json(inventory, {"projects": []})
        os.utime(inventory, (1, 1))
        args = SimpleNamespace(remote_cwd=REMOTE, desktop_inventory=str(inventory), host_id="remote-example")
        with patch("codex_airlift.cli.deployment", return_value=(self.root, LEASE)), \
             patch("codex_airlift.cli.validate_existing"), \
             patch("codex_airlift.probes.remote_rpc", side_effect=AssertionError("Stale readback must stop verification")):
            with self.assertRaises(ValueError):
                desktop_project_status(args)
        saved = read_json(journal)
        self.assertEqual(saved["desktop_status"], "unverified")
        self.assertNotIn("desktop_verified_at", saved)

    def test_setup_success_requires_server_and_desktop_readback(self):
        checkout = self.root / "example"
        checkout.mkdir()
        inventory = self.root / "desktop.json"
        write_json(inventory, {"projects": [{"projectId": "desktop-id", "hostId": "remote-example",
                                            "path": REMOTE, "isGitRepository": True}]})
        args = SimpleNamespace(path=str(checkout), folder=None, label=None,
                               desktop_inventory=str(inventory), host_id="remote-example")
        with patch("codex_airlift.cli.capture", return_value=str(checkout)), \
             patch("codex_airlift.cli.deployment", return_value=(self.root, LEASE)), \
             patch("codex_airlift.cli.validate_existing"), \
             patch("codex_airlift.cli.configure_ssh"), patch("codex_airlift.cli.doctor"), \
             patch("codex_airlift.cli.prepare_repo"), patch("codex_airlift.cli.install_skills"), \
             patch("codex_airlift.cli.ssh", return_value=SimpleNamespace(stdout="true\n")), \
             patch("codex_airlift.probes.remote_rpc", return_value=nullcontext(self.server)), \
             redirect_stdout(StringIO()):
            setup_project(args)
        saved = read_json(self.root / ("setup-" + project_key(LEASE, REMOTE) + ".json"))
        self.assertEqual(saved["status"], "ready")
        self.assertTrue(saved["desktop_registered"])
        self.assertEqual(saved["chats_moved"], 0)
        self.assertEqual(len(self.server.creates), 1)

    def test_failed_setup_cannot_leave_an_old_ready_receipt(self):
        checkout = self.root / "example"
        checkout.mkdir()
        receipt = self.root / ("setup-" + project_key(LEASE, REMOTE) + ".json")
        write_json(receipt, {"status": "ready", "desktop_registered": True})
        args = SimpleNamespace(path=str(checkout), folder=None, label=None, desktop_inventory=None)
        with patch("codex_airlift.cli.capture", return_value=str(checkout)), \
             patch("codex_airlift.cli.deployment", return_value=(self.root, LEASE)), \
             patch("codex_airlift.cli.configure_ssh"), \
             patch("codex_airlift.cli.doctor", side_effect=ValueError("Host unavailable")):
            with self.assertRaises(ValueError):
                setup_project(args)
        saved = read_json(receipt)
        self.assertEqual(saved["status"], "incomplete")
        self.assertFalse(saved["desktop_registered"])
        self.assertEqual(saved["reason"], "Host unavailable")
