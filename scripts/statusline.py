#!/usr/bin/env python3
"""kimi-usage: bottom status line command for kimi-code 0.30.0+.

The TUI spawns this command up to once per second (300ms cap) with a JSON
snapshot on stdin and renders the first stdout line as the footer status
line. We parse the session wire files to show the session's total input /
output tokens and cache hit rate, plus a per-model breakdown of sub-agent
usage when sub-agents exist.

Data source:
  <KIMI_CODE_HOME>/sessions/wd_*/session_*/agents/*/wire.jsonl
  (usage.record entries; each carries a "model" field)

The line is fitted to the live terminal width (queried from the console,
not the snapshot, which has no width field), degrading cheapest losses
first: the unit shrinks "token" -> "tok" -> dropped, the git branch and
cwd leave the prefix, the "总计："/"缓存" labels drop, " · " tightens,
sub-agent columns fall from the smallest consumer, and only as a last
resort the line is cut with an ellipsis.

Fail-open by design: any error prints a fallback indicator so the user can
see something is wrong, rather than silently falling back to the built-in
layout. Set KIMI_USAGE_DEBUG=1 to append diagnostics to
<KIMI_CODE_HOME>/kimi-usage-debug.log.
"""

import glob
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
import unicodedata


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
    """Cache hit rate as a float percent (None when no input yet). Kept
    unrounded: modern providers sit at 95%+ almost always, so the display
    and the color ramp both need sub-percent resolution up there."""
    total = input_total(u)
    if total <= 0:
        return None
    return u["inputCacheRead"] / total * 100


