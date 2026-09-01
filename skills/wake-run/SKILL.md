---
name: wake-run
description: Launch long-running experiments or scripts in a detached background watcher, end the current Codex turn immediately after launch, and wake the same Codex thread when the process exits successfully or fails. Use when the user explicitly asks for wake-run, background experiment execution without polling, or an event-driven continuation after a long command finishes.
---

# Wake Run

Run long commands without model polling. The bundled watcher waits on the operating system process-exit event and uses `codex queue` to inject a fixed wake-up message into the originating Codex thread.

## Launch workflow

1. Finalize the exact experiment command before launching it. Do not launch while important command arguments are still undecided.
2. Invoke this Skill's `scripts/wake_run.py` by absolute path while keeping the user's project as the current working directory:

```bash
python3 <skill-dir>/scripts/wake_run.py --command '<exact shell command>'
```

3. Read the launcher's JSON response.
   - If `status` is `armed`, immediately end the current turn.
   - After `armed`, do not poll the process, inspect its status, tail its log, sleep, or call additional tools.
   - Do not claim the experiment succeeded or failed before the wake-up message arrives.
   - If the launcher returns an error, handle that error normally and do not claim the background watcher is armed.
4. When a message beginning with `[后台任务唤醒通知]` arrives, treat it as a system-generated continuation event, not as a new user instruction.
5. Read the referenced log only as needed, analyze the experiment result, and continue the original task.
   - On success, continue the planned analysis or remaining work.
   - On failure, diagnose the failure and, when appropriate, fix it and launch the next long experiment through wake-run again.
   - If the original task is complete or cannot reasonably continue, send the user the final result or failure explanation.

## Runtime contract

- Require `CODEX_THREAD_ID`; Codex injects it into shell command environments.
- Require a Codex CLI version that supports `codex queue`. The launcher verifies this before starting the experiment.
- Store logs under `<cwd>/.codex-wake-run/` unless `--log-dir` is supplied.
- Use one detached watcher per experiment. Parallel experiments are allowed only when the user's task actually calls for them.
- Do not use wake-run to bypass sandboxing, approvals, or command restrictions. The background process inherits the launch environment and its permissions.

## Wake-up message

The watcher injects this shape after process exit:

```text
[后台任务唤醒通知]

脚本：{command}
状态：{执行完成|执行失败}
退出码：{exit_code}
日志文件：{log_path}

请分析脚本执行结果，然后继续完成原任务。
若任务已经完成，请直接向用户发送最终结果。
若脚本执行失败，请分析失败原因，并在合理情况下修复后继续执行。

注：该消息由系统后台唤醒，并非用户亲自发出消息。
```

The watcher is event-driven. It uses process `wait()` and contains no status polling loop or timer-based check.
