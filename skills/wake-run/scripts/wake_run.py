#!/usr/bin/env python3
"""Detach a long-running command and wake the originating Codex thread on exit."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

WAKE_HEADER = "[后台任务唤醒通知]"
SYSTEM_NOTE = "注：该消息由系统后台唤醒，并非用户亲自发出消息。"


def build_wake_message(
    command: str,
    exit_code: int | None,
    log_file: Path,
    *,
    launch_error: str | None = None,
) -> str:
    status = "执行完成" if exit_code == 0 and launch_error is None else "执行失败"
    exit_text = str(exit_code) if exit_code is not None else "未启动"
    lines = [
        WAKE_HEADER,
        "",
        f"脚本：{command}",
        f"状态：{status}",
        f"退出码：{exit_text}",
        f"日志文件：{log_file}",
    ]
    if launch_error:
        lines.extend(["", f"启动错误：{launch_error}"])
    lines.extend(
        [
            "",
            "请分析脚本执行结果，然后继续完成原任务。",
            "若任务已经完成，请直接向用户发送最终结果。",
            "若脚本执行失败，请分析失败原因，并在合理情况下修复后继续执行。",
            "",
            SYSTEM_NOTE,
        ]
    )
    return "\n".join(lines)


def preflight_codex_queue(codex_bin: str) -> None:
    resolved = shutil.which(codex_bin)
    if resolved is None:
        raise RuntimeError(f"Codex CLI not found: {codex_bin}")
    result = subprocess.run(
        [resolved, "queue", "--help"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("This Codex CLI does not support `codex queue`.")


def queue_wakeup(thread_id: str, message: str, codex_bin: str) -> None:
    result = subprocess.run(
        [codex_bin, "queue", "--thread", thread_id, "--message", message],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"codex queue failed with exit code {result.returncode}: {detail}")


def run_worker(
    *,
    thread_id: str,
    command: str,
    cwd: Path,
    log_file: Path,
    codex_bin: str,
) -> int:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    exit_code: int | None = None
    launch_error: str | None = None

    with log_file.open("ab", buffering=0) as log:
        log.write(f"$ {command}\n".encode("utf-8", errors="replace"))
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                shell=True,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            exit_code = process.wait()
        except Exception as exc:  # wake-up must still be attempted
            launch_error = f"{type(exc).__name__}: {exc}"
            log.write(f"\n[wake-run] launch failed: {launch_error}\n".encode("utf-8", errors="replace"))

    message = build_wake_message(command, exit_code, log_file, launch_error=launch_error)
    try:
        queue_wakeup(thread_id, message, codex_bin)
    except Exception as exc:
        with log_file.open("ab", buffering=0) as log:
            log.write(f"\n[wake-run] wake-up failed: {type(exc).__name__}: {exc}\n".encode("utf-8", errors="replace"))
        return 70

    if launch_error is not None:
        return 127
    return exit_code if exit_code is not None else 1


def detached_popen_kwargs() -> dict[str, object]:
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        kwargs["start_new_session"] = True
    return kwargs


def arm_watcher(
    *,
    thread_id: str,
    command: str,
    cwd: Path,
    log_dir: Path,
    codex_bin: str,
) -> dict[str, object]:
    preflight_codex_queue(codex_bin)
    run_id = uuid.uuid4().hex[:12]
    log_file = (log_dir / f"{run_id}.log").resolve()
    log_file.parent.mkdir(parents=True, exist_ok=True)

    worker_args = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--thread-id",
        thread_id,
        "--command",
        command,
        "--cwd",
        str(cwd),
        "--log-file",
        str(log_file),
        "--codex-bin",
        codex_bin,
    ]
    worker = subprocess.Popen(worker_args, **detached_popen_kwargs())
    return {
        "status": "armed",
        "run_id": run_id,
        "worker_pid": worker.pid,
        "log_file": str(log_file),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a command in a detached watcher and wake the current Codex thread on exit."
    )
    parser.add_argument("--command", required=True, help="Exact shell command to execute.")
    parser.add_argument("--cwd", default=os.getcwd(), help="Working directory for the command.")
    parser.add_argument("--log-dir", help="Directory for run logs; defaults to <cwd>/.codex-wake-run.")
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI executable.")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--thread-id", help=argparse.SUPPRESS)
    parser.add_argument("--log-file", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cwd = Path(args.cwd).expanduser().resolve()

    if args.worker:
        if not args.thread_id or not args.log_file:
            raise SystemExit("worker mode requires --thread-id and --log-file")
        return run_worker(
            thread_id=args.thread_id,
            command=args.command,
            cwd=cwd,
            log_file=Path(args.log_file).expanduser().resolve(),
            codex_bin=args.codex_bin,
        )

    thread_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not thread_id:
        raise SystemExit("CODEX_THREAD_ID is missing; run wake-run from a Codex shell command.")

    log_dir = Path(args.log_dir).expanduser().resolve() if args.log_dir else cwd / ".codex-wake-run"
    result = arm_watcher(
        thread_id=thread_id,
        command=args.command,
        cwd=cwd,
        log_dir=log_dir,
        codex_bin=args.codex_bin,
    )
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