def _debug(msg):
    """Append a debug line when KIMI_USAGE_DEBUG is set or the flag file
    <KIMI_CODE_HOME>/kimi-usage-debug exists (the latter needs no restart
    of the TUI, so it works for live troubleshooting)."""
    try:
        home = kimi_home()
        if not (os.environ.get("KIMI_USAGE_DEBUG")
                or os.path.exists(os.path.join(home, "kimi-usage-debug"))):
            return
        path = os.path.join(home, "kimi-usage-debug.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _read_stdin_json(timeout=0.18):
    """Read the JSON snapshot from stdin; return {} on any failure.

    The TUI may keep the stdin pipe open after writing the snapshot, so
    never wait for EOF: read1() returns as soon as any data arrives, and
    we give up after `timeout` seconds (the whole command is capped at
    300ms by the TUI). Runs in a daemon thread so a stuck read can never
    hang the process.
    """
    chunks = []
    done = threading.Event()

    def reader():
        try:
            src = getattr(sys.stdin, "buffer", sys.stdin)
            while True:
                data = src.read1(4096) if hasattr(src, "read1") else src.read(1)
                if not data:
                    break
                chunks.append(data)
        except Exception:
            pass
        finally:
            done.set()

    try:
        if sys.stdin.isatty():
            _debug("stdin is a tty")
            return {}
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        deadline = time.monotonic() + timeout
        while not chunks and not done.is_set() and time.monotonic() < deadline:
            done.wait(0.005)
        if chunks and not done.is_set():
            done.wait(0.03)  # grace period for the rest of the payload
        raw = b"".join(
            c if isinstance(c, bytes) else c.encode("utf-8", "ignore")
            for c in chunks
        ).decode("utf-8", "ignore").strip()
        _debug(f"stdin eof={done.is_set()} raw length={len(raw)}")
        return json.loads(raw) if raw else {}
    except Exception as e:
        _debug(f"stdin read failed: {e}")
        return {}


# --------------------------------------------------------------------------
# session resolution
# --------------------------------------------------------------------------

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

def iter_usage_records(path):
    """Yield (kind, record) for lines worth parsing.

    Cheap substring pre-filter keeps this fast on multi-MB wire files:
    only usage.record / profile.bind / swarm_mode lines are JSON-decoded.
    """
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if '"usage.record"' in line:
                    kind = "usage.record"
                elif '"profile.bind"' in line:
                    kind = "profile.bind"
                elif '"swarm_mode.' in line:
                    kind = "swarm"
                else:
                    continue
                try:
                    yield kind, json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def parse_session(session_dir):
    """Return (session_total, sub_by_model, swarm_on, effort).

    session_total: usage aggregated over every agent in the session
    (main + sub-agents).
    sub_by_model:  {model: usage} aggregated over sub-agent wires only.
    swarm_on:      state of the last swarm_mode op persisted in the main
    wire (swarm mode is a wire Model, so the log is authoritative).
    effort:        thinkingEffort of the latest profile.bind in the main
    wire (rebound on every request, so it tracks /effort switches).
    """
    session_total = empty_usage()
    sub_by_model = {}
    swarm_on = False
    effort = None

    for wire in glob.glob(os.path.join(session_dir, "agents", "*",
                                       "wire.jsonl")):
        agent = os.path.basename(os.path.dirname(wire))
        is_main = agent == "main"
        bound_model = None
        for kind, rec in iter_usage_records(wire):
            if kind == "swarm":
                if is_main:
                    swarm_on = rec.get("type") == "swarm_mode.enter"
                continue
            if kind == "profile.bind":
                bound_model = rec.get("modelAlias") or bound_model
                if is_main:
                    effort = rec.get("thinkingEffort", effort)
                continue
            u = {k: int(rec.get("usage", {}).get(k, 0) or 0)
                 for k in session_total}
            add_usage(session_total, u)
            if is_main:
                continue
            model = rec.get("model") or bound_model or "unknown"
            bucket = sub_by_model.get(model)
            if bucket is None:
                bucket = sub_by_model[model] = empty_usage()
            add_usage(bucket, u)

    return session_total, sub_by_model, swarm_on, effort


# --------------------------------------------------------------------------
# status line rendering
# --------------------------------------------------------------------------

def short_model(model):
    """'kimi-code/k3-256k' -> 'k3-256k'; keep the meaningful last segment."""
    return model.rsplit("/", 1)[-1] if model else model


def model_display_names():
    """alias -> display_name, scanned from the [models] table in config.toml.

    Regex scan instead of tomllib to keep Python 3.7 compatibility;
    [models."<alias>".overrides] entries win over the base section.
    """
    header = re.compile(
        r'^\s*\[\s*models\.\s*(?:"([^"]+)"|([A-Za-z0-9_\-]+))'
        r'(\s*\.\s*overrides\s*)?\]\s*$')
    disp = re.compile(r'^\s*display_name\s*=\s*"([^"]+)"')
    names, overrides = {}, {}
    current, is_override = None, False
    try:
        with open(os.path.join(kimi_home(), "config.toml"),
                  encoding="utf-8") as f:
            for line in f:
                m = header.match(line)
                if m:
                    current = m.group(1) or m.group(2)
                    is_override = bool(m.group(3))
                    continue
                if line.lstrip().startswith("["):
                    current, is_override = None, False
                    continue
                if current:
                    d = disp.match(line)
                    if d:
                        (overrides if is_override else names)[current] = \
                            d.group(1)
    except OSError:
        pass
    names.update(overrides)
    return names


def model_default_efforts():
    """alias -> default_effort, scanned from the [models] table in
    config.toml. Used as the thinking-effort fallback when the session
    wire log is not available yet (e.g. a brand-new conversation whose
    session directory has not been located)."""
    header = re.compile(
        r'^\s*\[\s*models\.\s*(?:"([^"]+)"|([A-Za-z0-9_\-]+))'
        r'(\s*\.\s*overrides\s*)?\]\s*$')
    eff = re.compile(r'^\s*default_effort\s*=\s*"([^"]+)"')
    names, overrides = {}, {}
    current, is_override = None, False
    try:
        with open(os.path.join(kimi_home(), "config.toml"),
                  encoding="utf-8") as f:
            for line in f:
                m = header.match(line)
                if m:
                    current = m.group(1) or m.group(2)
                    is_override = bool(m.group(3))
                    continue
                if line.lstrip().startswith("["):
                    current, is_override = None, False
                    continue
                if current:
                    d = eff.match(line)
                    if d:
                        (overrides if is_override else names)[current] = \
                            d.group(1)
    except OSError:
        pass
    names.update(overrides)
    return names


def _hsl_to_rgb(h, s, l):
    """h in degrees, s/l in 0..1 -> (r, g, b) ints."""
    h = h % 360
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = l - c / 2
    if h < 60:
        rp, gp, bp = c, x, 0
    elif h < 120:
        rp, gp, bp = x, c, 0
    elif h < 180:
        rp, gp, bp = 0, c, x
    elif h < 240:
        rp, gp, bp = 0, x, c
    elif h < 300:
        rp, gp, bp = x, 0, c
    else:
        rp, gp, bp = c, 0, x
    return tuple(round((v + m) * 255) for v in (rp, gp, bp))


# (rate%, hue°, saturation, lightness): a continuous HSL health ramp —
# brick red, through amber, into a jade "healthy" green. Resolution is
# deliberately concentrated at the high end: 0-95% travels hue 2->80,
# 95-100% travels 80->140, because modern providers almost never drop
# below 95%. Saturation/lightness stay moderate for light/dark themes.
_CACHE_STOPS = [
    (0,    2,   0.55, 0.52),
    (50,   22,  0.58, 0.50),
    (75,   38,  0.60, 0.49),
    (90,   55,  0.58, 0.47),
    (95,   80,  0.52, 0.46),
    (97,   100, 0.48, 0.45),
    (98,   112, 0.46, 0.45),
    (99,   124, 0.44, 0.45),
    (100,  140, 0.42, 0.45),
]


def _cache_sgr(rate):
    """Cache-rate color: continuous per-percent gradient emitted as a
    24-bit SGR sequence (Windows Terminal / Warp / GNOME Terminal all
    support truecolor; the TUI passes embedded escapes through)."""
    rate = max(0, min(100, rate))
    stops = _CACHE_STOPS
    for i in range(len(stops) - 1):
        r0, h0, s0, l0 = stops[i]
        r1, h1, s1, l1 = stops[i + 1]
        if rate <= r1:
            t = (rate - r0) / (r1 - r0)
            rgb = _hsl_to_rgb(h0 + (h1 - h0) * t,
                              s0 + (s1 - s0) * t,
                              l0 + (l1 - l0) * t)
            return "38;2;%d;%d;%d" % rgb
    rgb = _hsl_to_rgb(stops[-1][1], stops[-1][2], stops[-1][3])
    return "38;2;%d;%d;%d" % rgb


def stats_line(session_total, sub_by_model, display_names,
               unit="token", total_label=True, cache_label=True,
               dot_spaces=True, max_subs=None):
    """Session totals first, then per-model sub-agent usage, so the most
    important info survives narrow terminals. Input is painted blue,
    output magenta, and the cache rate on a red->green ramp. At 95%+ the
    rate keeps one decimal (95.3%); only a true 100% stays an integer.

    Compaction knobs (used by _fit_line to degrade gracefully): unit is
    the token-unit suffix ("token" -> "tok" -> ""), total_label drops
    "总计：", cache_label drops the "缓存" label keeping the colored
    percent, dot_spaces tightens " · " to "·", and max_subs caps how
    many sub-agent columns are appended (they are sorted by input
    tokens, so a cap drops the smallest consumers first)."""
    def seg(u):
        suffix = f" {unit}" if unit else ""
        s = (_paint(f"↑ {fmt_tokens(input_total(u))}{suffix}", "34")
             + _paint(" · " if dot_spaces else "·", "2")
             + _paint(f"↓ {fmt_tokens(u['output'])}{suffix}", "35"))
        r = cache_hit_rate(u)
        if r is not None:
            if r >= 100:
                text = "100%"
            elif r >= 95:
                text = f"{min(r, 99.9):.1f}%"
            else:
                text = f"{round(r)}%"
            s += _paint((" 缓存 " if cache_label else " ") + text,
                        _cache_sgr(r))
        return s

    line = ("总计：" if total_label else "") + seg(session_total)
    subs = []
    for model, u in sorted(sub_by_model.items(),
                           key=lambda kv: input_total(kv[1]),
                           reverse=True):
        if not (input_total(u) or u["output"]):
            continue
        name = display_names.get(model) or short_model(model)
        subs.append(f"{name} {seg(u)}")
    if max_subs is not None:
        subs = subs[:max_subs]
    if subs:
        line += " | " + " | ".join(subs)
    return line


# --------------------------------------------------------------------------
# width detection and graceful degradation
# --------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(s):
    """Terminal column count of a rendered line: ANSI escapes ignored,
    East Asian wide/fullwidth chars (总计, 缓存, …) count 2."""
    plain = _ANSI_RE.sub("", s)
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in plain)


