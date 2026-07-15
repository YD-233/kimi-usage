#!/usr/bin/env python3
"""kimi-usage: per-turn token usage in the terminal title bar.

A Kimi Code CLI plugin hook script, triggered on the Stop event (turn
end). The Stop hook's stdout is discarded by the hook engine, so this
script instead writes a one-line summary straight to the terminal title
via an OSC escape: exact turn-end timing, zero model-context cost.

Data source:
  <KIMI_CODE_HOME>/sessions/wd_*/session_*/agents/*/wire.jsonl
  (usage.record / turn.prompt entries)

Fail-open by design: any error is silently ignored.
"""

import glob
import json
import os
import select
import sys


def kimi_home():
    return os.environ.get(
        "KIMI_CODE_HOME", os.path.expanduser("~/.kimi-code")
    )


def fmt_tokens(n):
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.2f}M"


def input_total(u):
    return u["inputOther"] + u["inputCacheRead"] + u["inputCacheCreation"]


def empty_usage():
    return {"inputOther": 0, "output": 0,
            "inputCacheRead": 0, "inputCacheCreation": 0}


def add_usage(acc, u):
    for k in acc:
        acc[k] += int(u.get(k, 0) or 0)
    return acc


def cache_hit_rate(u):
    total = input_total(u)
    if total <= 0:
        return None
    return round(u["inputCacheRead"] / total * 100)


# --------------------------------------------------------------------------
# session resolution
# --------------------------------------------------------------------------

