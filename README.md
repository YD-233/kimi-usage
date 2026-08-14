# kimi-usage

**[Kimi Code CLI](https://github.com/MoonshotAI/kimi-code) 插件：把当前会话的 token 用量显示在 TUI 底部状态栏。**

有子 agent 时（按模型分列）：

```
yolo plan  K3-256k thinking: max  D:/project/kimi-usage  main | 总计：↑ 91.04M tok · ↓ 508.5k tok 缓存 99% | DeepSeek V4 Flash ↑ 87.60M tok · ↓ 457.1k tok 缓存 99% | K3-256k ↑ 117.5k tok · ↓ 1.0k tok 缓存 4%
```
<img width="3092" height="291" alt="image" src="https://github.com/user-attachments/assets/15e786b8-417d-4ac3-84f9-8b3741727241" />

无子 agent 时：

```
K3-256k thinking: max  D:/project/kimi-usage  main | 总计：↑ 396.0k tok · ↓ 13.0k tok 缓存 82%
```
<img width="3082" height="263" alt="image" src="https://github.com/user-attachments/assets/c59b11ad-5793-4bae-a05d-e3809a76f7fe" />

需要 kimi-code ≥ 0.30.0（[0.30.0](https://github.com/MoonshotAI/kimi-code/releases/tag/%40moonshot-ai%2Fkimi-code%400.30.0) 起支持 `[status_line].command` 自定义底部状态栏）。

## 显示说明

`|` 左侧**复刻内置状态栏第 1 行**，外观与官方一致：

- **模式徽标**（仅激活时显示）：`yolo`/`auto` 琥珀色加粗，`plan` 蓝色加粗，`swarm` 青色加粗
- **当前模型**：取 `config.toml` 里 `[models."<别名>"]` 的 `display_name`（未配置回退为别名最后一段），带 `thinking: max` 思考强度后缀
- **工作目录**（缩短为后三段）与 **git 分支**，暗色

`|` 右侧是**用量统计**：先是整会话（主 agent + 全部子 agent）的总输入 `↑`、总输出 `↓`、缓存命中率；之后每个有子 agent 用过的模型一段同样三项，按输入量降序。

实现细节：

- 模式/模型/目录/分支来自 TUI 每秒传入的 JSON 快照；快照没有的 **swarm 状态**和**思考强度**改从会话 `wire.jsonl` 里的 `swarm_mode.enter/exit` 与 `profile.bind`（含 `thinkingEffort`）记录取最后一条
- 配色用终端命名色，深浅主题都正常；设 `KIMI_USAGE_NO_COLOR=1` 输出纯文本
- 用量口径：总输入 = `inputOther + inputCacheRead + inputCacheCreation`；缓存命中率 = `inputCacheRead / 总输入`
- 无法复刻的内置项：goal / 后台任务徽标、git 增删行数、右侧轮换小提示（内置栏第 2 行的 `context: N%` 不受影响，照常显示）

## 特性

- **零上下文消耗** —— 不向模型上下文注入任何内容
- **实时更新** —— 状态栏每秒刷新，模型每次调用后立即可见
- **子 agent 按模型分列** —— 多模型混跑时各自花了多少一目了然
- **跨平台零依赖** —— Linux / macOS / Windows，单个 Python 3.7+ 标准库脚本

## 安装

前置条件：kimi-code ≥ 0.30.0，Python 3.7+（`python` 或 `python3` 在 `PATH` 上）。

```
/plugins install https://github.com/YD-233/kimi-usage
/reload
```

然后新开一个会话（`/new`）或重启 CLI：插件的 `SessionStart` hook 会把 `[status_line].command` 自动写入 `~/.kimi-code/tui.toml`（`# >>> kimi-usage` 标记块，幂等，可重复执行）。再执行 `/reload-tui`（或等下次启动），底部状态栏即开始显示。

说明：

- 已有自定义 `[status_line]` 段时插件**不会覆盖**；想换成本插件，把该段的 `command` 删掉或指向 `plugins/managed/kimi-usage/scripts/statusline.py` 即可
- 想恢复内置状态栏：禁用插件，或删掉 `tui.toml` 里两条 `# >>> / # <<< kimi-usage` 标记之间的块
- `/plugins remove kimi-usage` 只删安装记录，managed 副本和 `tui.toml` 标记块保留（状态栏仍可工作）；彻底清理请连同标记块一起删

### 常用命令

- 升级/指定版本：`/plugins install https://github.com/YD-233/kimi-usage/releases/tag/v1.4.1`
- 卸载：`/plugins remove kimi-usage`

## 平台支持

任何能运行 kimi-code ≥ 0.30.0 TUI 的平台均可使用（Linux / macOS / Windows 已验证）。

## 工作原理

- **自动配置**：插件 manifest 无法声明状态栏命令，故用 `SessionStart` hook 运行 `scripts/setup_statusline.py`，把 `[status_line].command` 幂等写入 `tui.toml`（hook 无 stdout，零上下文消耗）
- **Windows 的坑**：TUI 经 `cmd /d /s /c` 启动状态栏命令，内层引号会被 Node/libuv 转义成 cmd 无法识别的 `\"`，因此 Windows 上命令一律用 **8.3 短路径、不加引号**（手动配置同样如此，`dir /x` 可查短路径）
- **数据流**：TUI 每秒把会话快照（`sessionId`、`cwd`、模型、上下文用量等）以 JSON 通过 stdin 喂给命令，渲染其 stdout 第一行；脚本据此定位 `~/.kimi-code/sessions/<工作目录>/<会话>/agents/*/wire.jsonl`，汇总每次 LLM 调用的 `usage.record` 输出一行
- **性能**：行内容预过滤后才做 JSON 解码，另有按 `wire.jsonl` mtime 失效的轻量缓存——多 MB 的会话冷解析也稳在 TUI 的 300ms 时限内

## 调试

状态栏命令失败时 TUI 会静默回退到内置布局。排查：

```bash
# 手动喂一个 JSON 快照，看脚本输出
printf '{"sessionId":"YOUR_SESSION_ID","cwd":"/your/project"}' | \
  python3 ~/.kimi-code/plugins/managed/kimi-usage/scripts/statusline.py
```

诊断日志写到 `~/.kimi-code/kimi-usage-debug.log`，两种开启方式：设 `KIMI_USAGE_DEBUG=1`（需重启 CLI），或 `touch ~/.kimi-code/kimi-usage-debug` 建一个开关文件（即时生效，删文件即关闭）。

## License

MIT
