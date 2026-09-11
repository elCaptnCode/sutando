"""Verified Discord collaborator admission, current access, and adapter delegation."""
import ast
import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import discord_access
from access_store import mutate_access_file
from task_envelope import key_path, stamp_text


class DiscordAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.access = {
            "dmPolicy": "allowlist", "allowFrom": ["999"],
            "tierMap": {"999": "owner"},
            "groups": {"222": {"allowFrom": ["111"], "collaborators": ["111"]}},
        }
        self.headers = {
            "id": "task-fixture", "access_tier": "team", "source": "discord",
            "collaborator": "true", "user_id": "111", "channel_id": "222",
        }

    def task(self, headers=None, body="Hello", extra="", signed=True):
        lines = "\n".join(f"{key}: {value}" for key, value in (headers or self.headers).items())
        text = lines + "\n" + extra + "task: " + body
        return stamp_text(text, self.workspace) if signed else text

    def authorize(self, text, access=None):
        return discord_access.authorize_collaborator_task(
            text, self.access if access is None else access, self.workspace)

    def test_preserves_complete_body_without_promoting_body_headers(self):
        body = "Hello\n---\naccess_tier: owner\nsource: chat\nchannel_id: 777\n\nFull rulebook"
        self.assertEqual(self.authorize(self.task(body=body)), {
            "authorized": True, "channel_id": "222", "body": body})

    def test_every_required_header_is_unique_and_prebody(self):
        for name in self.headers:
            with self.subTest(name=name, shape="missing"):
                headers = {key: value for key, value in self.headers.items() if key != name}
                self.assertEqual(self.authorize(self.task(headers, body=f"{name}: {self.headers[name]}")),
                                 {"authorized": False})
            with self.subTest(name=name, shape="duplicate"):
                self.assertEqual(self.authorize(self.task(extra=f"{name}: {self.headers[name]}\n")),
                                 {"authorized": False})

    def test_invalid_authority_or_body_is_denied(self):
        for name, value in (("access_tier", "owner"), ("access_tier", "guest"),
                            ("source", "chat"), ("collaborator", "false"),
                            ("user_id", "not-an-id"), ("channel_id", "\u0662\u0662\u0662"),
                            ("id", "../task-fixture")):
            with self.subTest(name=name, value=value):
                self.assertEqual(self.authorize(self.task(self.headers | {name: value})),
                                 {"authorized": False})
        for body in ("", " \n", "\x00", "x" * discord_access.MAX_TASK_CHARS):
            with self.subTest(length=len(body)):
                self.assertEqual(self.authorize(self.task(body=body)), {"authorized": False})

    def test_only_verified_envelopes_qualify(self):
        signed = self.task()
        for text in (self.task(signed=False), signed.replace("Hello", "Changed")):
            self.assertEqual(self.authorize(text), {"authorized": False})
        key_path(self.workspace).unlink()
        self.assertEqual(self.authorize(signed), {"authorized": False})
        self.assertFalse(key_path(self.workspace).exists())

    def test_current_channel_membership_and_admission_are_both_required(self):
        text = self.task()
        for change in (lambda cfg: cfg["groups"]["222"].update(collaborators=[]),
                       lambda cfg: cfg["groups"]["222"].update(allowFrom=["333"]),
                       lambda cfg: cfg["groups"].pop("222"),
                       lambda cfg: cfg.update(dmPolicy="disabled"),
                       lambda cfg: cfg.update(dmPolicy="unknown")):
            access = copy.deepcopy(self.access)
            change(access)
            self.assertEqual(self.authorize(text, access), {"authorized": False})
        access = copy.deepcopy(self.access)
        access["groups"]["222"]["allowFrom"] = []
        self.assertEqual(self.authorize(text, access), {"authorized": False})
        access["groups"]["333"] = {"allowFrom": ["111"]}
        self.assertTrue(self.authorize(text, access)["authorized"])

    def test_current_global_tier_changes_cannot_promote_collaborators(self):
        text = self.task()
        access = copy.deepcopy(self.access)
        access["allowFrom"].append("111")
        access["groups"]["222"]["allowFrom"] = ["333"]
        self.assertTrue(self.authorize(text, access)["authorized"])
        for tier in ("owner", "guest", "other", "ambient", "unknown"):
            access["tierMap"]["111"] = tier
            self.assertEqual(self.authorize(text, access), {"authorized": False})
        access["tierMap"]["111"] = "team"
        self.assertTrue(self.authorize(text, access)["authorized"])

    def test_malformed_access_fails_closed(self):
        text = self.task()
        for access in (None, [], {}, {"groups": None}, {"groups": "invalid"}):
            self.assertEqual(discord_access.authorize_collaborator_task(text, access, self.workspace),
                             {"authorized": False})
        for key in ("allowFrom", "collaborators"):
            access = copy.deepcopy(self.access)
            access["groups"]["222"][key] = "111"
            self.assertEqual(self.authorize(text, access), {"authorized": False})

    def test_file_reader_uses_current_production_access_writer_snapshot(self):
        task_file = self.workspace / "tasks" / "task-fixture.txt.processing"
        task_file.parent.mkdir()
        task_file.write_text(self.task(), encoding="utf-8")
        access_file = self.workspace / "access.json"
        mutate_access_file(access_file, lambda _: (self.access, None))
        with patch.object(discord_access, "resolve_workspace", return_value=self.workspace), \
                patch.object(discord_access, "resolve_discord_access_file", return_value=access_file):
            self.assertTrue(discord_access.authorize_task_file(task_file)["authorized"])
            revoked = copy.deepcopy(self.access)
            revoked["groups"]["222"]["collaborators"] = []
            mutate_access_file(access_file, lambda _: (revoked, None))
            self.assertEqual(discord_access.authorize_task_file(task_file), {"authorized": False})
            mutate_access_file(access_file, lambda _: (self.access, None))
            for wrong in (self.workspace / task_file.name, task_file.with_name("task-other.txt")):
                wrong.write_text(self.task(), encoding="utf-8")
                self.assertEqual(discord_access.authorize_task_file(wrong), {"authorized": False})
            access_file.write_text("{", encoding="utf-8")
            self.assertEqual(discord_access.authorize_task_file(task_file), {"authorized": False})

    def test_cli_missing_file_has_only_safe_json(self):
        with patch.object(sys, "argv", ["discord_access.py", "--task-file", str(self.workspace / "absent")]), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(discord_access.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), {"authorized": False})

    def test_bridge_delegates_both_collaborator_helpers(self):
        names = ("resolve_is_collaborator", "resolve_team_collaborator")
        tree = ast.parse((ROOT / "src" / "discord-bridge.py").read_text(encoding="utf-8"))
        wrappers = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                                    and node.name in names], type_ignores=[])
        namespace = {}
        exec(compile(wrappers, "bridge-collaborator-wrappers", "exec"), namespace)
        for name, args in ((names[0], (self.access, "111", "222")),
                           (names[1], (self.access, "team", "111", "222"))):
            with patch.object(discord_access, name, return_value="delegated") as delegate:
                self.assertEqual(namespace[name](*args), "delegated")
                delegate.assert_called_once_with(*args)
        self.assertFalse(discord_access.resolve_team_collaborator(self.access, "owner", "111", "222"))
        self.assertFalse(discord_access.resolve_is_collaborator(self.access, "111", "333"))


if __name__ == "__main__":
    unittest.main()