def read_hook_stdin():
    """Read the hook payload from stdin (non-blocking when stdin is a tty)."""
    try:
        if sys.stdin.isatty():
            return {}
        if os.name == "nt":
            # Windows select() only works on sockets and raises on pipes.
            # The hook engine always closes stdin after writing the
            # payload, so a plain read is safe here.
            raw = sys.stdin.read().strip()
        else:
            r, _, _ = select.select([sys.stdin], [], [], 0.5)
            if not r:
                return {}
            raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def find_session_dir(session_id, cwd):
    home = kimi_home()
    # 1. exact match via the session index
    try:
        with open(os.path.join(home, "session_index.jsonl"),
                  encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if session_id and rec.get("sessionId") == session_id:
                    if os.path.isdir(rec.get("sessionDir", "")):
                        return rec["sessionDir"]
    except OSError:
        pass
    # 2. direct directory name match
    if session_id:
        for pattern in (session_id, f"session_{session_id}"):
            for d in glob.glob(os.path.join(home, "sessions", "*", pattern)):
                if os.path.isdir(d):
                    return d
    # 3. fallback: newest session whose workDir == cwd
    def norm(p):
        return os.path.normcase(os.path.normpath(p)) if p else p

    cwd_norm = norm(cwd)
    best, best_mtime = None, -1.0
    for state in glob.glob(os.path.join(home, "sessions", "*", "*",
                                        "state.json")):
        try:
            with open(state, encoding="utf-8") as f:
                if cwd_norm and norm(json.load(f).get("workDir")) != cwd_norm:
                    continue
            mtime = os.path.getmtime(state)
        except (OSError, json.JSONDecodeError):
            continue
        if mtime > best_mtime:
            best, best_mtime = os.path.dirname(state), mtime
    return best


# --------------------------------------------------------------------------
# wire.jsonl parsing
# --------------------------------------------------------------------------

def iter_wire_records(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def parse_session(session_dir):
    """Return (turns, session_total).

    turns: per-turn usage buckets delimited by turn.prompt in the main
    wire; sub-agent records are attributed to the latest turn they fall
    in by timestamp.
    """
    turns = []
    session_total = empty_usage()

    main_wire = os.path.join(session_dir, "agents", "main", "wire.jsonl")
    if os.path.isfile(main_wire):
        for rec in iter_wire_records(main_wire):
            rtype = rec.get("type")
            if rtype == "turn.prompt":
                turns.append({"start": rec.get("time", 0) or 0,
                              "usage": empty_usage()})
            elif rtype == "usage.record":
                u = {k: int(rec.get("usage", {}).get(k, 0) or 0)
                     for k in session_total}
                add_usage(session_total, u)
                if turns:
                    add_usage(turns[-1]["usage"], u)

    for sub_wire in glob.glob(os.path.join(session_dir, "agents", "*",
                                           "wire.jsonl")):
        if os.path.basename(os.path.dirname(sub_wire)) == "main":
            continue
        for rec in iter_wire_records(sub_wire):
            if rec.get("type") != "usage.record":
                continue
            u = {k: int(rec.get("usage", {}).get(k, 0) or 0)
                 for k in session_total}
            add_usage(session_total, u)
            t = rec.get("time", 0) or 0
            for turn in reversed(turns):
                if t >= turn["start"]:
                    add_usage(turn["usage"], u)
                    break

    return turns, session_total


# --------------------------------------------------------------------------
# terminal title
# --------------------------------------------------------------------------

def stats_line(turn_usage, session_total):
    parts = []
    if turn_usage is not None:
        parts.append(
            f"本轮 ↑{fmt_tokens(input_total(turn_usage))}"
            f"/↓{fmt_tokens(turn_usage['output'])}")
        rate = cache_hit_rate(turn_usage)
        if rate is not None:
            parts.append(f"缓存 {rate}%")
    parts.append(f"累计 ↑{fmt_tokens(input_total(session_total))}"
                 f"/↓{fmt_tokens(session_total['output'])}")
    return " · ".join(parts)


def last_active_turn(turns):
    for turn in reversed(turns):
        u = turn["usage"]
        if input_total(u) or u["output"]:
            return u
    return None


def session_title(session_dir):
    try:
        with open(os.path.join(session_dir, "state.json"),
                  encoding="utf-8") as f:
            return str(json.load(f).get("title", "")).strip()[:24]
    except (OSError, json.JSONDecodeError):
        return ""


def find_ancestor_tty():
    """Find the controlling terminal of the nearest ancestor that has one.

    Hook children are spawned with setsid (Node's detached: true), so they
    have no controlling terminal and /dev/tty is unavailable. Walk /proc
    up the parent chain; the first ancestor with a controlling terminal
    is the kimi TUI (or something closer to the real terminal)."""

    def stat_fields(pid):
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                data = f.read()
        except OSError:
            return None
        i = data.rfind(")")  # comm may contain spaces/parens
        if i == -1:
            return None
        return data[i + 2:].split()

    pid = os.getppid()
    for _ in range(16):
        fields = stat_fields(pid)
        if fields is None or len(fields) < 5:
            return None
        tty_nr = int(fields[4])
        if tty_nr:
            major = (tty_nr >> 8) & 0xFFF
            minor = (tty_nr & 0xFF) | ((tty_nr >> 12) & 0xFFF00)
            if major == 136:  # devpts: /dev/pts/<minor>
                return f"/dev/pts/{minor}"
            return None  # real console / other tty type: not supported
        pid = int(fields[1])  # ppid (fields: state ppid pgrp session tty_nr)
        if pid <= 1:
            return None
    return None


def win_ancestors():
    """Return [(pid, exe_name)] of this process's ancestors, nearest first."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * wintypes.MAX_PATH)]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snap == -1:
        return []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(pe)
        parents = {}
        if k32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                parents[pe.th32ProcessID] = (pe.th32ParentProcessID,
                                             pe.szExeFile)
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        k32.CloseHandle(snap)
    chain = []
    pid = os.getppid()
    while pid in parents:
        chain.append((pid, parents[pid][1]))
        pid = parents[pid][0]
    return chain


def win_attach_real_console():
    """Attach to the console of the nearest ancestor outside our own.

    On Windows the hook engine spawns children into a private, windowless
    console (ConPTY), so writing CONOUT$ there reaches nothing visible.
    Detach from it and attach to the first ancestor that isn't in our
    console's process list — that's the kimi TUI's real console."""
    import ctypes
    k32 = ctypes.windll.kernel32
    n = 64
    procs = (ctypes.c_ulong * n)()
    count = k32.GetConsoleProcessList(procs, n)
    own = set(procs[:min(count, n)]) if count else set()
    k32.FreeConsole()
    for pid, _name in win_ancestors():
        if pid in own:
            continue
        if k32.AttachConsole(pid):
            return True
    return False


def win_write_console(text):
    """Write text to the console screen buffer via WriteConsoleW.

    UTF-16 output avoids console-code-page (e.g. CP936) mangling of the
    non-ASCII parts of the title."""
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.CreateFileW("CONOUT$", 0x40000000, 3, None, 3, 0, None)
    if h == -1:
        return False
    try:
        written = ctypes.c_ulong(0)
        return bool(k32.WriteConsoleW(h, text, len(text),
                                      ctypes.byref(written), None))
    finally:
        k32.CloseHandle(h)


def set_terminal_title(text):
    """Write an OSC 0 title escape straight to the terminal.

    The kimi TUI only sets the title on session switch / title change, so
    a Stop-hook write survives until then. Escape sequences emit no
    printable characters and don't move the cursor, so the TUI's
    differential rendering bookkeeping is unaffected."""
    safe = "".join(ch for ch in text if ch.isprintable())
    seq = f"\x1b]0;{safe}\x07"
    debug = os.environ.get("KIMI_USAGE_DEBUG")
    if os.name == "nt":
        # Attach to the TUI's real console first (hooks run in a private
        # one), then the OSC 0 write reaches the terminal — Windows
        # Terminal, Warp and modern conhost all parse OSC 0.
        win_attach_real_console()
        ok = win_write_console(seq)
        if debug:
            print(f"kimi-usage: console write {'ok' if ok else 'failed'}",
                  file=sys.stderr)
        return
    candidates = [find_ancestor_tty(), "/dev/tty"]
    for path in dict.fromkeys(c for c in candidates if c):
        try:
            with open(path, "w", encoding="utf-8", errors="ignore") as tty:
                tty.write(seq)
                tty.flush()
            if debug:
                print(f"kimi-usage: title written to {path}", file=sys.stderr)
            return
        except OSError as e:
            if debug:
                print(f"kimi-usage: cannot write {path}: {e}", file=sys.stderr)


def main():
    hook = read_hook_stdin()
    session_id = hook.get("session_id")
    cwd = hook.get("cwd") or os.getcwd()

    session_dir = find_session_dir(session_id, cwd)
    if not session_dir:
        return
    turns, session_total = parse_session(session_dir)
    turn_usage = last_active_turn(turns)
    if turn_usage is None and not session_total["output"]:
        return

    line = "📊 " + stats_line(turn_usage, session_total)
    title = session_title(session_dir)
    set_terminal_title(line + (f" | {title}" if title else ""))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open: never break the agent loop
    sys.exit(0)
