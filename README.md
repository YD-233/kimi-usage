# kimi-usage

**[Kimi Code CLI](https://www.kimi.com/code/docs/kimi-code-cli/) 插件：把每轮 token 用量显示在终端标题栏。**

每轮对话结束时，插件会把本轮 token 用量、缓存命中率和会话累计写入终端标题：

```
↑1.26M/↓8.4k · 缓存 99% · 总 ↑19.15M/↓115.8k | 我的会话标题
```

最重要的信息（本轮输入/输出 + 缓存命中率）排在最前，窄标签页也能完整显示；累计用量和会话标题在后，悬停标签页或看窗口标题栏可见完整内容。

- **零上下文消耗** —— 不会有任何内容被注入模型上下文
- **精确到轮末** —— 由 `Stop` hook 驱动，模型结束本轮的瞬间触发
- **零依赖** —— 单个 Python 3.7+ 标准库脚本

> 标题栏只是**过渡方案**。目前 kimi-code 没有任何能在不污染模型上下文的前提下显示自定义文本的渠道，标题栏是唯一可行解。等官方支持 statusLine（[MoonshotAI/kimi-code#1171](https://github.com/MoonshotAI/kimi-code/issues/1171)）或其他显示能力后，本插件会迁移到更合适的显示方式，详见[后续计划](#后续计划)。

## 安装

前置条件：系统已安装 Python 3.7+，且 `python` 或 `python3` 在 `PATH` 上。

在 Kimi Code CLI 的 TUI 中：

```
/plugins install https://github.com/YD-233/kimi-usage
/reload
```

之后每轮结束标题自动更新。

- 卸载：`/plugins remove kimi-usage`
- 指定版本：`/plugins install https://github.com/YD-233/kimi-usage/releases/tag/v1.0.3`

## 平台支持

| 平台 | 状态 |
| --- | --- |
| Linux | 已验证（GNOME Terminal；理论上任何支持 OSC 标题的终端都行） |
| Windows | 已验证（Warp、Windows Terminal；新版 conhost 同理） |
| macOS | 未测试——有 `/dev/tty` 兜底；欢迎反馈 |

已知限制：

- TUI 在切换会话、会话改名、`/reload` 时会重置标题；下一轮结束时会写回。
- 终端必须能解析 OSC 0 标题序列。Windows 上 mintty（Git Bash 默认终端）不走 conhost，无法显示标题——请使用 Windows Terminal、Warp 等现代终端。

### 排查问题

如果标题没有变化，手动跑一次 hook 并打开调试输出：

```sh
# Linux / macOS
echo '{"hook_event_name":"Stop","cwd":"'"$PWD"'"}' | KIMI_USAGE_DEBUG=1 python3 ~/.kimi-code/plugins/managed/kimi-usage/scripts/usage.py
```

```cmd
:: Windows（在你的项目目录下运行）
set KIMI_USAGE_DEBUG=1
echo {"hook_event_name":"Stop","cwd":"%CD%"} | python %USERPROFILE%\.kimi-code\plugins\managed\kimi-usage\scripts\usage.py
```

调试输出会显示写入了哪个终端目标（或为什么失败）。如果命令本身跑不起来，请安装 Python 3 或确认 `python`/`python3` 在 `PATH` 上。

## 工作原理

- Kimi Code 的 `Stop` hook 在模型结束一轮时触发，而 hook 引擎会**丢弃**它的 stdout。插件反其道而行之：命令照跑，但任何东西都不可能进入模型上下文。
- 脚本直接向终端写入 OSC 0 转义序列。转义序列不打印字符、不移动光标，因此不影响 TUI 的差分渲染。
- 用量数据来自 `~/.kimi-code/sessions/<工作目录>/<会话>/agents/*/wire.jsonl` 中的 `usage.record` 记录（每次 LLM 调用一条；轮边界由 `turn.prompt` 划分）。子 agent 的用量按时间戳归属到对应的轮。

## 为什么是标题栏，而不是对话界面

目前官方没有任何渠道能在不污染模型上下文的前提下向 TUI 显示自定义文本（statusLine 的需求 [MoonshotAI/kimi-code#1171](https://github.com/MoonshotAI/kimi-code/issues/1171) 仍挂着）。以下每个备选方案都对着 kimi-code 0.24.1 源码验证过：

| 方案 | 问题 |
| --- | --- |
| statusLine / 状态栏配置 | 不存在。`tui.toml` 只有 5 个键（主题、编辑器、通知等） |
| `UserPromptSubmit` hook 输出 | 唯一会在 TUI 渲染的 hook 输出，但 ① 显示时机是**下一轮开头**而非本轮结束；② 文本会作为 `hook_result` 消息追加进模型上下文——每轮都在烧 token |
| `Stop` hook 输出 | 正常允许时 stdout 被丢弃；只有 *block* 结果会被消费——而 block 会强制模型多跑一步（每轮多一次 LLM 调用） |
| 其余 hook 事件 | 输出要么被丢弃，要么只用于阻断流程——都不会在 TUI 渲染自定义文本 |
| 插件斜杠命令 | 命令体只能是发给模型的 prompt——不能直接执行脚本，所以报告必然进入上下文 |
| `kimi server` 消息注入 | server 是独立进程，不与 TUI 共享会话的实时状态；REST API 对 transcript 只读 |
| 追加写 `wire.jsonl` | 运行中的 TUI 只写这个文件；追加的记录要等 resume/回放才可见 |
| 往 `/dev/tty` 写文本行 | TUI（pi-tui）使用行内差分渲染；外部写入会让它的光标记账失步，把界面搞花 |
| 桌面通知 | 能用，但太短暂，不适合做常驻用量面板 |

标题栏是目前唯一同时满足**零上下文、轮末时机、不破坏 TUI** 三个条件的渠道：

- `Stop` hook 的输出反正被丢弃——脚本自己写 OSC 0 序列，产生零上下文。
- `Stop` 恰好在模型结束本轮时触发。
- 转义序列不输出可见字符、不移动光标，渲染器的记账不受影响。
- hook 子进程被 `setsid` 丢了控制终端（所以 `/dev/tty` 不可用）；脚本沿 `/proc` 的父进程链找到 TUI 进程真正的 `/dev/pts/N`，写入 OSC 0。Windows 上 hook 子进程则被放进一个无窗口的私有控制台；脚本沿父进程链（`NtQueryInformationProcess`，不用会偶发阻塞的进程快照 API）`AttachConsole` 附着到 kimi 主进程的真实控制台，再用 `SetConsoleTitleW` 直接设置控制台标题——不经过 OSC 解析器，中文等 Unicode 字符不会出问题，ConPTY 会把标题变化转发给终端。

## 后续计划

标题栏显示是现阶段的权宜之计，不是最终形态。kimi-code 还在快速迭代，一旦官方提供以下任一能力，插件会第一时间迁移显示方式：

- **statusLine / 状态栏**（[MoonshotAI/kimi-code#1171](https://github.com/MoonshotAI/kimi-code/issues/1171)）——最理想的形态，常驻 TUI 底部
- 其他不进入模型上下文的展示渠道，例如 hook 输出可选择不注入上下文、插件自定义 UI 面板等

迁移后标题栏写入会保留为可选的兜底方式（比如在 ssh、tmux 等场景下仍然有用）。

## License

MIT
