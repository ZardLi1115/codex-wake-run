from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wake_run.py"
SPEC = importlib.util.spec_from_file_location("wake_run", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
wake_run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wake_run)


def python_shell_command(source: str) -> str:
    if os.name == "nt":
        executable = str(Path(sys.executable)).replace("'", "''")
        code = source.replace("'", "''")
        return f"& '{executable}' -c '{code}'"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


class WakeMessageTests(unittest.TestCase):
    def test_success_message_contains_fixed_system_note(self) -> None:
        message = wake_run.build_wake_message("python train.py", 0, Path("/tmp/run.log"))
        self.assertTrue(message.startswith("[后台任务唤醒通知]"))
        self.assertIn("状态：执行完成", message)
        self.assertIn("退出码：0", message)
        self.assertIn("注：该消息由系统后台唤醒，并非用户亲自发出消息。", message)

    def test_failure_message_reports_nonzero_exit(self) -> None:
        message = wake_run.build_wake_message("python train.py", 7, Path("/tmp/run.log"))
        self.assertIn("状态：执行失败", message)
        self.assertIn("退出码：7", message)


class InvocationTests(unittest.TestCase):
    def test_windows_experiment_uses_powershell_not_cmd(self) -> None:
        invocation = wake_run.build_experiment_invocation(
            "& '.\\experiment.ps1'",
            platform="nt",
            powershell_bin=r"C:\Program Files\PowerShell\7\pwsh.exe",
        )
        self.assertEqual(invocation[0], r"C:\Program Files\PowerShell\7\pwsh.exe")
        self.assertIn("-Command", invocation)
        self.assertEqual(invocation[-1], "& '.\\experiment.ps1'")
        self.assertNotIn("cmd.exe", " ".join(invocation).lower())

    def test_windows_codex_ps1_is_wrapped_with_powershell(self) -> None:
        invocation = wake_run.build_codex_invocation(
            r"C:\Users\me\AppData\Roaming\npm\codex.ps1",
            ["queue", "--thread", "thread-1", "--message", "hello\nworld"],
            platform="nt",
            powershell_bin="pwsh.exe",
        )
        self.assertEqual(
            invocation[:6],
            [
                "pwsh.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                r"C:\Users\me\AppData\Roaming\npm\codex.ps1",
            ],
        )
        self.assertEqual(invocation[-1], "hello\nworld")

    @mock.patch.object(wake_run.shutil, "which")
    def test_windows_codex_resolution_prefers_cmd_over_ps1(self, which: mock.Mock) -> None:
        candidates = {
            "codex.exe": None,
            "codex.cmd": r"C:\npm\codex.cmd",
            "codex.bat": None,
            "codex.ps1": r"C:\npm\codex.ps1",
            "codex": r"C:\npm\codex.ps1",
        }
        which.side_effect = lambda name: candidates.get(name)
        self.assertEqual(
            wake_run.resolve_codex_executable("codex", platform="nt"),
            r"C:\npm\codex.cmd",
        )


