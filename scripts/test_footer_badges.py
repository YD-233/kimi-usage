#!/usr/bin/env python3
"""Footer slot fidelity: mode badge names and every built-in slot's color.

kimi-code 0.40.0 renamed the permission modes (Always Ask / Ask When
Needed / Never Ask) and the footer paints all its slots from the theme's
ColorPalette. Each case here spawns statusline.py against a synthetic
session and checks the exact SGR sequence or visible text, mirroring the
upstream footer.ts / permission-mode.ts / colors.ts behavior. Every case
gets its own temp home: the script's TTL caches (tasks/git/pr) key on
the session dir, and a reused home would serve a stale 0-count cache to
the next case.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "statusline.py")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# dark / light palette values for the tokens the footer uses
# (upstream src/tui/theme/colors.ts), as "r;g;b" SGR parameters
PALETTE = {
    "dark": {"primary": "79;168;255", "accent": "91;192;190",
             "textDim": "136;136;136", "textMuted": "107;107;107",
             "warning": "232;168;56"},
    "light": {"primary": "21;101;192", "accent": "0;131;143",
              "textDim": "69;69;69", "textMuted": "95;95;95",
              "warning": "146;102;10"},
}


def painted(token, theme, bold):
    code = "38;2;" + PALETTE[theme][token] + (";1" if bold else "")
    return f"\x1b[{code}m"


def visible(line):
    return ANSI_RE.sub("", line)


def run(mode, plan=False, version="0.40.1", toml=None, branch=None,
        goal=None, tasks=(), wire_extra=(), theme_files=None):
    """One statusline.py run against a fresh synthetic home; returns the
    stdout line. `theme_files` maps theme name -> custom theme dict,
    written under <home>/themes first."""
    tmp = tempfile.mkdtemp(prefix="kimiusage-mode-")
    try:
        home = os.path.join(tmp, "kimihome")
        sess = os.path.join(home, "sessions", "wd_x", "session_s1")
        os.makedirs(os.path.join(sess, "agents", "main"))
        rec = json.dumps({"type": "usage.record",
                          "model": "kimi-code/k3-256k",
                          "usage": {"inputOther": 100, "output": 50,
                                    "inputCacheRead": 900,
                                    "inputCacheCreation": 30}})
        lines = [rec] + list(wire_extra)
        with open(os.path.join(sess, "agents", "main", "wire.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        state = {"workDir": tmp}
        if goal is not None:
            state["custom"] = {"goal": goal}
        with open(os.path.join(sess, "state.json"), "w",
                  encoding="utf-8") as f:
            json.dump(state, f)
        if tasks:
            tdir = os.path.join(sess, "agents", "main", "tasks")
            os.makedirs(tdir)
            for name in tasks:
                with open(os.path.join(tdir, f"{name}.json"), "w",
                          encoding="utf-8") as f:
                    json.dump({"status": "running"}, f)
        for name, theme in (theme_files or {}).items():
            themes = os.path.join(home, "themes")
            os.makedirs(themes, exist_ok=True)
            pathlib.Path(themes, f"{name}.json").write_text(
                json.dumps(theme), encoding="utf-8")
        if toml is not None:
            with open(os.path.join(home, "tui.toml"), "w",
                      encoding="utf-8") as f:
                f.write(toml)
        payload = {"sessionId": "s1", "cwd": tmp,
                   "model": "kimi-code/k3-256k", "permissionMode": mode,
                   "planMode": plan, "gitBranch": branch,
                   "version": version}
        env = dict(os.environ, KIMI_CODE_HOME=home, KIMI_USAGE_NO_COLOR="")
        r = subprocess.run([sys.executable, SCRIPT],
                           input=json.dumps(payload), capture_output=True,
                           text=True, env=env, timeout=30)
        return r.stdout.strip()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


FAILURES = 0


def check(name, cond, detail=""):
    global FAILURES
    if not cond:
        FAILURES += 1
    print(f"[{'ok' if cond else 'FAIL'}] {name}"
          f"{'' if cond else f': {detail}'}")


def main():
    out = run("yolo")
    vis = visible(out)
    check("yolo -> Ask When Needed",
          "Ask When Needed" in vis.split("  ")[0], vis)
    check("yolo badge painted warning bold (dark)",
          painted("warning", "dark", True) + "Ask When Needed" in out, out)

    out = run("auto")
    vis = visible(out)
    check("auto -> Never Ask", "Never Ask" in vis.split("  ")[0], vis)
    check("auto badge painted warning bold (dark)",
          painted("warning", "dark", True) + "Never Ask" in out, out)

    vis = visible(run("manual"))
    head = vis.split("  ")[0].split()
    check("manual draws no badge (Always Ask is the default)",
          "Always" not in head and "Ask" not in head, vis)

    vis = visible(run("yolo", version="0.39.1"))
    check("pre-0.40 TUI keeps the raw name",
          "yolo" in vis.split("  ")[0] and "Ask When Needed" not in vis, vis)
    vis = visible(run("yolo", version=None))
    check("missing version treated as current naming",
          "Ask When Needed" in vis, vis)

    out = run("yolo", plan=True)
    vis = visible(out)
    check("mode slot single-spaced, slots double-spaced",
          "Ask When Needed plan  " in vis, vis)
    check("plan painted primary bold (dark)",
          painted("primary", "dark", True) + "plan" in out, out)

    out = run("manual", wire_extra=(
        '{"type":"swarm_mode.enter"}',
        '{"type":"tower_mode.enter","agentId":"main"}'))
    vis = visible(out)
    check("swarm and tower badges single-spaced", "swarm tower" in vis, vis)
    check("swarm/tower painted accent bold (dark)",
          painted("accent", "dark", True) + "swarm" in out
          and painted("accent", "dark", True) + "tower" in out, out)

    out = run("manual", branch="main")
    check("git badge painted textDim (dark)",
          painted("textDim", "dark", False) + "main" in out, out)

    out = run("manual", tasks=("bash-1", "agent-2"))
    vis = visible(out)
    check("task badges present", "[1 task running]" in vis
          and "[1 agent running]" in vis, vis)
    check("task badges painted primary (dark)",
          painted("primary", "dark", False) + "[1 task running]" in out, out)

    goal = {"goalId": "g1", "status": "blocked", "turnsUsed": 3,
            "wallClockMs": 240000, "budget": {}}
    out = run("manual", goal=goal)
    vis = visible(out)
    check("goal badge rendered",
          "[goal ● blocked · 4m · 3 turns]" in vis, vis)
    check("blocked goal dot painted warning (dark)",
          painted("warning", "dark", False) + "●" in out, out)
    check("goal label painted textMuted (dark)",
          painted("textMuted", "dark", False) + "[goal " in out, out)
    goal["status"] = "active"
    out = run("manual", goal=goal)
    check("active goal dot painted primary (dark)",
          painted("primary", "dark", False) + "●" in out, out)

    out = run("auto", toml='theme = "light"\n')
    check("light theme palette for badges",
          painted("warning", "light", True) + "Never Ask" in out, out)
    out = run("manual", branch="main", toml='theme = "light"\n')
    check("light theme palette for git slot",
          painted("textDim", "light", False) + "main" in out, out)

    warn = {"name": "warn", "colors": {"warning": "#FF00FF",
                                       "oops": "nothex",
                                       "primary": "#00FF00"}}
    out = run("auto", toml='theme = "warn"\n', theme_files={"warn": warn})
    check("custom theme override, invalid hex dropped",
          "\x1b[38;2;255;0;255;1mNever Ask" in out
          and painted("primary", "dark", True) + "plan" not in out, out)
    baselight = {"name": "baselight", "base": "light"}
    out = run("auto", toml='theme = "baselight"\n',
              theme_files={"baselight": baselight})
    check("custom theme base light",
          painted("warning", "light", True) + "Never Ask" in out, out)
    out = run("auto", toml='theme = "ghost"\n')
    check("missing custom theme falls back to dark",
          painted("warning", "dark", True) + "Never Ask" in out, out)

    print("FAIL" if FAILURES else "all cases passed")
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()
