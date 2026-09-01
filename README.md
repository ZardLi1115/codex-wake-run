<h1 align="center">wake-run-skill</h1>

<p align="center">A Codex Skill that runs long commands in a detached watcher and wakes the originating Codex thread when the process exits, so the model never polls for status.</p>

<p align="center">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square" alt="Python 3.12"> <a href="https://linux.do/latest"><img src="https://img.shields.io/badge/Linux.do-Community-7C3AED?style=flat-square" alt="Linux.do community"></a>
</p>

Long experiments create an awkward choice inside an agent session: either the model sits in a polling loop burning turns while it waits, or you lose the thread of the original task and have to re-explain it later.

wake-run removes the wait. You hand it the finalized command; it spawns a detached watcher, prints `status: armed`, and the current turn ends right away. The watcher blocks on the operating system process-exit event. When the command finishes, succeeds or fails, the watcher uses `codex queue` to inject a wake-up message back into the same thread that launched it, carrying the exit code and log path. The model picks the original task back up with its context intact.

## Highlights

| Highlight | Why it matters |
|---|---|
| Event-driven, not polled | The watcher blocks on `process.wait()`. There is no sleep loop, timer, or status check in the runtime. |
| The turn ends immediately | The launcher spawns a detached worker, prints one JSON line, and exits, so no model turn is spent waiting. |
| Wakes the same thread | The watcher calls `codex queue --thread "$CODEX_THREAD_ID"`, so the continuation lands in the conversation that started the job. |
| Failures wake you too | A non-zero exit and a failed process launch both produce a wake-up message. |
| Windows and POSIX | Commands run through PowerShell on Windows and `/bin/sh` on POSIX, including `codex.ps1` shim handling. |
| One log per run | Each run streams stdout and stderr into `<cwd>/.codex-wake-run/<run_id>.log`. |

## Architecture

```text
Codex thread
    │
    │ launch wake-run
    ▼
wake_run.py launcher
    │
    │ detached worker, then current turn ends
    ▼
experiment process
    │
    │ process.wait()
    ▼
exit code + log
    │
    │ codex queue --thread <originating thread>
    ▼
Codex thread wakes and continues
```

The launcher verifies `codex queue` support before it starts anything, so an incompatible Codex CLI fails fast instead of running your job and then failing to wake anyone.

## Usage Example

**You:**

```text
Use the wake-run skill to run this experiment and continue after it exits.
```

**Codex** launches the job and gets `armed` back:

```json
{"status": "armed", "run_id": "b7599ab35869", "worker_pid": 97153, "log_file": "/work/project/.codex-wake-run/b7599ab35869.log"}
```

It then ends the turn. Nothing polls the job.

When the script exits, the watcher queues a wake-up into that same thread:

```text
[后台任务唤醒通知]

脚本：echo "training started"; sleep 2; echo "done"
状态：执行完成
退出码：0
日志文件：/work/project/.codex-wake-run/b7599ab35869.log

请分析脚本执行结果，然后继续完成原任务。
若任务已经完成，请直接向用户发送最终结果。
若脚本执行失败，请分析失败原因，并在合理情况下修复后继续执行。

注：该消息由系统后台唤醒，并非用户亲自发出消息。
```

Codex reads the referenced log when needed and continues the original task.

## Quick Start

This repository is itself a standalone Skill. There is no plugin manifest or nested Skill directory.

Ask Codex to install it:

```text
Install the wake-run skill for me: https://github.com/ZardLi1115/codex-wake-run
```

Once installed, use it like this:

```text
Use the wake-run skill to run xxx for me.
```

A few things worth knowing:

- **It only works inside a Codex session.** The Skill needs `CODEX_THREAD_ID` to know which thread to wake.
- **Your Codex CLI needs `codex queue`.** The launcher preflights this before starting the experiment.
- **It is not a way around your sandbox.** The background process inherits the launch environment and its permissions.
- **One watcher per experiment.** Multiple watchers are fine when the task genuinely requires parallel jobs.

## Repository layout

| Path | What it holds |
|---|---|
| [`SKILL.md`](./SKILL.md) | Skill instructions: launch workflow, runtime contract, and wake-up message shape. |
| [`scripts/wake_run.py`](./scripts/wake_run.py) | Launcher and detached watcher. |
| [`agents/openai.yaml`](./agents/openai.yaml) | Agent-facing Skill metadata; implicit invocation is enabled. |
| [`tests/test_wake_run.py`](./tests/test_wake_run.py) | Unit, integration, Windows, and regression tests. |

Run logs are written to `.codex-wake-run/` in whichever project you launch from, and that directory is gitignored.

## Acknowledgements

Thanks to the [Linux.do](https://linux.do/latest) community for its support.
