<h1 align="center">wake-run-skill</h1>

<p align="center">一个 Codex skill：把长时间运行的命令交给分离的守护进程，进程退出时唤醒最初发起的 Codex 线程，模型全程无需轮询。</p>

<p align="center">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square" alt="Python 3.12"> <a href="https://linux.do/latest"><img src="https://img.shields.io/badge/Linux.do-Community-7C3AED?style=flat-square" alt="Linux.do 社区"></a>
</p>

在 agent 会话里跑长实验，通常只有两种难受的选择：要么让模型停在轮询循环里，一轮一轮地等；要么放弃这条线程，等结果出来之后重新把任务背景讲一遍。

wake-run 把这段等待去掉。你把已经定稿的命令交给它，它启动一个分离的守护进程，打印 `status: armed`，当前这一轮立刻结束。守护进程阻塞在操作系统的进程退出事件上。命令结束时，无论成功还是失败，守护进程都会用 `codex queue` 把一条唤醒消息注入发起它的那条线程，消息里带着退出码和日志路径。模型带着完整上下文接着做原来的任务。

## 核心特性

| 特性 | 为什么重要 |
|---|---|
| 事件驱动，而非轮询 | 守护进程阻塞在 `process.wait()` 上。运行时代码里没有任何 sleep 循环、定时器或状态检查，并且有一条回归测试专门守住这一点。 |
| 当前轮次立即结束 | 启动器派生出分离的 worker，打印一行 JSON 就退出，不占用任何模型轮次去等待。 |
| 唤醒的是同一条线程 | 守护进程调用 `codex queue --thread "$CODEX_THREAD_ID"`，续跑消息落回发起任务的那次对话，而不是新开一条。 |
| 失败同样会唤醒 | 非零退出和进程启动失败都会生成带退出码的唤醒消息，任务挂掉不会变成无声无息。 |
| Windows 与 POSIX 双支持 | 命令在 Windows 上走 PowerShell，在 POSIX 上走 `/bin/sh`，并正确处理 `codex.ps1` shim。测试在 `ubuntu-latest` 和 `windows-latest` 上都会运行。 |
| 每次运行独立日志 | 每次运行把 stdout 和 stderr 一起写入 `<cwd>/.codex-wake-run/<run_id>.log`，唤醒消息里点明的就是这个文件。 |

## 架构

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

启动器在动手之前先验证 `codex queue` 是否可用。这样，不兼容的 Codex CLI 会立刻报错，而不是先把任务跑完、最后发现没人可唤醒。

## 使用示例

一次完整的往返，实际会话里就是这样。

**你：**

```text
帮我用 wake-run-skill 运行这个脚本。
```

**Codex** 启动脚本，从启动器拿到 `armed`：

```json
{"status": "armed", "run_id": "b7599ab35869", "worker_pid": 97153, "log_file": "/work/project/.codex-wake-run/b7599ab35869.log"}
```

随后它停止推理，结束这一轮。没有任何东西在轮询任务，不占用轮次去等，这段时间会话是空闲的。

**一段时间以后**，脚本退出，守护进程往同一条线程里注入唤醒消息：

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

**Codex** 把这条消息当作系统续跑事件，而不是用户的新指令；需要细节时读消息里指明的日志，然后继续执行原来的任务：

```text
$ echo "training started"; sleep 2; echo "done"
training started
done
```

唤醒消息的结构是固定的，始终包含命令、状态、退出码和日志路径。失败时状态行为 `执行失败`，退出码是真实的非零值；如果进程根本没能启动，消息末尾会追加一行 `启动错误`。无论哪种情况线程都会被唤醒，所以 Codex 可以直接定位失败原因并重新启动，而不用你自己察觉到那份沉默。

## 快速开始

两步都是直接对 Codex 说的话。不需要编译，也没有第三方依赖，守护进程只用 Python 标准库。

**1. 安装 skill。** 让你的 Codex 去装：

```text
帮我安装 wake-run 这个 skill：https://github.com/ZardLi1115/codex-wake-run
```

**2. 用起来。** 装好之后，把长任务交给它：

```text
帮我调用 wake-run 这个 skill 执行 xxx 任务。
```

剩下的交给 Codex：它会把命令定稿、调用启动器，看到 `status: armed` 就结束这一轮。任务退出时，唤醒消息会把线程带回来，Codex 接着往下做。

几件值得知道的事：

- **只能在 Codex 会话内工作。** skill 需要 `CODEX_THREAD_ID` 才知道该唤醒哪条线程，这个变量由 Codex 注入到 shell 命令环境中。在会话之外，启动器会以 `CODEX_THREAD_ID is missing; run wake-run from a Codex shell command.` 退出。
- **你的 Codex CLI 需要支持 `codex queue`。** 启动器用 `codex queue --help` 预检，不支持就直接拒绝启动。你也可以用同样的命令自己确认。
- **它不是绕过沙箱的手段。** 后台进程继承启动时的环境及其权限，因此无法用它来规避沙箱、审批或命令限制。
- **每个实验一个守护进程。** 任务确实需要时，并行启动多个也是允许的。

如果想绕过 skill 直接调用启动器，请用绝对路径，并保持工作目录为你的项目：

```bash
python3 /path/to/codex-wake-run/skills/wake-run/scripts/wake_run.py \
  --command '<exact command>'
```

Windows 上通常用 `python` 而不是 `python3`。

## 启动器参数

`skills/wake-run/scripts/wake_run.py` 支持以下参数：

| 参数 | 默认值 | 作用 |
|---|---|---|
| `--command` | 必填 | 要执行的确切 shell 命令。启动前请先把参数定稿。 |
| `--cwd` | 当前目录 | 命令的工作目录。 |
| `--log-dir` | `<cwd>/.codex-wake-run` | 运行日志目录。 |
| `--codex-bin` | `codex` | 用于预检和发送唤醒消息的 Codex CLI 可执行文件。 |

分离的守护进程会以命令自身的退出码退出；如果进程未能启动，退出码为 `127`；如果命令跑完了但 `codex queue` 失败，退出码为 `70`。最后这种情况下失败原因会追加写入运行日志，因为此时已经没有线程可以汇报了。

## 仓库结构

| 路径 | 内容 |
|---|---|
| [`skills/wake-run/SKILL.md`](./skills/wake-run/SKILL.md) | Skill 指令：启动流程、运行时约定、唤醒消息结构。 |
| [`skills/wake-run/scripts/wake_run.py`](./skills/wake-run/scripts/wake_run.py) | 启动器与分离守护进程。 |
| [`skills/wake-run/agents/openai.yaml`](./skills/wake-run/agents/openai.yaml) | 面向 agent 的接口元数据，已开启隐式调用。 |
| [`.codex-plugin/plugin.json`](./.codex-plugin/plugin.json) | Codex 插件清单。 |
| [`tests/test_wake_run.py`](./tests/test_wake_run.py) | 单元测试与回归测试。 |

运行日志写入你启动任务所在项目的 `.codex-wake-run/` 目录，该目录已被 gitignore。

## 致谢

感谢 [Linux Do](https://linux.do/latest) 社区的支持。

