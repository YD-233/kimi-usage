#!/usr/bin/env python3
"""kimi-usage: visual preview of the status-line color scheme.

Prints the cache-hit-rate gradient as it actually renders in the status
line (24-bit truecolor), plus a few full-line examples, so color tweaks
can be eyeballed and screenshotted without a live session.

Run from the repo root:
    python scripts/preview_colors.py
Colors follow the same rules as the status line: TERM=dumb or
KIMI_USAGE_NO_COLOR=1 renders plain text.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import statusline


def fmt_rate(r):
    """Same percent formatting as stats_line.seg."""
    if r >= 100:
        return "100%"
    if r >= 95:
        return f"{min(r, 99.9):.1f}%"
    return f"{round(r)}%"


def usage(read, other, out):
    return {"inputOther": other, "output": out,
            "inputCacheRead": read, "inputCacheCreation": 0}


def main():
    if not statusline._colors_on():
        print("(colors are off: TERM=dumb or KIMI_USAGE_NO_COLOR is set)",
              file=sys.stderr)

    print("缓存命中率渐变（0-90 每 5%，90-100 每 1%）：")
    rates = list(range(0, 91, 5)) + list(range(91, 101))
    for r in rates:
        code = statusline._cache_sgr(r)
        swatch = statusline._paint("████████", code)
        label = statusline._paint(f"缓存 {fmt_rate(r)}", code)
        print(f"  {r:>5}%  {swatch}  {label}")

    print()
    print("99-100% 细分（每 0.2%）：")
    for i in range(5):
        r = 99 + i * 0.2
        code = statusline._cache_sgr(r)
        swatch = statusline._paint("████████", code)
        label = statusline._paint(f"缓存 {fmt_rate(r)}", code)
        print(f"  {r:>5.1f}%  {swatch}  {label}")

    print()
    print("模式徽标（主题色板：warning / primary / accent，加粗）：")
    for label, token in (("Ask When Needed (yolo)", "warning"),
                         ("Never Ask (auto)", "warning"),
                         ("plan", "primary"),
                         ("swarm", "accent"),
                         ("tower", "accent")):
        print(f"  {statusline._paint(label, statusline._token_sgr(token, bold=True))}")

    print()
    print("完整状态行示例：")
    payload = {"permissionMode": "yolo", "planMode": False,
               "model": "kimi-code/k3-256k",
               "cwd": "D:/project/kimi-usage", "gitBranch": "main",
               "version": "0.40.1"}
    prefix = statusline.prefix_line(payload, statusline.model_display_names(),
                                    False, False, "max")
    examples = [
        statusline.stats_line(usage(968, 32, 100), {},
                              statusline.model_display_names()),
        statusline.stats_line(usage(993, 7, 508),
                              {"opencode-go/deepseek-v4-flash":
                               usage(991, 9, 457)},
                              statusline.model_display_names()),
        statusline.stats_line(usage(820, 180, 13), {},
                              statusline.model_display_names()),
        statusline.stats_line(usage(100, 0, 1), {},
                              statusline.model_display_names()),
    ]
    for line in examples:
        print(f"  {prefix} | {line}")


if __name__ == "__main__":
    main()