def _truncate(s, width):
    """Hard-cut a rendered line to `width` columns with an ellipsis,
    keeping ANSI escapes intact and appending a reset so the TUI's own
    painting is not poisoned."""
    out, used, i = [], 0, 0
    while i < len(s):
        m = _ANSI_RE.match(s, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        ch = s[i]
        w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
        i += 1
    out.append("…")
    if _colors_on():
        out.append("\x1b[0m")
    return "".join(out)


def _terminal_width(payload):
    """Best-effort width of the terminal the TUI is running in.

    The TUI snapshot carries no width field (checked against kimi-code
    0.30 docs) and stdout is a pipe, so the console is queried directly:
    CONOUT$ on Windows (the spawned command inherits the TUI's console),
    /dev/tty on POSIX. Returns None when undetectable — the caller then
    emits the full line and lets the TUI truncate as before."""
    for key in ("width", "terminalWidth", "cols"):
        w = payload.get(key)
        if isinstance(w, int) and not isinstance(w, bool) and w > 0:
            return w
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class _CSBI(ctypes.Structure):
                _fields_ = [("dwSize", wintypes._COORD),
                            ("dwCursorPosition", wintypes._COORD),
                            ("wAttributes", wintypes.WORD),
                            ("srWindow", wintypes.SMALL_RECT),
                            ("dwMaximumWindowSize", wintypes._COORD)]

            h = ctypes.windll.kernel32.CreateFileW(
                "CONOUT$", 0xC0000000, 3, None, 3, 0, None)
            if h and h != -1:
                info = _CSBI()
                try:
                    if ctypes.windll.kernel32.GetConsoleScreenBufferInfo(
                            h, ctypes.byref(info)):
                        w = info.srWindow.Right - info.srWindow.Left + 1
                        if w > 0:
                            return w
                finally:
                    ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            pass
    else:
        try:
            fd = os.open("/dev/tty", os.O_RDONLY)
            try:
                w = os.get_terminal_size(fd).columns
                if w > 0:
                    return w
            finally:
                os.close(fd)
        except OSError:
            pass
    try:
        w = int(os.environ.get("COLUMNS", ""))
        if w > 0:
            return w
    except ValueError:
        pass
    return None


def _fit_line(payload, session_total, sub_by_model, display_names,
              swarm_on, effort, width):
    """Render prefix + stats to fit `width` columns by graceful
    degradation, cheapest losses first: unit "token" -> "tok" ->
    dropped, git branch dropped, cwd dropped, the "总计：" and "缓存"
    labels dropped, " · " tightened to "·", then sub-agent columns from
    the smallest consumer, the whole prefix, and only as a last resort
    an ellipsis cut. With width=None the full line is returned and the
    TUI truncates as before."""
    base = dict(unit="token", total_label=True, cache_label=True,
                dot_spaces=True, with_cwd=True, with_git=True)
    # each stage updates cumulatively on top of the previous one
    stages = [
        {},                     # full
        {"unit": "tok"},
        {"with_git": False},
        {"with_cwd": False},
        {"unit": ""},
        {"total_label": False},
        {"cache_label": False},
        {"dot_spaces": False},
    ]

    def compose(o, max_subs=None, with_prefix=True):
        usage = stats_line(session_total, sub_by_model, display_names,
                           unit=o["unit"], total_label=o["total_label"],
                           cache_label=o["cache_label"],
                           dot_spaces=o["dot_spaces"], max_subs=max_subs)
        if not with_prefix:
            return usage
        p = prefix_line(payload, display_names, swarm_on, effort,
                        with_cwd=o["with_cwd"], with_git=o["with_git"])
        return f"{p} | {usage}" if p else usage

    opts = dict(base)
    line = compose(opts)
    if not width or _visible_len(line) <= width:
        return line
    for stage in stages[1:]:
        opts.update(stage)
        line = compose(opts)
        if _visible_len(line) <= width:
            return line
    n = len(sub_by_model)
    while n > 0:
        n -= 1
        line = compose(opts, max_subs=n)
        if _visible_len(line) <= width:
            return line
    line = compose(opts, max_subs=0, with_prefix=False)
    if _visible_len(line) <= width:
        return line
    return _truncate(line, width)


def _colors_on():
    # TERM=dumb means no color rendering (also what CI/tool harnesses set);
    # NO_COLOR is deliberately NOT honored — agent shells often export it and
    # it would silently strip colors from the TUI's status line spawns too.
    return (os.environ.get("TERM", "") != "dumb"
            and not os.environ.get("KIMI_USAGE_NO_COLOR"))


def _paint(text, code):
    """Wrap text in an ANSI SGR escape. The TUI paints the whole custom
    line in colors.text, so embedded escapes reproduce the built-in
    footer's per-slot colors: amber bold for auto/yolo, blue bold for
    plan, dim for cwd/git. Named colors follow the terminal's own palette,
    which keeps theme="auto" sensible in both dark and light terminals."""
    if not text or not _colors_on():
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def shorten_cwd(path):
    """Mirror the built-in footer's shortenCwd: '~' for home, otherwise the
    last 3 segments as '…/a/b/c'."""
    if not path:
        return path
    path = path.replace("\\", "/")
    home = os.path.expanduser("~").replace("\\", "/")
    if path == home:
        return "~"
    if home:
        cmp = (lambda p: os.path.normcase(p)) if os.name == "nt" else (lambda p: p)
        if cmp(path).startswith(cmp(home) + "/"):
            path = "~" + path[len(home):]
    segments = [s for s in path.split("/") if s]
    if len(segments) <= 3:
        return path
    return "…/" + "/".join(segments[-3:])


def prefix_line(payload, display_names, swarm_on, effort,
                with_cwd=True, with_git=True):
    """Rebuilt on every run (not cached). Reproduces the built-in footer
    line 1 slots that the TUI snapshot exposes, in the original order and
    spacing (two-space separated): mode badges (bare words, only when
    active), model display_name with thinking-effort suffix, shortened
    cwd, git branch. with_cwd / with_git drop those slots for narrow
    terminals (cwd is the longest and least load-bearing, so it goes
    first).

    swarm state and effort come from the session wire log (the TUI
    snapshot omits both); goal/tasks badges, rotating tips and git diff
    stats are not reproducible."""
    parts = []
    modes = []
    perm = payload.get("permissionMode") or ""
    if perm == "auto":
        modes.append(_paint("auto", "33;1"))
    if perm == "yolo":
        modes.append(_paint("yolo", "33;1"))
    if payload.get("planMode"):
        modes.append(_paint("plan", "34;1"))
    if swarm_on:
        modes.append(_paint("swarm", "36;1"))
    if modes:
        parts.append(" ".join(modes))
    model = payload.get("model") or ""
    if model:
        label = display_names.get(model) or short_model(model)
        # Mirror the built-in model slot: concrete effort levels render as
        # "thinking: max", boolean-on legacy models as plain " thinking".
        if effort is True:
            label += " thinking"
        elif effort and effort not in ("off", "on", False):
            label += f" thinking: {effort}"
        elif effort == "on":
            label += " thinking"
        parts.append(label)
    cwd = shorten_cwd(payload.get("cwd") or "") if with_cwd else ""
    if cwd:
        parts.append(_paint(cwd, "2"))
    branch = payload.get("gitBranch") if with_git else None
    if branch:
        parts.append(_paint(branch, "2"))
    return "  ".join(parts)


# --------------------------------------------------------------------------
# per-session cache (invalidate on any wire.jsonl mtime change)
# --------------------------------------------------------------------------

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


def _cached_stats(session_dir, session_id):
    """Return (session_total, sub_by_model, swarm_on, effort) when the
    wire files are unchanged since the cache write. Raw stats are cached
    rather than the rendered line: rendering depends on the live terminal
    width, which can change between runs (window resize)."""
    try:
        with open(_cache_path(session_id), encoding="utf-8") as f:
            c = json.load(f)
        if c.get("v") == 10 and c.get("mtime") == _max_wire_mtime(session_dir):
            _debug("cache hit")
            return (c["session_total"], c["sub_by_model"],
                    bool(c.get("swarm")), c.get("effort"))
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


def _save_cache(session_dir, session_id, session_total, sub_by_model,
                swarm_on, effort):
    try:
        with open(_cache_path(session_id), "w", encoding="utf-8") as f:
            json.dump({"v": 10, "mtime": _max_wire_mtime(session_dir),
                       "session_total": session_total,
                       "sub_by_model": sub_by_model, "swarm": swarm_on,
                       "effort": effort}, f)
    except OSError:
        pass


def main():
    _debug("invoked")
    payload = _read_stdin_json()
    _debug(f"payload keys={list(payload.keys())}")
    session_id = payload.get("sessionId") or payload.get("session_id")
    cwd = payload.get("cwd") or os.getcwd()

    session_dir = find_session_dir(session_id, cwd)
    _debug(f"session_id={session_id!r} cwd={cwd!r} session_dir={session_dir!r}")
    if not session_dir:
        # Fresh conversations can fail session resolution for a moment
        # (no state.json / wire files yet). Still render the prefix slots
        # from the snapshot; the effort suffix falls back to the model's
        # default_effort from config.toml since there is no wire log.
        effort = model_default_efforts().get(payload.get("model") or "")
        prefix = prefix_line(payload, model_display_names(), False, effort)
        note = _paint("开启对话以显示上下文情况", "2")
        line = f"{prefix} | {note}" if prefix else note
        _debug(f"line={line!r}")
        print(line)
        return

    cached = _cached_stats(session_dir, session_id) if session_id else None
    if cached is not None:
        session_total, sub_by_model, swarm_on, effort = cached
    else:
        session_total, sub_by_model, swarm_on, effort = \
            parse_session(session_dir)
        _debug(f"swarm_on={swarm_on} effort={effort!r} "
               f"sub_models={list(sub_by_model)}")
        if session_id:
            _save_cache(session_dir, session_id, session_total,
                        sub_by_model, swarm_on, effort)

    # Rendering happens on every run (cache holds raw stats): the line is
    # fitted to the live terminal width, so a window resize takes effect
    # on the next refresh instead of after the wire files change.
    width = _terminal_width(payload)
    line = _fit_line(payload, session_total, sub_by_model,
                     model_display_names(), swarm_on, effort, width)
    _debug(f"width={width} line={line!r}")
    print(line)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _debug(f"exception: {e}\n{traceback.format_exc()}")
        print("kimi-usage: 错误")
    sys.exit(0)