class QueueTests(unittest.TestCase):
    @mock.patch.object(wake_run, "resolve_codex_executable", return_value="/usr/bin/codex")
    @mock.patch.object(wake_run.subprocess, "run")
    def test_queue_wakeup_uses_originating_thread(self, run: mock.Mock, _resolve: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        wake_run.queue_wakeup("thread-123", "wake message", "codex")
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/codex", "queue", "--thread", "thread-123", "--message", "wake message"],
        )

    @mock.patch.object(wake_run, "resolve_codex_executable", return_value="/usr/bin/codex")
    @mock.patch.object(wake_run.subprocess, "run")
    def test_preflight_rejects_codex_without_queue(self, run: mock.Mock, _resolve: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess([], 2)
        with self.assertRaisesRegex(RuntimeError, "does not support"):
            wake_run.preflight_codex_queue("codex")

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell integration test")
    def test_real_codex_ps1_shim_receives_multiline_wake_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture = tmp_path / "capture.json"
            fake_codex = tmp_path / "codex.ps1"
            fake_codex.write_text(
                "$payload = ConvertTo-Json -Compress -InputObject @($args)\n"
                "$utf8 = New-Object System.Text.UTF8Encoding($false)\n"
                "[System.IO.File]::WriteAllText($env:WAKE_CAPTURE, $payload, $utf8)\n"
                "exit 0\n",
                encoding="utf-8",
            )
            message = "[后台任务唤醒通知]\n状态：执行完成\n注：系统后台唤醒"
            with mock.patch.dict(os.environ, {"WAKE_CAPTURE": str(capture)}):
                wake_run.queue_wakeup("thread-win", message, str(fake_codex))
            args = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(args[:4], ["queue", "--thread", "thread-win", "--message"])
            self.assertEqual(args[4], message)


class WorkerTests(unittest.TestCase):
    def test_worker_waits_for_success_and_queues_wakeup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "success.log"
            command = python_shell_command("print(12345)")
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
            self.assertIn("状态：执行完成", queue.call_args.args[1])

    def test_worker_waits_for_failure_and_queues_wakeup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "failure.log"
            command = python_shell_command("import sys; print(67890); sys.exit(9)")
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
            self.assertIn("状态：执行失败", queue.call_args.args[1])

    def test_runtime_source_has_no_poll_or_sleep_loop(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn(".poll(", source)
        self.assertNotIn("sleep(", source)
        self.assertNotIn("shell=True", source)
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
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--worker",
                    "--thread-id",
                    "thread-integration",
                    "--command",
                    python_shell_command("print(24680)"),
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
            args = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(args[:4], ["queue", "--thread", "thread-integration", "--message"])
            self.assertIn("状态：执行完成", args[4])

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell integration test")
    def test_windows_worker_executes_single_quoted_powershell_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script = tmp_path / "wake run test.ps1"
            script.write_text("Write-Output 'WINDOWS_WAKE_OK'\nexit 0\n", encoding="utf-8")
            log_file = tmp_path / "windows.log"
            with mock.patch.object(wake_run, "queue_wakeup"):
                exit_code = wake_run.run_worker(
                    thread_id="thread-windows",
                    command=f"& '{script}'",
                    cwd=tmp_path,
                    log_file=log_file,
                    codex_bin="codex",
                )
            self.assertEqual(exit_code, 0)
            self.assertIn("WINDOWS_WAKE_OK", log_file.read_text(encoding="utf-8", errors="replace"))


class LauncherTests(unittest.TestCase):
    @mock.patch.object(wake_run, "preflight_codex_queue", return_value="/resolved/codex")
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
        self.assertIn("/resolved/codex", popen.call_args.args[0])


class LayoutTests(unittest.TestCase):
    def test_repo_root_is_the_skill(self) -> None:
        self.assertTrue((ROOT / "SKILL.md").is_file())
        self.assertTrue((ROOT / "agents" / "openai.yaml").is_file())
        self.assertTrue((ROOT / "scripts" / "wake_run.py").is_file())
        self.assertFalse((ROOT / ".codex-plugin").exists())
        self.assertFalse((ROOT / "skills").exists())
        skill_text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("[TODO:", skill_text)
        self.assertTrue(skill_text.startswith("---\nname: wake-run\n"))

    def test_readmes_do_not_document_direct_launcher_options(self) -> None:
        english = (ROOT / "README.md").read_text(encoding="utf-8")
        chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        self.assertNotIn("## Launcher options", english)
        self.assertNotIn("To call the launcher directly", english)
        self.assertNotIn("## 启动器参数", chinese)
        self.assertNotIn("如果想绕过 skill 直接调用启动器", chinese)
        self.assertNotIn("skills/wake-run/", english)
        self.assertNotIn("skills/wake-run/", chinese)


if __name__ == "__main__":
    unittest.main()
