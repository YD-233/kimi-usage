#!/usr/bin/env python3
"""Feed a synthetic session (with tower_mode records) to statusline.py."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "statusline.py")

ANSI_RE = None


def make_home(tmp, main_wire_lines, sub_wire_lines=None):
    home = os.path.join(tmp, "kimihome")
    sess = os.path.join(home, "sessions", "wd_x", "session_sess-test",
                        "agents")
    os.makedirs(os.path.join(sess, "main"))
    with open(os.path.join(sess, "main", "wire.jsonl"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(main_wire_lines) + "\n")
    if sub_wire_lines:
        os.makedirs(os.path.join(sess, "agent-1"))
        with open(os.path.join(sess, "agent-1", "wire.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(sub_wire_lines) + "\n")
    with open(os.path.join(sess, "state.json"), "w", encoding="utf-8") as f:
        json.dump({"workDir": tmp}, f)
    return home


def run(home, tmp, script=None):
    payload = json.dumps({"sessionId": "sess-test", "cwd": tmp,
                          "model": "kimi-code/k3-256k",
                          "permissionMode": "auto", "planMode": False,
                          "gitBranch": None})
    env = dict(os.environ, KIMI_CODE_HOME=home, KIMI_USAGE_NO_COLOR="")
    r = subprocess.run([sys.executable, script or SCRIPT], input=payload,
                       capture_output=True, text=True, env=env, timeout=30)
    return r


def visible(line):
    out, i = [], 0
    while i < len(line):
        if line[i] == "\x1b":
            j = line.find("m", i)
            i = (j + 1) if j >= 0 else i + 1
            continue
        out.append(line[i])
        i += 1
    return "".join(out)


USAGE_MAIN = json.dumps({"type": "usage.record", "model": "kimi-code/k3-256k",
                         "usage": {"inputOther": 100, "output": 50,
                                   "inputCacheRead": 900,
                                   "inputCacheCreation": 30}})
USAGE_SUB = json.dumps({"type": "usage.record", "model": "other/model-x",
                        "usage": {"inputOther": 10, "output": 5,
                                  "inputCacheRead": 20,
                                  "inputCacheCreation": 2}})
# a quoted record inside tool context must not flip the badge (the type
# match on _WANTED_RECORDS guards this; assert it here)
QUOTED = json.dumps({"type": "context.append_loop_event",
                     "text": 'json {"type":"tower_mode.exit","agentId":"main"}'})

CASES = [
    ("tower on", True,
     [USAGE_MAIN,
      '{"type":"swarm_mode.enter","agentId":"main"}',
      '{"type":"tower_mode.enter","agentId":"main","sessionId":"other"}',
      QUOTED,
      '{"type":"llm.request","modelAlias":"k3","model":"kimi-code/k3-256k",'
      '"thinkingEffort":"high"}'],
     None),
    ("tower on alone", True,
     [USAGE_MAIN,
      '{"type":"tower_mode.enter","agentId":"main","sessionId":"x"}'],
     [USAGE_SUB]),
    ("tower exit after enter", False,
     [USAGE_MAIN,
      '{"type":"tower_mode.enter","agentId":"main"}',
      '{"type":"tower_mode.exit","agentId":"main"}'],
     None),
    ("sub-agent tower event ignored", False,
     [USAGE_MAIN,
      '{"type":"llm.request","modelAlias":"k3","model":"kimi-code/k3-256k",'
      '"thinkingEffort":"high"}'],
     ['{"type":"tower_mode.enter","agentId":"agent-1"}', USAGE_SUB]),
    ("no tower records", False,
     [USAGE_MAIN,
      '{"type":"llm.request","modelAlias":"k3","model":"kimi-code/k3-256k",'
      '"thinkingEffort":"high"}'],
     None),
]

WIRE_WITH_TOWER = [
    USAGE_MAIN,
    '{"type":"tower_mode.enter","agentId":"main","sessionId":"x"}',
    '{"type":"llm.request","modelAlias":"k3","model":"kimi-code/k3-256k",'
    '"thinkingEffort":"high"}',
]

TUI_TOML = ("# header comment\n"
            "# >>> kimi-usage\n"
            "[status_line]\n"
            "command = 'python statusline.py'\n"
            "# <<< kimi-usage\n")


def make_managed_install(tmp, record):
    """A managed copy under plugins/managed plus installed.json: the only
    layout in which plugin_disabled() polices itself."""
    home = make_home(tmp, WIRE_WITH_TOWER)
    managed = os.path.join(home, "plugins", "managed", "kimi-usage",
                           "scripts")
    os.makedirs(managed)
    for name in ("statusline.py", "setup_statusline.py"):
        shutil.copy(os.path.join(HERE, name), os.path.join(managed, name))
    with open(os.path.join(home, "plugins", "installed.json"), "w",
              encoding="utf-8") as f:
        json.dump({"plugins": [record] if record else []}, f)
    with open(os.path.join(home, "tui.toml"), "w", encoding="utf-8") as f:
        f.write(TUI_TOML)
    return home, os.path.join(managed, "statusline.py")


def check(replica_case, record, failures):
    tmp = tempfile.mkdtemp(prefix="kimiusage-rep-")
    try:
        home, script = make_managed_install(tmp, record)
        r = run(home, tmp, script=script)
        vis = visible(r.stdout.strip())
        tokens = vis.split("|")[0].split()
        problems = []
        if r.returncode != 0:
            problems.append(f"exit={r.returncode}")
        if "总计" in vis:
            problems.append("usage section still rendered")
        if "k3-256k" not in tokens:
            problems.append("model slot missing")
        if "tower" not in tokens:
            problems.append("tower badge lost (replica not faithful)")
        toml = open(os.path.join(home, "tui.toml"), encoding="utf-8").read()
        if "kimi-usage" in toml:
            problems.append("tui.toml block not scrubbed")
        status = "FAIL (" + ", ".join(problems) + ")" if problems else "ok"
        if problems:
            failures += 1
        print(f"[{status}] {replica_case}: {vis!r}")
        return failures
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_enabled(failures):
    """enabled:true through the managed copy must keep the normal render
    (usage present, tower badge, tui.toml untouched)."""
    tmp = tempfile.mkdtemp(prefix="kimiusage-en-")
    try:
        home, script = make_managed_install(
            tmp, {"id": "kimi-usage", "enabled": True})
        r = run(home, tmp, script=script)
        vis = visible(r.stdout.strip())
        tokens = vis.split("|")[0].split()
        problems = []
        if "总计" not in vis:
            problems.append("usage section missing")
        if "tower" not in tokens:
            problems.append("tower badge missing")
        if "kimi-usage" not in open(os.path.join(home, "tui.toml"),
                                    encoding="utf-8").read():
            problems.append("tui.toml block wrongly scrubbed")
        status = "FAIL (" + ", ".join(problems) + ")" if problems else "ok"
        if problems:
            failures += 1
        print(f"[{status}] enabled: {vis!r}")
        return failures
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_top1(failures):
    """Usage section shows the total plus only the heaviest sub-agent
    model — and stays correct after the ranking flips, proving the
    unshown model's usage survived in the cache."""
    tmp = tempfile.mkdtemp(prefix="kimiusage-top1-")
    try:
        home = make_home(tmp, WIRE_WITH_TOWER)
        agents = os.path.join(home, "sessions", "wd_x", "session_sess-test",
                              "agents")
        for agent, model, usage in (
                ("agent-a", "repo/model-a",
                 {"inputOther": 500, "output": 5, "inputCacheRead": 400,
                  "inputCacheCreation": 100}),          # input 1000
                ("agent-b", "repo/model-b",
                 {"inputOther": 150, "output": 3, "inputCacheRead": 40,
                  "inputCacheCreation": 10})):          # input 200
            os.makedirs(os.path.join(agents, agent))
            with open(os.path.join(agents, agent, "wire.jsonl"), "w",
                      encoding="utf-8") as f:
                f.write(json.dumps({"type": "usage.record", "model": model,
                                    "usage": usage}) + "\n")

        vis = visible(run(home, tmp).stdout.strip())
        problems = []
        if "model-a" not in vis.split():
            problems.append("top model-a missing")
        if "model-b" in vis.split():
            problems.append("smaller model-b still shown")
        if "↑ 2.2k" not in vis:       # 1030 main + 1000 a + 200 b
            problems.append("total wrong: no '↑ 2.2k'")
        if problems:
            failures += 1
        print(f"[{'FAIL (' + ', '.join(problems) + ')' if problems else 'ok'}] "
              f"top-1 only: {vis!r}")

        # model-b overtakes: its column appears with the full accumulated
        # usage, and the total keeps model-a's contribution (cache kept
        # every model even though only one was ever rendered)
        with open(os.path.join(agents, "agent-b", "wire.jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps({
                "type": "usage.record", "model": "repo/model-b",
                "usage": {"inputOther": 4900, "output": 1,
                          "inputCacheRead": 100,
                          "inputCacheCreation": 0}}) + "\n")
        vis = visible(run(home, tmp).stdout.strip())
        problems = []
        if "model-b" not in vis.split():
            problems.append("new top model-b missing")
        if "model-a" in vis.split():
            problems.append("overtaken model-a still shown")
        if "↑ 7.2k" not in vis:       # 1030 + 1000 a (kept) + 5200 b
            problems.append("total lost hidden model: no '↑ 7.2k'")
        if "model-b ↑ 5.2k" not in vis:
            problems.append("model-b column not accumulated: no 5.2k")
        if problems:
            failures += 1
        print(f"[{'FAIL (' + ', '.join(problems) + ')' if problems else 'ok'}] "
              f"ranking flip: {vis!r}")
        return failures
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    failures = 0
    for name, expect_on, main_lines, sub_lines in CASES:
        tmp = tempfile.mkdtemp(prefix="kimiusage-t-")
        try:
            home = make_home(tmp, main_lines, sub_lines)
            r = run(home, tmp)
            out = r.stdout.strip()
            vis = visible(out)
            # exact token match: the cwd slot can legitimately contain the
            # substring "tower" inside a path name
            has = "tower" in vis.split("|")[0].split()
            status = "ok" if has == expect_on else "FAIL"
            if has != expect_on:
                failures += 1
            colored = ("\x1b[38;2;91;192;190;1mtower\x1b[22m\x1b[39m" in out
                       or "\x1b[38;2;0;131;143;1mtower\x1b[22m\x1b[39m" in out)
            if expect_on and not colored:
                status += " (missing accent paint)"
                failures += 1
            print(f"[{status}] {name}: {vis!r}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # a disabled/removed managed install must stand down gracefully:
    # built-in replica (usage gone, slots and mode badges intact, tower
    # badge still faithful) and the tui.toml block scrubbed
    failures = check("disabled", {"id": "kimi-usage", "enabled": False},
                     failures)
    failures = check("removed", None, failures)
    failures = check_enabled(failures)
    failures = check_top1(failures)

    print("FAIL" if failures else "all cases passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
