#!/usr/bin/env python3
"""kimi-usage: status line auto-setup, run by the plugin's SessionStart hook.

The kimi-code plugin manifest cannot declare a [status_line] command, so
this hook merges one into <KIMI_CODE_HOME>/tui.toml:

- idempotent: our managed block (delimited by marker comments) is created
  once and refreshed in place on later runs (e.g. after the plugin root
  moves on reinstall)
- conservative: a [status_line] section the user already owns keeps its
  own `command`; a section that only sets `items` (a complementary
  setting) gets our command line added inside it, and a command that is
  already ours is adopted and refreshed
- safe: the result is checked before it replaces the live file — nothing
  is written that would leave tui.toml unparseable, because the CLI then
  falls back to default TUI preferences across the board
- silent: prints nothing — hook stdout may be appended to model context
- fail-open: any error is swallowed; set KIMI_USAGE_DEBUG=1 to log to
  <KIMI_CODE_HOME>/kimi-usage-debug.log

`remove_block()` is the counterpart, called by the status line command
itself once the plugin is disabled or removed (hooks stop at that point,
so nothing else could clean up).

The interpreter path is taken from sys.executable, so the status line
always runs with the same Python that successfully ran this hook.
"""

import os
import re
import sys
import time

MARK_BEGIN = "# >>> kimi-usage"
MARK_END = "# <<< kimi-usage"

# A [status_line] header, tolerantly: TOML allows surrounding whitespace, a
# quoted key and a trailing comment. Matching only the bare spelling used to
# mean we appended a *second* [status_line] table, which makes the whole
# file invalid TOML — and an invalid tui.toml drops every TUI preference
# (theme, editor, notifications, auto-update) back to its default.
SECTION_RE = re.compile(r'^\s*\[\s*"?status_line"?\s*\]\s*(#.*)?$')
# Any other table header, used to find where a section ends. Requires the
# whole line to be a header so an array continuation line ("x", …] does not
# count as one.
ANY_SECTION_RE = re.compile(r'^\s*\[[^\]]*\]\s*(#.*)?$')
COMMAND_RE = re.compile(r'(?m)^\s*command\s*=\s*(.+)$')


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


def build_block(with_header=True):
    """Our managed block.

    Standalone it carries the [status_line] header; merged into a section the
    user already owns it is just the command line, because a second header
    would redefine the table and break the file.
    """
    body = f"command = {toml_quote(statusline_command())}\n"
    if with_header:
        body = "[status_line]\n" + body
    return (
        f"{MARK_BEGIN} auto-configured; delete these two marker lines and"
        f" everything between them to remove\n"
        f"{body}"
        f"{MARK_END}\n"
    )


def find_sections(lines):
    """Line indices of every [status_line] header."""
    return [i for i, line in enumerate(lines) if SECTION_RE.match(line)]


def section_end(lines, start):
    """Index of the line that ends the section opened at `start`."""
    for j in range(start + 1, len(lines)):
        if ANY_SECTION_RE.match(lines[j]):
            return j
    return len(lines)


def marker_span(text):
    """(start, end) of our managed block in `text`, or None."""
    if MARK_BEGIN not in text or MARK_END not in text:
        return None
    m = re.search(
        re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n?",
        text, re.S)
    if m is None:       # markers present but in the wrong order
        _debug("marker block malformed (END before BEGIN)")
        return None
    return m.start(), m.end()


def validated(candidate):
    """Return `candidate` only if it is a usable tui.toml, else None.

    Writing a file the CLI cannot parse is never a win: it would silently
    reset the user's theme, editor, notification and auto-update settings.
    """
    if len(find_sections(candidate.splitlines())) > 1:
        _debug("candidate has more than one [status_line] table, not writing")
        return None
    try:
        import tomllib          # 3.11+; older Pythons keep the check above
    except ImportError:
        return candidate
    try:
        tomllib.loads(candidate)
    except Exception as e:
        _debug(f"candidate is not valid TOML ({e}), not writing")
        return None
    return candidate


def is_our_command(value):
    """Whether a [status_line].command already points at this plugin.

    8.3 short names mangle "kimi-usage" into "KIMI-U~N", and the ~N
    numbering shifts as sibling plugin dirs come and go, so a stale short
    name must still be recognized as ours.
    """
    cmd = value.lower()
    return ("kimi-usage" in cmd or "kimi-u~" in cmd
            or "statusline.py" in cmd or "status~" in cmd)


def merge(text):
    """Return the new tui.toml content, or None when nothing should change."""
    lines = text.splitlines()

    span = marker_span(text)
    if span is not None:
        # refresh our own block in place, keeping its shape: a block that
        # sits inside a section the user owns must not repeat the header
        start, end = span
        current = text[start:end]
        block = build_block(
            any(SECTION_RE.match(l) for l in current.splitlines()))
        if current == block:
            return None
        return validated(text[:start] + block + text[end:])

    sections = find_sections(lines)
    if len(sections) > 1:
        _debug("multiple [status_line] tables, leaving the file alone")
        return None

    if sections:
        start = sections[0]
        end = section_end(lines, start)
        tail = end
        while tail > start + 1 and not lines[tail - 1].strip():
            tail -= 1       # keep the blank lines that follow the section
        m = COMMAND_RE.search("\n".join(lines[start:end]))
        if m is None:
            # The user's section has no command (e.g. only `items`, which is
            # a complementary setting, not an alternative). Add ours inside
            # it: giving up here used to leave the plugin permanently
            # unconfigured with nothing but a debug-log line to show why.
            new_lines = (lines[:tail] + build_block(False).splitlines()
                         + lines[tail:])
            return validated("\n".join(new_lines) + "\n")
        if is_our_command(m.group(1)):
            # our own command from a manual or older setup: adopt and refresh
            new_lines = lines[:start] + build_block(True).splitlines() \
                + lines[tail:]
            return validated("\n".join(new_lines) + "\n")
        _debug("user-defined [status_line].command found, leaving it untouched")
        return None

    sep = "" if not text or text.endswith("\n") else "\n"
    pad = "\n" if text.strip() else ""
    return validated(text + sep + pad + build_block(True))


def remove_block():
    """Delete our managed block from tui.toml (idempotent).

    Disabling or removing a plugin stops its hooks but leaves the managed
    copy and this command on disk, so nothing else is left to clean up after
    us — the status line command calls this when it notices the plugin is
    gone.
    """
    path = os.path.join(kimi_home(), "tui.toml")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return
    span = marker_span(text)
    if span is None:
        return
    new_text = validated(text[:span[0]] + text[span[1]:])
    if new_text is None or new_text == text:
        return
    write_atomic(path, new_text)
    _debug(f"managed block removed from {path}")


def write_atomic(path, text):
    tmp = f"{path}.kimi-usage-{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError:
        # a read-only tui.toml (or one replaced by a directory) must not
        # leave the temp file behind; the pid keeps concurrent hooks apart
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
