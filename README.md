<h1 align="center">Codex Wake Run</h1>

<p align="center">A Codex plugin that runs long commands in a detached watcher and wakes the originating Codex thread when the process exits, so the model never polls for status.</p>

<p align="center">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square" alt="Python 3.12"> <a href="https://linux.do/latest"><img src="https://img.shields.io/badge/Linux.do-Community-7C3AED?style=flat-square" alt="Linux.do community"></a>
</p>

Long experiments create an awkward choice inside an agent session: either the model sits in a polling loop burning turns while it waits, or you lose the thread of the original task and have to re-explain it later.

Codex Wake Run removes the wait. You hand it the finalized command; it spawns a detached watcher, prints `status: armed`, and the current turn ends right away. The watcher blocks on the operating system process-exit event. When the command finishes, succeeds or fails, the watcher uses `codex queue` to inject a wake-up message back into the same thread that launched it, carrying the exit code and log path. The model picks the original task back up with its context intact.

## Highlights

| Highlight | Why it matters |
|---|---|
| Event-driven, not polled | The watcher blocks on `process.wait()`. There is no sleep loop, timer, or status check anywhere in the runtime, and a regression test asserts this. |
| The turn ends immediately | The launcher spawns a detached worker, prints one JSON line, and exits, so no model turn is spent waiting. |
| Wakes the same thread | The watcher calls `codex queue --thread "$CODEX_THREAD_ID"`, so the continuation lands in the conversation that started the job, not a new one. |
| Failures wake you too | A non-zero exit and a failed process launch both produce a wake-up message with the exit code, so a broken job does not simply go quiet. |
| Windows and POSIX | Commands run through PowerShell on Windows and `/bin/sh` on POSIX, including `codex.ps1` shim handling. CI runs the suite on `ubuntu-latest` and `windows-latest`. |
| One log per run | Each run streams stdout and stderr into `<cwd>/.codex-wake-run/<run_id>.log`, and the wake-up message names that exact file. |

## Architecture

```text
┌──────────────────────────────────────────────────┐
│  Codex thread                                    │
│  CODEX_THREAD_ID injected by Codex               │◀─────┐
└──────┬───────────────────────────────────────────┘      │
       │ shell command                                    │
       ▼                                                  │
┌──────────────────────────────────────────────────┐      │
│  wake_run.py (launcher)                          │      │
│  preflight: codex queue --help                   │      │
│  prints status: armed                            │      │
└──────┬───────────────────────────────────────────┘      │
       │ spawn detached, then the turn ends               │
       ▼                                                  │
┌──────────────────────────────────────────────────┐      │
│  Detached watcher (one per run)                  │      │
│  ┌────────────────────────────────────────────┐  │      │
│  │  Experiment process                        │  │      │
│  │  PowerShell on Windows, /bin/sh on POSIX   │  │      │
│  └───┬────────────────────────────────────────┘  │      │
│      │ stdout and stderr                         │      │
│      ▼                                           │      │
│  ┌────────────────────────────────────────────┐  │      │
│  │  Run log file                              │  │      │
│  │  <cwd>/.codex-wake-run/<run_id>.log        │  │      │
│  └────────────────────────────────────────────┘  │      │ codex queue --thread <id>
│  process.wait() -> exit code -> wake-up          ├──────┘
└──────────────────────────────────────────────────┘
```

The launcher verifies `codex queue` support before it starts anything, so an incompatible Codex CLI fails fast instead of running your job and then failing to wake anyone.

## Usage Example

Launch a training run from inside a Codex session, keeping your project as the working directory:

```bash
python3 /path/to/codex-wake-run/skills/wake-run/scripts/wake_run.py \
  --command 'echo "training started"; sleep 2; echo "done"'
```

The launcher returns one line and exits:

```json
{"status": "armed", "run_id": "b7599ab35869", "worker_pid": 97153, "log_file": "/work/project/.codex-wake-run/b7599ab35869.log"}
```

Once `status` is `armed`, the turn ends. Nothing polls the job. When the command exits, the watcher queues this message into the originating thread:

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

The wake-up message is fixed in shape and always reports the command, status, exit code, and log path. On failure the status line reads `执行失败` and the exit code is the real non-zero code; if the process could not be launched at all, an `启动错误` line is appended. Meanwhile the log file holds the command line followed by its combined output:

```text
$ echo "training started"; sleep 2; echo "done"
training started
done
```

## Quick Install

Clone the repository anywhere on the machine that runs Codex:

