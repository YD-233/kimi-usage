# kimi-usage

**[Kimi Code CLI](https://github.com/MoonshotAI/kimi-code) 插件：把当前会话的 token 用量显示在 TUI 底部状态栏。**

![kimi-usage 实际效果：底部状态栏显示模型、目录、分支与会话用量](images/screenshot-tui.png)

需要 kimi-code ≥ 0.30.0（[0.30.0](https://github.com/MoonshotAI/kimi-code/releases/tag/%40moonshot-ai%2Fkimi-code%400.30.0) 起支持 `[status_line].command` 自定义底部状态栏）。

## 状态栏显示什么

一行，`|` 分隔左右：

- **左侧**：模式徽标（权限模式 `Ask When Needed`/`Never Ask`（0.40.0 的新文案，对应 `yolo`/`auto`；默认的 `Always Ask`/manual 与内置一致不显示徽标，旧版 TUI 则显示原始模式名）、`plan`/`swarm`/`tower`，仅激活时显示；`tower` 是 0.39.0 起的实验性多 agent 编排模式，需 `KIMI_CODE_EXPERIMENTAL_TOWER=1` 且 `/tower on`）、goal 徽标（如 `[goal ● active · 4m · 7 turns]`）、当前模型与思考强度（如 `K3-256k thinking: high`，跟随 `/effort` 切换；`/dance` 开启时变成彩虹，流动几秒后定格，`/dance off` 关闭）、后台任务徽标（如 `[2 tasks running]`）、工作目录、git 分支与改动统计（如 `main [+12 -3 ↑1]`，当前分支有打开的 PR 时追加可点击的 `[PR#42]`，需安装 `gh`）——外观与内置状态栏一致，各槽位颜色取自主题色板（`tui.toml` 的 `theme`，支持内置 dark/light 与自定义主题；`theme = "auto"` 时按 dark 处理，子进程探测不了终端背景色）
- **右侧**：整会话（主 agent + 全部子 agent）的总输入 `↑`、总输出 `↓`、缓存命中率；有子 agent 时再列一段消耗最多的子模型（按输入量取最多者）的同样三项

缓存命中率带健康渐变：低红高绿，95% 以上显示一位小数（如 `97.8%`），现代渠道常见的高缓存区间每一格都可分辨：

<img width="714" height="101" alt="image" src="https://github.com/user-attachments/assets/3bd19861-1ac9-4d9e-91a5-17dd96f4df9c" />

不同用量下的整行效果：

![状态行示例：含子 agent 分列、不同缓存率配色](images/statusline-examples.png)

## 特性

- **零上下文消耗** —— 不向模型上下文注入任何内容
- **实时更新** —— 状态栏每秒刷新，模型每次调用后立即可见
- **子 agent 只列消耗最多者** —— 总量之外再给消耗最大的一个子模型的明细，多模型混跑时最重要的开销一眼可见；其余模型的用量仍计入总计，排名变化时明细自动跟着换
- **跨平台零依赖** —— Linux / macOS / Windows，单个 Python 3.7+ 标准库脚本
- **自适应终端宽度** —— 行宽超出终端时按代价逐级退让：单位 `token` → `tok` → 省略，然后依次丢 git 分支、目录、`总计：`、`缓存` 标签、收紧 `·` 间距，再丢子 agent 明细列；窗口调宽后自动恢复完整显示

## 安装

前置条件：kimi-code ≥ 0.30.0，Python 3.7+（`python` 或 `python3` 在 `PATH` 上）。

```
/plugins install https://github.com/YD-233/kimi-usage
/reload
```

然后新开一个会话（`/new`）或重启 CLI，再执行 `/reload-tui`（或等下次启动），底部状态栏即开始显示。

- 已有自定义 `[status_line]` 段时：里面已有 `command` 的话插件**不会覆盖**（想换成本插件，把那行删掉，或指向 `plugins/managed/kimi-usage/scripts/statusline.py`）；只设了 `items` 的段落会被补上一行 `command`，两者并不冲突
- 任何情况下都不会把 `tui.toml` 写坏：写入前会校验一遍，只要结果不是合法 TOML 就放弃（`kimi doctor tui` 可以自己复核）
- 升级/指定版本：`/plugins install https://github.com/YD-233/kimi-usage/releases/tag/v1.5.5`

## 卸载与恢复

- 卸载：`/plugins remove kimi-usage`；禁用：`/plugins disable kimi-usage`
- 两者都会自动让位：状态栏脚本发现自己已被禁用或移除后，会删掉 `tui.toml` 里自己写入的那段，并当场把底栏渲染成内置样式——模式徽标、goal、模型、任务、目录、git 槽位照常，只是不再显示用量（唯一复刻不了的是轮换提示，上游本就不在自定义状态栏旁显示它）。当前会话立即恢复观感，无需 `/reload-tui`；在 `/reload-tui` 或重启之前 TUI 仍会每秒拉起一次脚本做这次渲染，重启后彻底停止
- 也可以手动删：`~/.kimi-code/tui.toml` 里 `# >>> kimi-usage` / `# <<< kimi-usage` 两条标记之间的块

## 调试

状态栏命令失败时 TUI 会保留上一次渲染的行（只有从没成功过才显示内置布局）。诊断日志写到 `~/.kimi-code/kimi-usage-debug.log`，两种开启方式：设 `KIMI_USAGE_DEBUG=1`（需重启 CLI），或 `touch ~/.kimi-code/kimi-usage-debug` 建一个开关文件（即时生效，删文件即关闭）。设 `KIMI_USAGE_NO_COLOR=1` 可输出纯文本。

也可以手动喂一个 JSON 快照，直接看脚本输出：

```bash
printf '{"sessionId":"YOUR_SESSION_ID","cwd":"/your/project"}' | \
  python3 ~/.kimi-code/plugins/managed/kimi-usage/scripts/statusline.py
```

## License

MIT
