# kimi-usage

**[Kimi Code CLI](https://github.com/MoonshotAI/kimi-code) 插件：把当前会话的 token 用量显示在 TUI 底部状态栏。**

kimi-code 0.30.0+ 支持 `[status_line].command` 自定义底部状态栏（[#0.30.0](https://github.com/MoonshotAI/kimi-code/releases/tag/%40moonshot-ai%2Fkimi-code%400.30.0)）。插件提供一个状态栏命令 `scripts/statusline.py`，每秒从会话的 `wire.jsonl` 中汇总：

- 当前会话的**总输入、总输出、缓存命中率**（主 agent + 全部子 agent）
- 存在子 agent 时，按**模型**分列的子 agent 总输入、总输出、缓存命中率

```
yolo plan  K3-256k  D:/project/kimi-usage  main | 总计：↑ 91.04M tok · ↓ 508.5k tok 缓存 99% | DeepSeek V4 Flash ↑ 87.60M tok · ↓ 457.1k tok 缓存 99% | K3-256k ↑ 117.5k tok · ↓ 1.0k tok 缓存 4%
```

`|` 左侧复刻内置栏第 1 行：模式徽标（`yolo`/`auto` 琥珀色加粗、`plan` 蓝色加粗、`swarm` 青色加粗，仅激活时显示）、当前模型（取 `display_name`，带 `thinking: max` 思考强度后缀）、缩短后的工作目录与 git 分支（暗色），槽位间两个空格，配色与官方样式一致（用终端命名色，深浅主题都正常；设 `KIMI_USAGE_NO_COLOR=1` 可关掉）。swarm 状态与思考强度不在 TUI 快照里，但 `swarm_mode.enter/exit` 和 `profile.bind`（带 thinkingEffort）都会持久化到会话 wire 日志，插件从日志取最后一条为准。（仍无法复刻的：goal/后台任务徽标、git 增删行数、右侧轮换小提示；内置栏第 2 行的 `context: N%` 不受影响。）没有子 agent 时只显示会话总计：

```
K3-256k  D:/project/kimi-usage  main | 总计：↑ 396.0k tok · ↓ 13.0k tok 缓存 82%
```

> 模型名优先取 `config.toml` 里 `[models."<别名>"]` 的 `display_name`，未配置时回退为别名最后一段。

## 特性

- **零上下文消耗** —— 不会向模型上下文注入任何内容
- **实时更新** —— 状态栏每秒刷新一次，模型每次调用后立即可见
- **按模型分列** —— 子 agent 用量按 `usage.record` 里的模型别名聚合，并显示 `config.toml` 中配置的 `display_name`，多模型混跑一目了然
- **跨平台** —— Linux / macOS / Windows 均可使用
- **零依赖** —— 单个 Python 3.7+ 标准库脚本

## 安装

前置条件：kimi-code ≥ 0.30.0，系统已安装 Python 3.7+，且 `python` 或 `python3` 在 `PATH` 上。

在 Kimi Code CLI 的 TUI 中：

```
/plugins install https://github.com/YD-233/kimi-usage
/reload
```

之后新开一个会话（`/new`）或重启 CLI，插件的 `SessionStart` hook 会自动把 `[status_line].command` 写入 `~/.kimi-code/tui.toml`（带 `# >>> kimi-usage` 标记块，指向当前解释器与插件目录的绝对路径，可重复执行、不会写重）。执行 `/reload-tui` 或再等一次启动，底部状态栏即开始显示用量。

说明：

- 如果你的 `tui.toml` 里已经有自己配置的 `[status_line]` 段，插件**不会覆盖**；想用本插件，把该段的 `command` 删掉或改成指向 `plugins/managed/kimi-usage/scripts/statusline.py` 即可。
- 想恢复内置状态栏：禁用插件（`/plugins` 里空格键），或删掉 `tui.toml` 里两条 `# >>> / # <<< kimi-usage` 标记之间的块。
- 卸载 `/plugins remove kimi-usage` 只删安装记录，managed 副本和 `tui.toml` 标记块会保留（状态栏仍可工作）；要彻底清掉就按上一条删标记块。

### 常用命令

- 卸载：`/plugins remove kimi-usage`
- 指定版本：`/plugins install https://github.com/YD-233/kimi-usage/releases/tag/v1.4.0`

## 平台支持

任何能运行 kimi-code ≥ 0.30.0 TUI 的平台均可使用（Linux / macOS / Windows 已验证）。状态栏命令失败时 TUI 会静默回退到内置布局，用下面的调试方法排查。

## 工作原理

- 插件 manifest 无法直接声明状态栏命令，因此用 `SessionStart` hook 运行 `scripts/setup_statusline.py`，把 `[status_line].command` 幂等写入 `~/.kimi-code/tui.toml`；hook 不产生任何 stdout，零上下文消耗。Windows 下 TUI 经 `cmd /d /s /c` 启动命令，内层引号会被 Node/libuv 转义成 cmd 无法识别的 `\"`，因此自动配置在 Windows 上会把解释器和脚本路径转成 8.3 短路径后**不加引号**写入（手动配置时请保持同样写法，或用 `dir /x` 查短路径）。
- kimi-code 每秒把当前会话快照（`sessionId`、`cwd`、模型、上下文用量等）以 JSON 形式通过 stdin 喂给 `[status_line].command`，脚本取 stdout 第一行渲染到底部状态栏。
- `scripts/statusline.py` 根据 `sessionId` 定位到 `~/.kimi-code/sessions/<工作目录>/<会话>/agents/*/wire.jsonl`，汇总其中每次 LLM 调用产生的 `usage.record` 记录（含模型别名），输出一行用量文本。
- 脚本带一个轻量缓存，按 `wire.jsonl` 修改时间失效，避免每秒重复解析大文件；解析时只对含 `usage.record` 的行做 JSON 解码，多 MB 的 wire 文件冷解析也在 300ms 的状态栏时限内。
- 用量口径：总输入 = `inputOther + inputCacheRead + inputCacheCreation`；缓存命中率 = `inputCacheRead / 总输入`。

## 调试

状态栏命令失败时，TUI 会静默回退到内置状态栏。排查方法：

```bash
# 手动喂一个 JSON 快照，看脚本输出什么
printf '{"sessionId":"YOUR_SESSION_ID","cwd":"/your/project"}' | \
  python3 ~/.kimi-code/plugins/managed/kimi-usage/scripts/statusline.py
```

也可用 `KIMI_USAGE_DEBUG=1` 让脚本把诊断信息追加到 `~/.kimi-code/kimi-usage-debug.log`。

## License

MIT