```bash
git clone https://github.com/ZardLi1115/codex-wake-run.git
```

There is nothing to build and there are no third-party dependencies; the watcher uses only the Python standard library. The skill lives at `skills/wake-run/`, and its launcher is invoked by absolute path, so the clone location is up to you.

## Quick Start

wake-run only works from inside a Codex session, because it needs the thread it is supposed to wake.

1. **Confirm your Codex CLI supports `codex queue`.** The launcher preflights this and refuses to start otherwise:

   ```bash
   codex queue --help
   ```

2. **Ask Codex to use the skill.** From a Codex session in your project, the plugin's own suggested prompt is:

   ```text
   Use wake-run to launch this experiment and continue after it exits.
   ```

   Codex invokes the launcher for you. To run it directly instead, call it by absolute path while keeping your project as the working directory:

   ```bash
   python3 /path/to/codex-wake-run/skills/wake-run/scripts/wake_run.py \
     --command '<exact command>'
   ```

   Use `python` rather than `python3` on Windows.

3. **Read the JSON, then stop.** A `status` of `armed` means the watcher owns the job. End the turn. Do not tail the log, check the process, or sleep.

4. **Continue when the wake-up arrives.** The message beginning `[后台任务唤醒通知]` is a system-generated continuation event, not a new user instruction. Read the named log if you need detail, then resume the original task: analyze the result on success, or diagnose and relaunch on failure.

If `CODEX_THREAD_ID` is not set, the launcher exits with `CODEX_THREAD_ID is missing; run wake-run from a Codex shell command.` Codex injects that variable into shell command environments, so this error normally means the command was run outside a Codex session.

## Launcher options

`skills/wake-run/scripts/wake_run.py` accepts:

| Option | Default | Purpose |
|---|---|---|
| `--command` | required | The exact shell command to execute. Finalize its arguments before launching. |
| `--cwd` | current directory | Working directory for the command. |
| `--log-dir` | `<cwd>/.codex-wake-run` | Directory for run logs. |
| `--codex-bin` | `codex` | Codex CLI executable used for the preflight and the wake-up. |

The detached watcher exits with the command's own exit code, `127` if the process could not be launched, or `70` if the command finished but `codex queue` failed. In that last case the reason is appended to the run log, since there is no thread left to report it to.

## Runtime requirements

- **A Codex session.** `CODEX_THREAD_ID` must be present in the environment; Codex provides it.
- **A Codex CLI with `codex queue`.** Verified by preflight before your command runs.
- **Python 3.** Standard library only. CI exercises the suite on Python 3.12.
- **Windows notes.** Commands are interpreted by PowerShell, never wrapped in an extra `cmd.exe` layer, and a `codex.ps1` shim is invoked through PowerShell rather than passed to `CreateProcess`.

Run one watcher per experiment. Parallel watchers are fine when the task genuinely calls for them.

wake-run is not a way around your sandbox. The background process inherits the launch environment and its permissions, so do not use it to bypass sandboxing, approvals, or command restrictions.

## Repository layout

| Path | What it holds |
|---|---|
| [`skills/wake-run/SKILL.md`](./skills/wake-run/SKILL.md) | The skill instructions: launch workflow, runtime contract, and wake-up message shape. |
| [`skills/wake-run/scripts/wake_run.py`](./skills/wake-run/scripts/wake_run.py) | Launcher and detached watcher. |
| [`skills/wake-run/agents/openai.yaml`](./skills/wake-run/agents/openai.yaml) | Agent-facing interface metadata; implicit invocation is enabled. |
| [`.codex-plugin/plugin.json`](./.codex-plugin/plugin.json) | Codex plugin manifest. |
| [`tests/test_wake_run.py`](./tests/test_wake_run.py) | Unit and regression tests. |

Run logs are written to `.codex-wake-run/` in whichever project you launch from, and that directory is gitignored.

## Development

```bash
python -m unittest discover -s tests -v
```

The same command runs in CI on `ubuntu-latest` and `windows-latest` with Python 3.12, on every push and pull request. Windows-specific cases are skipped on POSIX hosts. The suite covers wake-up message content, PowerShell and `codex.ps1` invocation, thread targeting, the `codex queue` preflight, watcher success and failure paths, detached launch, and an assertion that the runtime contains no polling or sleep loop.

## Community and support

Questions and bug reports are welcome in [GitHub Issues](https://github.com/ZardLi1115/codex-wake-run/issues). Broader discussion happens on [Linux.do](https://linux.do/latest).

