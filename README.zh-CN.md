<h1 align="center">wake-run-skill</h1>

<p align="center">一个 Codex Skill：把长时间运行的命令交给分离的守护进程，进程退出时唤醒最初发起的 Codex 线程，模型全程无需轮询。</p>

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
| 事件驱动，而非轮询 | 守护进程阻塞在 `process.wait()` 上。运行时代码里没有 sleep 循环、定时器或状态检查。 |
| 当前轮次立即结束 | 启动器派生出分离的 worker，打印一行 JSON 就退出，不占用模型轮次去等待。 |
| 唤醒同一条线程 | 守护进程调用 `codex queue --thread "$CODEX_THREAD_ID"`，续跑消息回到发起任务的那次对话。 |
| 失败同样会唤醒 | 非零退出和进程启动失败都会生成唤醒消息。 |
| Windows 与 POSIX 双支持 | Windows 走 PowerShell，POSIX 走 `/bin/sh`，并正确处理 `codex.ps1` shim。 |
| 每次运行独立日志 | stdout 和 stderr 写入 `<cwd>/.codex-wake-run/<run_id>.log`。 |

## 架构

```text
Codex thread
    │
    │ 启动 wake-run
    ▼
wake_run.py launcher
    │
    │ 启动 detached worker，当前 turn 结束
    ▼
实验进程
    │
    │ process.wait()
    ▼
退出码 + 日志
    │
    │ codex queue --thread <原 thread>
    ▼
Codex 被唤醒并继续原任务
```

启动器在执行实验之前先验证 `codex queue` 是否可用。这样，不兼容的 Codex CLI 会立刻报错，而不是任务跑完后才发现无法唤醒。

## 使用示例

**你：**

```text
帮我用 wake-run 运行这个实验，结束后继续完成任务。
```

**Codex** 启动任务并拿到 `armed`：

```json
{"status": "armed", "run_id": "b7599ab35869", "worker_pid": 97153, "log_file": "/work/project/.codex-wake-run/b7599ab35869.log"}
```

随后当前轮次结束，不会轮询后台任务。

脚本退出时，守护进程会向同一条线程写入：

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

Codex 根据需要读取日志，然后继续原任务。

## 快速开始

这个仓库本身就是一个独立 Skill，不需要 `.codex-plugin`，也没有额外的 `skills/wake-run` 套娃目录。

让 Codex 安装：

```text
帮我安装 wake-run 这个 skill：https://github.com/ZardLi1115/codex-wake-run
```

安装后直接使用：

```text
帮我调用 wake-run 这个 skill 执行 xxx 任务。
```

几件值得知道的事：

- **只能在 Codex 会话内工作。** Skill 依赖 `CODEX_THREAD_ID` 判断应该唤醒哪条线程。
- **Codex CLI 需要支持 `codex queue`。** 启动器会在实验启动前预检。
- **它不是绕过沙箱的手段。** 后台进程继承启动环境及其权限。
- **每个实验一个 watcher。** 任务确实需要时可以并行运行多个。

## 仓库结构

| 路径 | 内容 |
|---|---|
| [`SKILL.md`](./SKILL.md) | Skill 指令：启动流程、运行时约定、唤醒消息结构。 |
| [`scripts/wake_run.py`](./scripts/wake_run.py) | 启动器与分离守护进程。 |
| [`agents/openai.yaml`](./agents/openai.yaml) | 面向 agent 的 Skill 元数据，已开启隐式调用。 |
| [`tests/test_wake_run.py`](./tests/test_wake_run.py) | 单元测试、集成测试、Windows 回归测试。 |

运行日志写入启动任务所在项目的 `.codex-wake-run/` 目录，该目录已被 gitignore。

## 致谢

感谢 [Linux Do](https://linux.do/latest) 社区的支持。
