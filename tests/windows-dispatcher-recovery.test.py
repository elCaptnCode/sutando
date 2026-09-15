"""Run the production dispatcher with isolated, deterministic POSIX CLI shims."""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")


@unittest.skipUnless(
    PWSH and os.name == "posix",
    "Requires pwsh and POSIX CLI shims; Windows uses the live PowerShell suite",
)
class DispatcherRecoveryTest(unittest.TestCase):
    def test_crash_recovery_and_instance_ownership(self):
        with tempfile.TemporaryDirectory(prefix="dispatcher recovery ") as temp:
            workspace = pathlib.Path(temp)
            shim = workspace / "bin"
            for directory in (shim, workspace / "state", workspace / "tasks", workspace / "results"):
                directory.mkdir()
            (shim / "python").symlink_to(sys.executable)
            claude = shim / "claude"
            claude.write_text('#!/bin/sh\nprintf \'{"result":"PORTABLE_OWNER_OK"}\\n\'\n')
            claude.chmod(0o755)
            env = dict(
                os.environ,
                PATH=str(shim) + ":/usr/bin:/bin",
                SUTANDO_TEST_MODE="1",
                SUTANDO_WORKSPACE=temp,
            )
            pidfile = workspace / "state/task-dispatcher.pid"
            pidfile.write_text(str(os.getpid()))
            (workspace / "tasks/task-orphan.txt.processing").write_text("task: Do not retry\n")
            (workspace / "tasks/task-done.txt.processing").write_text("task: Already complete\n")
            (workspace / "results/task-done.txt").write_text("PREVIOUS_RESULT")
            command = [PWSH, "-NoProfile", "-File", str(REPO / "src/task-dispatcher.ps1")]
            with (workspace / "service.log").open("w+") as log:
                process = subprocess.Popen(command, env=env, stdout=log, stderr=log)

                def wait(path):
                    for _ in range(100):
                        if path.exists():
                            return
                        if process.poll() is not None:
                            log.seek(0)
                            self.fail(log.read())
                        time.sleep(0.1)
                    log.seek(0)
                    self.fail(f"Timeout: {path}\n{log.read()}")

                try:
                    wait(workspace / "tasks/archive/task-orphan.txt")
                    self.assertEqual(int(pidfile.read_text()), process.pid)
                    self.assertTrue((workspace / "results/task-orphan.txt").read_text().startswith(
                        "This task was interrupted."
                    ))
                    self.assertEqual((workspace / "results/task-done.txt").read_text(), "PREVIOUS_RESULT")
                    duplicate = subprocess.run(
                        command + ["-ValidateOnly"], env=env, capture_output=True,
                        text=True, timeout=20,
                    )
                    self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
                    self.assertIn("already running", duplicate.stdout)
                    self.assertEqual(int(pidfile.read_text()), process.pid)
                    (workspace / "tasks/task-new.txt").write_text("access_tier: owner\ntask: Return marker\n")
                    wait(workspace / "tasks/archive/task-new.txt")
                    self.assertEqual((workspace / "results/task-new.txt").read_text(), "PORTABLE_OWNER_OK")
                    process.kill()
                    process.wait(timeout=10)
                    (workspace / "tasks/task-crash.txt.processing").write_text("task: Interrupted\n")
                    process = subprocess.Popen(command, env=env, stdout=log, stderr=log)
                    wait(workspace / "tasks/archive/task-crash.txt")
                    (workspace / "tasks/task-after.txt").write_text("access_tier: owner\ntask: Return marker\n")
                    wait(workspace / "tasks/archive/task-after.txt")
                    self.assertEqual((workspace / "results/task-after.txt").read_text(), "PORTABLE_OWNER_OK")
                finally:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
