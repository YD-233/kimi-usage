# kimi-usage

[Kimi Code CLI](https://www.kimi.com/code/docs/kimi-code-cli/) 插件：每轮对话结束时，把本轮 token 用量、缓存命中率、会话累计写进**终端标题栏**。

```
📊 本轮 ↑1.26M/↓8.4k · 缓存 99% · 累计 ↑19.15M/↓115.8k | 当前会话标题
```

- **零上下文污染**：统计信息完全不进入模型上下文
- **轮末即时**：Stop hook 在本轮结束时触发，时机精确
- **零依赖**：Python 3.7+ 标准库，单文件脚本

## 安装

```
/plugins install <本仓库路径或 URL>
/reload
```

之后每轮对话结束，终端标题自动更新。卸载：`/plugins remove kimi-usage`。

> 已知限制：TUI 在会话切换、会话改名、`/reload` 时会重置标题，下一轮结束后会重新写入。
> 平台支持：Linux 已验证（hook 子进程经 setsid 失去控制终端，脚本通过 `/proc`
> 向上查找到 TUI 进程的 `/dev/pts/N`）；macOS / Windows 未测试，欢迎 PR。

## 原理

- Kimi Code 的 Stop hook 在模型结束本轮时触发，引擎会**丢弃**它的 stdout——本插件反利用这一点：命令照常执行，却没有任何内容进入模型上下文。
- 脚本向 TUI 进程的控制终端写 OSC 0 转义序列设置标题；转义序列不产生可见字符、不移动光标，不影响 TUI 的差分渲染。
- 用量数据来自 `~/.kimi-code/sessions/<wd>/<session>/agents/*/wire.jsonl` 中的 `usage.record`（每次 LLM 调用一条），轮次边界由 `turn.prompt` 划分；子 agent 用量按时间窗口归属。

## 为什么是标题栏，而不是对话内显示

插件/Hook 体系目前没有任何"在 TUI 里显示自定义文本且不污染上下文"的官方通道
（statusLine 功能请求 [MoonshotAI/kimi-code#1171](https://github.com/MoonshotAI/kimi-code/issues/1171) 仍开放）。
以下每个方案都在 kimi-code 0.24.1 源码中验证过：

| 方案 | 问题 |
| --- | --- |
| statusLine / 底栏配置 | 不存在。`tui.toml` 只有 theme、editor、notifications 等 5 个配置项 |
| UserPromptSubmit hook 输出 | 唯一能渲染进 TUI 的 hook 输出，但①时机是**下轮开头**而非轮末；②文本作为 `hook_result` 消息**进入模型上下文**，每轮白烧 token |
| Stop hook 输出 | 放行时 stdout 被引擎**直接丢弃**；只有"阻断"结果被处理，而阻断会强制模型多跑一步（每轮多一次 LLM 调用） |
| 其余 14 个 hook 事件 | 全部 fire-and-forget，输出被丢弃 |
| 插件斜杠命令 | 命令正文只能作为 prompt 发给模型，不能直接执行脚本——报表必然进上下文且消耗 token |
| `kimi server` 注入消息 | server 是独立进程，与 TUI 不共享活跃会话状态；REST 只有只读 transcript 接口 |
| 手动追加 `wire.jsonl` | 运行中的 TUI 对该文件只写不读，追加内容要到 resume/replay 才可见 |
| 直接写 `/dev/tty` 文本行 | TUI（pi-tui）是行内差分渲染，外部写入使光标记账失步，界面必然错位 |
| 桌面通知 | 可行但转瞬即逝，不适合作为常驻用量面板 |

标题栏方案因此成为唯一同时满足**零上下文成本 + 轮末即时 + 不干扰 TUI 渲染**的通道：

- Stop hook 的输出反正被丢弃——脚本自己向终端写 OSC 0 转义序列，不产生任何上下文；
- Stop 事件恰好在模型结束本轮时触发，时机精确；
- 转义序列不输出可见字符、不移动光标，差分渲染的光标记账不受影响；
- hook 子进程经 `setsid` 失去控制终端（`/dev/tty` 不可用），脚本改为从 `/proc`
  沿父进程链找到 TUI 进程的控制终端 `/dev/pts/N`。

## License

MIT
