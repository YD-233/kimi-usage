#!/usr/bin/env python3
"""kimi-usage: status line auto-setup, run by the plugin's SessionStart hook.

The kimi-code plugin manifest cannot declare a [status_line] command, so
this hook merges one into <KIMI_CODE_HOME>/tui.toml:

- idempotent: our managed block (delimited by marker comments) is created
  once and refreshed in place on later runs (e.g. after the plugin root
  moves on reinstall)
- conservative: an existing user-defined [status_line] section is left
  untouched, unless its command already points at a kimi-usage script
  (then it is adopted and refreshed)
- silent: prints nothing — hook stdout may be appended to model context
- fail-open: any error is swallowed; set KIMI_USAGE_DEBUG=1 to log to
  <KIMI_CODE_HOME>/kimi-usage-debug.log

The interpreter path is taken from sys.executable, so the status line
always runs with the same Python that successfully ran this hook.
"""

import os
import re
import sys
import time

MARK_BEGIN = "# >>> kimi-usage"
MARK_END = "# <<< kimi-usage"


def kimi_home():
    return os.environ.get(
        "KIMI_CODE_HOME", os.path.expanduser("~/.kimi-code")
    )


def plugin_root():
    return os.environ.get(
        "KIMI_PLUGIN_ROOT",
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )


def _debug(msg):
    if not os.environ.get("KIMI_USAGE_DEBUG"):
        return
    try:
        path = os.path.join(kimi_home(), "kimi-usage-debug.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} setup: {msg}\n")
    except Exception:
        pass


def _win_short_path(path):
    """8.3 short path (no spaces) so the command needs no quoting."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024):
            return buf.value
    except Exception:
        pass
    return path


def statusline_command():
    """Build the [status_line].command value.

    The TUI spawns it via `cmd.exe /d /s /c <command>` on Windows, and the
    Node/libuv argument quoting rewrites every inner `"` to `\\"`, which
    cmd cannot parse — any quoted path fails to launch. So on Windows both
    paths are converted to 8.3 short names and used unquoted. POSIX goes
    through `sh -c`, where quoting works normally.
    """
    exe = sys.executable
    script = os.path.join(plugin_root(), "scripts", "statusline.py")
    if os.name == "nt":
        exe = _win_short_path(exe)
        script = _win_short_path(script)
        if " " in exe or " " in script:
            _debug("no 8.3 short name for a space-containing path; "
                   "the status line command may fail to launch")
        return f"{exe} {script}"
    return f'"{exe}" "{script}"'


def toml_quote(value):
    """Quote as a TOML literal string; fall back to a basic string when the
    value contains a single quote."""
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_block():
    return (
        f"{MARK_BEGIN} auto-configured; delete these two marker lines and"
        f" everything between them to remove\n"
        f"[status_line]\n"
        f"command = {toml_quote(statusline_command())}\n"
        f"{MARK_END}\n"
    )


def find_active_section(lines, name):
    """Return [start, end) of the first uncommented [<name>] section, or None."""
    header = f"[{name}]"
    start = None
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s == header:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j].strip()
        if s.startswith("[") and not s.startswith("#"):
            end = j
            break
    return start, end


def merge(text):
    """Return the new tui.toml content, or None when nothing should change."""
    block = build_block()

    if MARK_BEGIN in text and MARK_END in text:
        pattern = re.compile(
            re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n?",
            re.S,
        )
        if pattern.search(text).group(0) == block:
            return None  # already up to date
        return pattern.sub(lambda _: block, text)

    lines = text.splitlines()
    sec = find_active_section(lines, "status_line")
    if sec is not None:
        start, end = sec
        m = re.search(r"(?m)^\s*command\s*=\s*(.+)$",
                      "\n".join(lines[start:end]))
        # Recognize our own command even when 8.3 short names mangled
        # "kimi-usage" into "KIMI-U~N" (the ~N numbering shifts as sibling
        # plugin dirs come and go, so a stale short name must still match).
        cmd = m.group(1).lower() if m else ""
        if "kimi-usage" in cmd or "kimi-u~" in cmd or "statusline.py" in cmd \
                or "status~" in cmd:
            # our own command from a manual/older setup: adopt and refresh
            new_lines = lines[:start] + block.splitlines() + lines[end:]
            return "\n".join(new_lines) + "\n"
        _debug("user-defined [status_line] found, leaving it untouched")
        return None

    sep = "" if not text or text.endswith("\n") else "\n"
    pad = "\n" if text.strip() else ""
    return text + sep + pad + block


def write_atomic(path, text):
    tmp = path + ".kimi-usage-tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def main():
    path = os.path.join(kimi_home(), "tui.toml")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""

    new_text = merge(text)
    if new_text is None:
        _debug("no change needed")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_atomic(path, new_text)
    _debug(f"status_line command written to {path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _debug(f"exception: {e!r}")
    sys.exit(0)
