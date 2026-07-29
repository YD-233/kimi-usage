#!/usr/bin/env python3
"""kimi-usage: status line command for kimi-code 0.30.0+.

The TUI spawns this command up to once per second with a JSON snapshot on
stdin and renders the first stdout line as the footer status line. We parse
the session wire files (same data source as the Stop hook) to show per-turn
token usage and cache hit rate.

Data source:
  <KIMI_CODE_HOME>/sessions/wd_*/session_*/agents/*/wire.jsonl

Fail-open by design: any error prints an empty line, so the TUI falls back
to its built-in status line layout.
"""

import glob
import hashlib
import json
import os
import select
import sys

from usage import (
    cache_hit_rate,
    find_session_dir,
    input_total,
    kimi_home,
    last_active_turn,
    parse_session,
    stats_line,
)


def _read_stdin_json(timeout=0.5):
    """Read a JSON object from stdin; return {} on any failure."""
    try:
        if sys.stdin.isatty():
            return {}
        if os.name == "nt":
            raw = sys.stdin.read().strip()
        else:
            r, _, _ = select.select([sys.stdin], [], [], timeout)
            if not r:
                return {}
            raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _wire_files(session_dir):
    return glob.glob(os.path.join(session_dir, "agents", "*", "wire.jsonl"))


def _max_wire_mtime(session_dir):
    files = _wire_files(session_dir)
    if not files:
        return 0
    return max((os.path.getmtime(f) for f in files), default=0)


def _cache_path(session_id):
    base = os.path.join(kimi_home(), "kimi-usage-cache")
    os.makedirs(base, exist_ok=True)
    safe = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return os.path.join(base, f"{safe}.json")


def _cached_line(session_dir, session_id):
    try:
        with open(_cache_path(session_id), encoding="utf-8") as f:
            c = json.load(f)
        if c.get("mtime") == _max_wire_mtime(session_dir):
            return c.get("line")
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _save_cache(session_dir, session_id, line):
    try:
        with open(_cache_path(session_id), "w", encoding="utf-8") as f:
            json.dump({"mtime": _max_wire_mtime(session_dir), "line": line}, f)
    except OSError:
        pass


def main():
    payload = _read_stdin_json()
    session_id = payload.get("sessionId")
    cwd = payload.get("cwd") or os.getcwd()

    session_dir = find_session_dir(session_id, cwd)
    if not session_dir:
        print("")
        return

    if session_id:
        line = _cached_line(session_dir, session_id)
        if line is not None:
            print(line)
            return

    turns, session_total = parse_session(session_dir)
    turn_usage = last_active_turn(turns)
    line = stats_line(turn_usage, session_total)
    if session_id:
        _save_cache(session_dir, session_id, line)
    print(line)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("")
    sys.exit(0)
