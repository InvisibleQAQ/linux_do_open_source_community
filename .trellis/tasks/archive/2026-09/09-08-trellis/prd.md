# 让 .trellis 进入版本控制

## Goal

`.trellis/` 目前 0 个文件被跟踪。管着代码的编码规范、PRD 与架构决策记录全都只存在于
本地磁盘上。让它进入版本控制，且**不修改用户机器级的全局配置**。

## What I Already Know（已实测，非推测）

### 为什么现在没被跟踪

- 用户**全局** gitignore `C:/Users/18368/.gitignore_global:8` 有一条 `.trellis/`。
- 项目 `.gitignore` 只排除 `.trellis/.runtime/` 和 `.trellis/scripts/**/__pycache__/`，
  从未打算排除整个 `.trellis/`。
- `git ls-files .trellis | wc -l` == 0。

后果已经发生：`09-08-llm` 那轮改的 3 份 `.trellis/spec/backend/*.md`、任务 PRD、
以及父 PRD 的 9 处修订，全部没进 commit `23b6b19`。Trellis 自己的 `task.py archive`
和 `add_session.py` 也因为检测到被忽略而**跳过了 git**，所以没有 archive / journal commit。

### 内层 `.trellis/.gitignore` 已经写对了

它排除的正是不该跟踪的东西，无需我们再补：

| 条目 | 为什么不该跟踪 |
|---|---|
| `.developer` | 本地开发者身份（`name=` + `initialized_at=`） |
| `.current-task` | per-dev 的当前任务指针，每人不同 |
| `.runtime/` | session / window 作用域的运行时状态 |
| `.ralph-state.json`、`.agents/`、`.agent-log`、`.session-id`、`.plan-log` | agent 运行时 |
| `*.tmp`、`.backup-*`、`*.new` | 原子更新与冲突解决的临时产物 |
| `**/__pycache__/`、`**/*.pyc` | Python 缓存 |

所以脚本里那句 "Do NOT use `git add -f .trellis/`" 是在警告 **`-f` 会绕过这份内层
`.gitignore`**。全局忽略一解除，普通 `git add .trellis/` 会正确遵守它。

### 项目级否定规则足够，实测有效

gitignore 优先级：`core.excludesFile`（全局）< `.git/info/exclude` < 仓库内 `.gitignore`
（越深优先级越高）。全局那条来自**更低优先级**的文件，所以项目 `.gitignore` 里的
`!.trellis/` 能压过它。

实测（加上 `!.trellis/` 后）：

```
git check-ignore -v .trellis/spec/backend/index.md .trellis/workflow.md
  -> 无输出，不再被忽略

git check-ignore -v .trellis/.developer .trellis/.runtime
  -> .trellis/.gitignore:2:.developer   .trellis/.developer
  -> .trellis/.gitignore:8:.runtime/    .trellis/.runtime
     仍被忽略，且是被【内层】规则忽略 —— 正确
```

### 会纳入什么

74 个文件、617K：

| 目录 | 文件数 | 是什么 |
|---|---|---|
| `scripts/` | 28 | Trellis 自带运行时（vendored 工具） |
| `tasks/` | 20 | PRD、jsonl、archive —— 项目的决策历史 |
| `spec/` | 18 | 分层编码规范 —— **管着代码，最该跟代码同版本** |
| `workspace/` | 3 | 项目会话日志（个人身份文件已被内层规则排除） |
| 顶层 5 个 | 5 | `.gitignore`、`.version`、`.template-hashes.json`、`config.yaml`、`workflow.md` |

## Requirements

### R1 用项目级否定规则解除忽略，不动全局配置

在项目 `.gitignore` 的 Trellis 段加 `!.trellis/`，保留现有的 `.trellis/.runtime/` 与
`.trellis/scripts/**/__pycache__/`（与内层规则重复无害，是双保险）。

**不修改 `C:/Users/18368/.gitignore_global`。** 那是机器级配置，会影响用户其他所有项目；
而且队友克隆本仓库时不会有它，靠它反而让行为依赖各人的机器状态。repo 内解决才是可复现的。

### R2 不使用 `git add -f`

`-f` 会绕过 `.trellis/.gitignore`，把 `.developer`、`.runtime/`、`__pycache__`、
备份目录一并拖进来。普通 `git add .trellis/` 即可。

### R3 记录这个行为变化

`.trellis/config.yaml` 的 `session_auto_commit` 默认为 true。之前脚本因为检测到
`.trellis/` 被忽略而跳过 git；跟踪之后，`add_session.py` 与 `task.py archive`
会开始**自己产生 commit**（`chore: record journal` / `chore(task): archive ...`）。
这是 Trellis 的既定设计，但属于可观察的行为变化，写进 `CLAUDE.md`。

### R4 补上上一轮丢失的内容

commit `23b6b19` 漏掉的 `.trellis/spec/backend/*.md`（3 份）、`09-08-llm` 的 PRD、
父 PRD 的 9 处修订，随本次提交一并进仓库。

## Non-Goals

- 不修改用户全局 gitignore。
- 不调整 `.trellis/.gitignore` 的任何条目——它已经正确。
- 不改 `session_auto_commit` 的默认值。
- 不把 `.trellis/` 加进 `.gitattributes` 做 LF 规范化（另一件事，且会改动 74 个文件的换行）。

## Acceptance Criteria

1. `git check-ignore .trellis/spec/backend/index.md` 无输出（不再被忽略）。
2. `git check-ignore -v .trellis/.developer` 仍有输出，且来源是 `.trellis/.gitignore`，
   不是项目 `.gitignore`。
3. `git ls-files .trellis` 覆盖 `spec/`、`tasks/`、`scripts/`、`workspace/` 四个目录，
   以及 `workflow.md`、`config.yaml`、`.version`、`.template-hashes.json`、`.gitignore`
   五个顶层文件。（原先写的 "== 74" 是动手前的快照，而本任务自身又新建了两个任务目录——
   硬编码计数天生会过期，改为按覆盖面判定。）
4. `git ls-files .trellis` 结果中不含 `.developer`、`.runtime/`、`__pycache__`、
   `*.pyc`、`.backup-*`、`.current-task`。
5. 全局 gitignore `C:/Users/18368/.gitignore_global` 未被修改。
6. `.trellis/spec/backend/{environment-configuration,directory-structure,error-handling,logging-guidelines}.md`
   的 `09-08-llm` 改动、`09-08-llm/prd.md`、父 PRD 的修订都在仓库里。
7. 根 `CLAUDE.md` 记录 `.trellis/` 已跟踪，以及随之生效的脚本自动提交行为。

## Open Questions

无。唯一的方案分支（项目级否定 vs 改全局配置）已由实测关闭：项目级可行且严格更优。
