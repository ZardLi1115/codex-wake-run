from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "wake-run" / "scripts" / "wake_run.py"
SPEC = importlib.util.spec_from_file_location("wake_run", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
wake_run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wake_run)


class WakeMessageTests(unittest.TestCase):
    def test_success_message_contains_fixed_system_note(self) -> None:
        message = wake_run.build_wake_message("python train.py", 0, Path("/tmp/run.log"))
        self.assertTrue(message.startswith("[后台任务唤醒通知]"))
        self.assertIn("脚本：python train.py", message)
        self.assertIn("状态：执行完成", message)
        self.assertIn("退出码：0", message)
        self.assertIn("注：该消息由系统后台唤醒，并非用户亲自发出消息。", message)

    def test_failure_message_reports_nonzero_exit(self) -> None:
        message = wake_run.build_wake_message("python train.py", 7, Path("/tmp/run.log"))
        self.assertIn("状态：执行失败", message)
        self.assertIn("退出码：7", message)


class QueueTests(unittest.TestCase):
    @mock.patch.object(wake_run.subprocess, "run")
    def test_queue_wakeup_uses_originating_thread(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        wake_run.queue_wakeup("thread-123", "wake message", "codex")
        self.assertEqual(
            run.call_args.args[0],
            ["codex", "queue", "--thread", "thread-123", "--message", "wake message"],
        )

    @mock.patch.object(wake_run.shutil, "which", return_value="/usr/bin/codex")
    @mock.patch.object(wake_run.subprocess, "run")
    def test_preflight_rejects_codex_without_queue(self, run: mock.Mock, _which: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess([], 2)
        with self.assertRaisesRegex(RuntimeError, "does not support"):
            wake_run.preflight_codex_queue("codex")


class WorkerTests(unittest.TestCase):
    def test_worker_waits_for_success_and_queues_wakeup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "success.log"
            command = f'"{sys.executable}" -c "print(12345)"'
            with mock.patch.object(wake_run, "queue_wakeup") as queue:
                exit_code = wake_run.run_worker(
                    thread_id="thread-success",
                    command=command,
                    cwd=Path(tmp),
                    log_file=log_file,
                    codex_bin="codex",
                )
            self.assertEqual(exit_code, 0)
            self.assertIn("12345", log_file.read_text(encoding="utf-8"))
            message = queue.call_args.args[1]
            self.assertIn("状态：执行完成", message)
            self.assertIn("退出码：0", message)

    def test_worker_waits_for_failure_and_queues_wakeup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "failure.log"
            command = f'"{sys.executable}" -c "import sys; print(67890); sys.exit(9)"'
            with mock.patch.object(wake_run, "queue_wakeup") as queue:
                exit_code = wake_run.run_worker(
                    thread_id="thread-failure",
                    command=command,
                    cwd=Path(tmp),
                    log_file=log_file,
                    codex_bin="codex",
                )
            self.assertEqual(exit_code, 9)
            self.assertIn("67890", log_file.read_text(encoding="utf-8"))
            message = queue.call_args.args[1]
            self.assertIn("状态：执行失败", message)
            self.assertIn("退出码：9", message)

    def test_runtime_source_has_no_poll_or_sleep_loop(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn(".poll(", source)
        self.assertNotIn("sleep(", source)
        self.assertIn("process.wait()", source)

    @unittest.skipIf(os.name == "nt", "POSIX fake executable test")
    def test_worker_cli_sends_real_queue_command_to_fake_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture = tmp_path / "queue-args.txt"
            fake_codex = tmp_path / "codex"
            fake_codex.write_text(
                f"#!{sys.executable}\n"
                "import json, sys\n"
                f"open({str(capture)!r}, 'w', encoding='utf-8').write(json.dumps(sys.argv[1:], ensure_ascii=False))\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            log_file = tmp_path / "integration.log"
            command = f'"{sys.executable}" -c "print(24680)"'
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--worker",
                    "--thread-id",
                    "thread-integration",
                    "--command",
                    command,
                    "--cwd",
                    str(tmp_path),
                    "--log-file",
                    str(log_file),
                    "--codex-bin",
                    str(fake_codex),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("24680", log_file.read_text(encoding="utf-8"))
            args = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(args[0], "queue")
            self.assertEqual(args[1:3], ["--thread", "thread-integration"])
            self.assertEqual(args[3], "--message")
            self.assertIn("[后台任务唤醒通知]", args[4])
            self.assertIn("状态：执行完成", args[4])


class LauncherTests(unittest.TestCase):
    @mock.patch.object(wake_run, "preflight_codex_queue")
    @mock.patch.object(wake_run.subprocess, "Popen")
    def test_launcher_detaches_worker_and_returns_armed(self, popen: mock.Mock, _preflight: mock.Mock) -> None:
        popen.return_value.pid = 4242
        with tempfile.TemporaryDirectory() as tmp:
            result = wake_run.arm_watcher(
                thread_id="thread-abc",
                command="python train.py",
                cwd=Path(tmp),
                log_dir=Path(tmp) / "logs",
                codex_bin="codex",
            )
        self.assertEqual(result["status"], "armed")
        self.assertEqual(result["worker_pid"], 4242)
        worker_args = popen.call_args.args[0]
        self.assertIn("--worker", worker_args)
        self.assertIn("thread-abc", worker_args)
        self.assertIn("python train.py", worker_args)


class LayoutTests(unittest.TestCase):
    def test_plugin_manifest_and_skill_have_no_placeholders(self) -> None:
        manifest_path = ROOT / ".codex-plugin" / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "codex-wake-run")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertIn("defaultPrompt", manifest["interface"])
        skill_text = (ROOT / "skills" / "wake-run" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("[TODO:", skill_text)
        self.assertTrue(skill_text.startswith("---\nname: wake-run\n"))


if __name__ == "__main__":
    unittest.main()
