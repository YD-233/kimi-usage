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

Wire files are read incrementally: the per-session cache holds a byte
offset per wire file, so a refresh only parses what was appended since the
last one. The first sighting of a huge session is spread over several
refreshes (at most _MAX_TAIL_BYTES per file per run), because being killed
mid-parse loses the cache write and would freeze the line forever.

The line is fitted to the live terminal width (queried from the console,
not the snapshot, which has no width field), degrading cheapest losses
first: the unit shrinks "token" -> "tok" -> dropped, the git branch and
cwd leave the prefix, the "总计："/"缓存" labels drop, " · " tightens,
sub-agent columns fall from the smallest consumer, and only as a last
resort the line is cut with an ellipsis.

The custom line replaces the built-in footer line 1 entirely (line 2, the
right-aligned context readout, stays built-in), so the built-in slots that
would otherwise vanish are reproduced here in upstream's order and
spacing: mode badges, goal badge (wall clock anchored to when the goal
snapshot last changed, mirroring upstream's goalObservedAtMs), model +
thinking effort (tracked from the wire's llm.request / profile.bind
records, which follow /effort switches), background task badges, cwd, git
branch with diff stats and PR badge — and the /dance easter egg's rainbow
model label (detected from the per-cwd input history, where /dance
commands are recorded; hold freezes at the same palette phase upstream's
3s flow lands on). Only the rotating tips are skipped: upstream itself
never renders them next to a custom line.

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
import shutil
import sys
import threading
import time
import traceback
import unicodedata


def _memo(fn):
    """Cache a helper's result for the life of the process.

    _fit_line rebuilds the prefix once per degradation stage, and these
    slots each cost a stat storm (142 task files on a big session) or a
    subprocess. One run renders one line, i.e. one instant, so caching
    within the process cannot go stale.
    """
    cache = {}

    def wrapper(*args):
        try:
            if args not in cache:
                cache[args] = fn(*args)
            return cache[args]
        except TypeError:      # unhashable argument: just don't cache
            return fn(*args)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def kimi_home():
    return os.environ.get(
        "KIMI_CODE_HOME", os.path.expanduser("~/.kimi-code")
    )


def force_utf8_stdout():
    """Print UTF-8 no matter what the console code page is.

    The TUI reads our stdout with setEncoding('utf-8'), but Python picks the
    ANSI code page for a pipe: on a cp1252 console every CJK label raises
    UnicodeEncodeError, the error fallback raises again, and the user just
    silently gets the built-in footer.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


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


_PLUGIN_ID = "kimi-usage"


def plugin_disabled():
    """Whether this managed plugin copy has been disabled or removed.

    `/plugins disable` only stops the plugin's hooks and `/plugins remove`
    only drops its record — the managed copy and the tui.toml command both
    stay on disk, so without this check the status line keeps rendering long
    after the user thought they had turned it off.

    Only a copy living under plugins/managed/ polices itself: a dev checkout
    or a hand-written [status_line].command has no plugin record to consult
    and must keep working. An unreadable installed.json fails open.
    """
    here = os.path.normcase(os.path.abspath(__file__))
    if os.path.normcase(os.path.join("plugins", "managed")) not in here:
        return False
    try:
        with open(os.path.join(kimi_home(), "plugins", "installed.json"),
                  encoding="utf-8") as f:
            records = json.load(f).get("plugins")
    except (OSError, json.JSONDecodeError, AttributeError):
        return False
    if not isinstance(records, list):
        return False
    for rec in records:
        if isinstance(rec, dict) and rec.get("id") == _PLUGIN_ID:
            return not rec.get("enabled", True)
    return True     # no record left: the plugin was removed


def restore_builtin_statusline():
    """Take our command out of tui.toml so the TUI stops spawning us."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import setup_statusline
        setup_statusline.remove_block()
    except Exception as e:
        _debug(f"could not remove the tui.toml block: {e}")


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

# Records worth decoding. Matched on the line's own "type" field rather than
# a substring search: tool arguments and results are persisted verbatim
# inside context.append_loop_event lines, so a line that merely *quotes* a
# record name (reading this very file does it) would otherwise be taken for
# the record itself — which used to clear the swarm badge.
_WANTED_RECORDS = frozenset((
    "usage.record", "profile.bind", "swarm_mode.enter", "swarm_mode.exit",
    "llm.request", "config.update",
))
_TYPE_RE = re.compile(rb'^\{\s*"type"\s*:\s*"([^"]{1,40})"')

# Reserved aliases that stand in for a real model: the subagent recipe
# synthesizes __secondary__ (SECONDARY_DERIVED_MODEL_ALIAS upstream) and the
# spawn tools accept the symbolic primary/secondary choices. usage.record
# stores whichever alias was bound, so these have to be mapped back to the
# concrete model from the same agent's llm.request records.
_PLACEHOLDER_ALIASES = frozenset(("__secondary__", "secondary", "primary"))

# Most bytes a single wire file may contribute per run, and the wall-clock
# budget for the whole parse. A first sighting of a huge session is spread
# over consecutive refreshes instead of blowing the 300ms cap — a killed run
# saves nothing, so it would re-read the same bytes forever and the line
# would never update again. The time budget makes that hold on a slow
# machine too: whatever is left keeps its cursor and is picked up next run.
_MAX_TAIL_BYTES = 12_000_000
_PARSE_BUDGET_S = 0.12


def read_wire_tail(path, start, mid, size):
    """Read the bytes appended to `path` after offset `start`.

    Returns (complete_lines, new_offset, mid). Nothing appended means no
    open() at all — and the read is sized to what is actually there, because
    read(_MAX_TAIL_BYTES) preallocates that many bytes even at EOF, which on
    a session with dozens of sub-agents costs more than the parse itself.
    A trailing partial line (the TUI may be mid-write) is left for the next
    run, so the offset only ever advances past a newline. `mid` is carried
    over when a single line is longer than the read cap: the offset then
    lands inside that line and the next run drops bytes up to the following
    newline.
    """
    remaining = size - start
    if remaining <= 0:
        return b"", start, mid
    try:
        with open(path, "rb") as f:
            f.seek(start)
            chunk = f.read(min(_MAX_TAIL_BYTES, remaining))
    except OSError:
        return b"", start, mid
    at_eof = len(chunk) >= remaining
    if mid:
        nl = chunk.find(b"\n")
        if nl < 0:
            return b"", start + len(chunk), not at_eof
        start += nl + 1
        chunk = chunk[nl + 1:]
    cut = chunk.rfind(b"\n")
    if cut < 0:
        # no complete line: either wait for the writer, or step over a line
        # that is longer than the cap (a huge tool result, never a record).
        return b"", start if at_eof else start + len(chunk), not at_eof
    return chunk[:cut + 1], start + cut + 1, False


def iter_wire_records(chunk):
    """Yield (type, record) for the records of interest in `chunk`."""
    for raw in chunk.splitlines():
        m = _TYPE_RE.match(raw)
        if m is None:
            continue
        kind = m.group(1).decode("ascii", "ignore")
        if kind not in _WANTED_RECORDS:
            continue
        try:
            rec = json.loads(raw.decode("utf-8", "ignore"))
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            yield kind, rec


def _usage_of(rec, keys):
    """The record's usage counters, coerced to ints; None when unusable."""
    usage = rec.get("usage")
    if not isinstance(usage, dict):
        return None
    out = {}
    for k in keys:
        try:
            out[k] = int(usage.get(k, 0) or 0)
        except (TypeError, ValueError):
            out[k] = 0
    return out


def parse_session(session_dir, prior=None):
    """Return (session_total, sub_by_model, swarm_on, effort, files).

    session_total: usage aggregated over every agent in the session
    (main + sub-agents).
    sub_by_model:  {model: usage} aggregated over sub-agent wires only.
    swarm_on:      state of the last swarm_mode op persisted in the main
    wire (swarm mode is a wire Model, so the log is authoritative).
    effort:        thinkingEffort of the latest effort-bearing record in
    the main wire. profile.bind is only written on (re)bind, so the
    freshest source is llm.request (written on every model request);
    config.update covers the legacy engine. This tracks /effort
    switches as soon as the next request goes out.
    files:         per-agent parse cursor ({agent: {offset, mid, bound,
    real}}) to hand back as `prior` next time.

    With `prior` the totals are carried over and only the bytes appended
    since the recorded offsets are read, so a refresh costs the same on a
    100MB session as on a fresh one. A wire that shrank (append-only in
    practice, but a copied or truncated session would) forces one full
    re-parse, since per-file subtotals are not kept.
    """
    prior = prior if isinstance(prior, dict) else {}
    try:
        session_total = add_usage(empty_usage(),
                                  prior.get("session_total") or {})
    except (TypeError, ValueError):
        session_total = empty_usage()      # hand-edited / corrupt cache
    sub_by_model = {}
    prior_subs = prior.get("sub_by_model")
    prior_subs = prior_subs if isinstance(prior_subs, dict) else {}
    for model, u in prior_subs.items():
        if not isinstance(u, dict):
            continue
        try:
            sub_by_model[str(model)] = add_usage(empty_usage(), u)
        except (TypeError, ValueError):
            continue                   # skip the corrupt bucket only
    swarm_on = bool(prior.get("swarm"))
    effort = prior.get("effort")
    prior_files = prior.get("files")
    prior_files = prior_files if isinstance(prior_files, dict) else {}
    # start from the previous cursors so an early stop (time budget) leaves
    # the untouched files where they were instead of rewinding them to 0
    files = {k: v for k, v in prior_files.items() if isinstance(v, dict)}
    deadline = time.monotonic() + _PARSE_BUDGET_S

    for wire in glob.glob(os.path.join(glob.escape(session_dir), "agents",
                                       "*", "wire.jsonl")):
        agent = os.path.basename(os.path.dirname(wire))
        is_main = agent == "main"
        cursor = prior_files.get(agent)
        cursor = cursor if isinstance(cursor, dict) else {}
        try:
            start = max(0, int(cursor.get("offset") or 0))
        except (TypeError, ValueError):
            start = 0
        bound_model = cursor.get("bound")
        real_model = cursor.get("real")
        try:
            size = os.path.getsize(wire)
        except OSError:
            continue
        if start and size < start:
            _debug(f"{agent}: wire shrank below cursor, full re-parse")
            return parse_session(session_dir, None)
        if size > start and time.monotonic() > deadline:
            _debug(f"{agent}: parse budget spent, resuming next run")
            continue

        chunk, offset, mid = read_wire_tail(wire, start,
                                            bool(cursor.get("mid")), size)
        for kind, rec in iter_wire_records(chunk):
            if kind in ("swarm_mode.enter", "swarm_mode.exit"):
                if is_main:
                    swarm_on = kind == "swarm_mode.enter"
                continue
            if kind == "profile.bind":
                bound_model = rec.get("modelAlias") or bound_model
                if is_main:
                    effort = rec.get("thinkingEffort",
                                     rec.get("thinkingLevel", effort))
                continue
            if kind in ("llm.request", "config.update"):
                bound_model = rec.get("modelAlias") or bound_model
                # llm.request carries the concrete model behind the bound
                # alias — the only way back to a real name when the alias is
                # a placeholder.
                if kind == "llm.request" and rec.get("model"):
                    real_model = rec["model"]
                if is_main:
                    e = rec.get("thinkingEffort",
                                rec.get("thinkingLevel"))
                    if e is not None:
                        effort = e
                continue
            u = _usage_of(rec, session_total)
            if u is None:
                continue
            add_usage(session_total, u)
            if is_main:
                continue
            model = rec.get("model")
            if not isinstance(model, str):
                model = ""
            if not model or model in _PLACEHOLDER_ALIASES:
                model = real_model or bound_model or model
            if not isinstance(model, str) or not model:
                model = "unknown"
            bucket = sub_by_model.get(model)
            if bucket is None:
                bucket = sub_by_model[model] = empty_usage()
            add_usage(bucket, u)

        files[agent] = {"offset": offset, "mid": mid,
                        "bound": bound_model, "real": real_model}

    return session_total, sub_by_model, swarm_on, effort, files


# --------------------------------------------------------------------------
# status line rendering
# --------------------------------------------------------------------------

def short_model(model):
    """'kimi-code/k3-256k' -> 'k3-256k'; keep the meaningful last segment."""
    return model.rsplit("/", 1)[-1] if model else model


@_memo
def _model_config():
    """One scan of the [models] table in config.toml.

    Returns (display_names, default_efforts): alias -> display_name and
    alias -> default_effort. Regex scan instead of tomllib to keep Python
    3.7 compatibility; [models."<alias>".overrides] entries win over the
    base section. Both maps additionally register the entry's
    provider-side `model` and its display_name as keys for the same
    value, because the spellings seen at runtime vary: the snapshot's
    "model" field is already the display name (upstream's
    statusLinePayload runs it through modelDisplayName), older versions
    sent the alias, and a sub-agent whose usage was booked under a
    placeholder alias leaves only the provider-side name in the wire —
    which should still read "DeepSeek V4 Pro", not "deepseek-v4-pro".
    """
    header = re.compile(
        r'^\s*\[\s*models\.\s*(?:"([^"]+)"|([A-Za-z0-9_\-]+))'
        r'(\s*\.\s*overrides\s*)?\]\s*$')
    disp = re.compile(r'^\s*display_name\s*=\s*"([^"]+)"')
    mdl = re.compile(r'^\s*model\s*=\s*"([^"]+)"')
    eff = re.compile(r'^\s*default_effort\s*=\s*"([^"]+)"')
    names, names_o = {}, {}
    models, models_o = {}, {}
    efforts, efforts_o = {}, {}
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
                if not current:
                    continue
                d = disp.match(line)
                if d:
                    (names_o if is_override else names)[current] = \
                        d.group(1)
                    continue
                p = mdl.match(line)
                if p:
                    (models_o if is_override else models)[current] = \
                        p.group(1)
                    continue
                e = eff.match(line)
                if e:
                    (efforts_o if is_override else efforts)[current] = \
                        e.group(1)
    except OSError:
        pass
    names.update(names_o)
    models.update(models_o)
    efforts.update(efforts_o)
    for alias, provider_model in models.items():
        if provider_model not in names and alias in names:
            names[provider_model] = names[alias]
        if alias in efforts:
            efforts.setdefault(provider_model, efforts[alias])
    for alias, disp_name in names.items():
        if alias in efforts:
            efforts.setdefault(disp_name, efforts[alias])
    return names, efforts


def model_display_names():
    """alias/provider-model -> display_name from config.toml; see
    _model_config."""
    return _model_config()[0]


def model_default_efforts():
    """model spelling -> default_effort from config.toml (alias, provider
    model and display name all work as keys); see _model_config. Used as
    the thinking-effort fallback when the session wire log is not
    available yet (e.g. a brand-new conversation whose session directory
    has not been located)."""
    return _model_config()[1]


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

# SGR spans plus OSC 8 hyperlinks (ESC ]8;;url BEL ... ESC ]8;; BEL,
# optionally ST-terminated) — the PR badge is hyperlinked, and both width
# math and truncation must treat the whole sequence as zero-width.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# Upstream wraps the footer in GutterContainer(CHROME_GUTTER, CHROME_GUTTER),
# so the status line gets the terminal width minus one column on each side.
_CHROME_GUTTER = 1


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
        out.append(_SGR_CLOSE)
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
              swarm_on, effort, width, session_dir=None,
              goal_observed=None):
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
                        with_cwd=o["with_cwd"], with_git=o["with_git"],
                        session_dir=session_dir, goal_observed=goal_observed)
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


# Close a painted span with "normal intensity + default foreground" instead
# of a full reset. The TUI wraps our whole line in chalk.hex(colors.text),
# and chalk re-opens its color wherever it finds its own close code
# (\x1b[39m) inside the string — a full \x1b[0m just kills it, which would
# leave everything after our first colored span (i.e. the entire usage
# section) in the terminal's default foreground instead of the theme's.
_SGR_CLOSE = "\x1b[22m\x1b[39m"


def _paint(text, code):
    """Wrap text in an ANSI SGR escape. The TUI paints the whole custom
    line in colors.text, so embedded escapes reproduce the built-in
    footer's per-slot colors: amber bold for auto/yolo, blue bold for
    plan, dim for cwd/git. Named colors follow the terminal's own palette,
    which keeps theme="auto" sensible in both dark and light terminals."""
    if not text or not _colors_on():
        return text
    return f"\x1b[{code}m{text}{_SGR_CLOSE}"


# --------------------------------------------------------------------------
# /dance easter egg (rainbow model label)
# --------------------------------------------------------------------------

# Palettes from upstream src/tui/easter-eggs/dance.ts.
_DARK_RAINBOW = ["#4FA8FF", "#5BC0BE", "#4EC87E", "#E8A838",
                 "#FFCB6B", "#C678B8", "#A274D9", "#7C8DFF"]
_LIGHT_RAINBOW = ["#1565C0", "#00838F", "#0E7A38", "#92660A", "#9A4A00",
                  "#B91C1C", "#8A3A75", "#6B3A9A", "#354CB5"]
_DANCE_FLOW_S = 3.0      # upstream DANCE_FLOW_MS = 3000


@_memo
def _dance_palette():
    """Light palette only for an explicit light theme; dark otherwise
    (upstream picks by theme text color, which we can't see)."""
    try:
        with open(os.path.join(kimi_home(), "tui.toml"),
                  encoding="utf-8") as f:
            if re.search(r'(?m)^\s*theme\s*=\s*"light"', f.read()):
                return _LIGHT_RAINBOW
    except OSError:
        pass
    return _DARK_RAINBOW


def _rainbow_text(text, phase, palette):
    """Paint text char-by-char through the palette, skipping spaces —
    a byte-for-byte port of upstream rainbowText(), as 24-bit SGR."""
    if not text or not _colors_on():
        return text
    out, i = [], phase
    for ch in text:
        if ch == " ":
            out.append(ch)
            continue
        hexc = palette[i % len(palette)]
        i += 1
        r, g, b = (int(hexc[j:j + 2], 16) for j in (1, 3, 5))
        out.append(f"\x1b[38;2;{r};{g};{b}m{ch}{_SGR_CLOSE}")
    return "".join(out)


def _history_path(cwd):
    """Per-cwd input history: <home>/user-history/md5(cwd).jsonl. The TUI
    hashes its own workDir spelling, so try the likely variants."""
    if not cwd:
        return None
    variants = [cwd, cwd.replace("/", "\\"), cwd.replace("\\", "/"),
                os.path.normpath(cwd)]
    seen = set()
    for v in variants:
        if v in seen:
            continue
        seen.add(v)
        p = os.path.join(kimi_home(), "user-history",
                         hashlib.md5(v.encode("utf-8")).hexdigest()
                         + ".jsonl")
        if os.path.isfile(p):
            return p
    return None


@_memo
def dance_state(cwd):
    """Reproduce the /dance easter egg state from the input history, where
    every submitted slash command is recorded. Returns "on" (static hold),
    "flow" (within the ~3s flow window), or None — following upstream's
    tryHandleDanceCommand: "/dance off" clears immediately, "/dance on"
    flows first and only then freezes into the static rainbow, anything
    else flows and fades. Sub-command matching is case-insensitive, same
    as upstream (parsed.args.trim().toLowerCase()). The flow window is
    reckoned from the history file's mtime, since entries carry no
    timestamp of their own."""
    path = _history_path(cwd)
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", "ignore")
        last = None
        for line in tail.splitlines():
            if "dance" not in line:
                continue
            try:
                content = json.loads(line).get("content") or ""
            except json.JSONDecodeError:
                continue
            content = content.strip()
            if content != "/dance" and not content.startswith("/dance "):
                continue
            sub = content[len("/dance"):].strip().lower()
            if sub == "off":
                last = None
            elif sub == "on":
                last = "on"
            else:
                last = "flow"
        if last in ("on", "flow"):
            age = time.time() - os.path.getmtime(path)
            if age <= _DANCE_FLOW_S + 0.5:
                return "flow"
            if last == "on":
                return "on"
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------
# goal badge and background-task badges
# --------------------------------------------------------------------------

@_memo
def _goal_snapshot(session_dir):
    """The goalSnapshot persisted under state.json's reserved custom key
    "goal" ({goalId, status, turnsUsed, tokensUsed, wallClockMs, budget,
    terminalReason, ...}), or None. The TUI keeps its own copy in memory;
    this on-disk mirror is what a status-line command can read."""
    try:
        with open(os.path.join(session_dir, "state.json"),
                  encoding="utf-8") as f:
            goal = (json.load(f).get("custom") or {}).get("goal")
    except (OSError, json.JSONDecodeError):
        return None
    return goal if isinstance(goal, dict) else None


def _goal_key(goal):
    """Identity of a goal snapshot, mirroring upstream's goalSnapshotKey:
    the footer restarts its wall-clock anchor whenever any of these
    fields changes."""
    if goal is None:
        return None
    budget = goal.get("budget") or {}
    if not isinstance(budget, dict):
        budget = {}
    return "\0".join(str(x) for x in (
        goal.get("goalId"), goal.get("status"),
        goal.get("terminalReason") or "",
        goal.get("turnsUsed"), goal.get("tokensUsed"),
        goal.get("wallClockMs"),
        budget.get("tokenBudget"), budget.get("turnBudget"),
        budget.get("wallClockBudgetMs")))


@_memo
def goal_badge(session_dir, observed_at=None):
    """[goal ● active · 4m · 7 turns], a port of upstream's
    formatGoalBadge. Only live (active/paused/blocked) goals get a badge,
    same as the built-in footer. An active goal's wall clock keeps
    ticking past the persisted snapshot: upstream adds the time since the
    snapshot last changed (goalObservedAtMs); we get the same anchor from
    the per-session cache (observed_at), falling back to the state file's
    mtime when there is no cache to carry it."""
    goal = _goal_snapshot(session_dir)
    if goal is None:
        return None
    status = goal.get("status")
    if status not in ("active", "paused", "blocked"):
        return None
    try:
        turns_used = int(goal.get("turnsUsed", 0) or 0)
    except (TypeError, ValueError):
        turns_used = 0
    budget = goal.get("budget") or {}
    turn_budget = budget.get("turnBudget") if isinstance(budget, dict) \
        else None
    if turn_budget is not None:
        turns = f"{turns_used}/{turn_budget} turns"
    else:
        turns = f"{turns_used} " + ("turn" if turns_used == 1 else "turns")
    try:
        wall_ms = float(goal.get("wallClockMs", 0) or 0)
    except (TypeError, ValueError):
        wall_ms = 0.0
    if status == "active":
        if observed_at is None:
            try:
                observed_at = os.path.getmtime(
                    os.path.join(session_dir, "state.json"))
            except OSError:
                observed_at = time.time()
        wall_ms += max(0.0, (time.time() - observed_at) * 1000)
    secs = int(round(wall_ms / 1000.0))
    if secs < 60:
        elapsed = f"{secs}s"
    elif secs < 3600:
        elapsed = f"{secs // 60}m"
    else:
        elapsed = f"{secs // 3600}h{secs % 3600 // 60}m"
    dot = {"active": "36", "blocked": "33", "paused": "2"}[status]
    return (_paint("[goal ", "2") + _paint("●", dot)
            + _paint(f" {status} · {elapsed} · {turns}]", "2"))


_TASKS_CACHE_TTL = 2.0    # badges may lag a task start/finish by this much


def _tasks_cache_path(session_dir):
    base = os.path.join(kimi_home(), "kimi-usage-cache")
    safe = hashlib.sha256(session_dir.encode("utf-8")).hexdigest()[:16]
    return os.path.join(base, f"tasks-{safe}.json")


def _count_running_tasks(session_dir):
    """(bash, agent) running-task counts with a small TTL file cache.

    Every status-line run is a fresh process, so @_memo alone would still
    open every agents/*/tasks/*.json once per second (142 files on a big
    session). The counts only change when a background task starts or
    finishes, so a 2s staleness is invisible in practice."""
    path = _tasks_cache_path(session_dir)
    try:
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        v = cached["v"]
        if time.time() - cached["t"] < _TASKS_CACHE_TTL:
            return int(v[0]), int(v[1])
    except (OSError, json.JSONDecodeError, KeyError, TypeError,
            ValueError, IndexError):
        pass
    bash_n = agent_n = 0
    for tf in glob.glob(os.path.join(glob.escape(session_dir), "agents",
                                     "*", "tasks", "*.json")):
        name = os.path.basename(tf)
        if name.startswith("bash-"):
            kind = "bash"
        elif name.startswith("agent-"):
            kind = "agent"
        else:
            continue
        try:
            with open(tf, encoding="utf-8") as f:
                if json.load(f).get("status") != "running":
                    continue
        except (OSError, json.JSONDecodeError):
            continue
        if kind == "bash":
            bash_n += 1
        else:
            agent_n += 1
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"t": time.time(), "v": [bash_n, agent_n]}, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return bash_n, agent_n


@_memo
def task_badges(session_dir):
    """[N tasks running] / [N agents running] from the per-agent task
    records (agents/*/tasks/<id>.json). bash-* ids are shell tasks,
    agent-* ids are background sub-agents; only "running" counts."""
    bash_n, agent_n = _count_running_tasks(session_dir)
    badges = []
    if bash_n:
        noun = "task" if bash_n == 1 else "tasks"
        badges.append(_paint(f"[{bash_n} {noun} running]", "36"))
    if agent_n:
        noun = "agent" if agent_n == 1 else "agents"
        badges.append(_paint(f"[{agent_n} {noun} running]", "36"))
    return badges


# --------------------------------------------------------------------------
# git working-tree status (dirty / ahead / behind / diff stats)
# --------------------------------------------------------------------------

_GIT_CACHE_TTL = 15.0   # mirrors upstream STATUS_TTL_MS

_RUN_STARTED = None   # monotonic time this run's main() started


def _over_budget():
    """Whether this run has already spent most of the 300ms the TUI
    allows the status-line command before taskkilling it."""
    return (_RUN_STARTED is not None
            and time.monotonic() - _RUN_STARTED > 0.18)


def _git_cache_path(cwd):
    base = os.path.join(kimi_home(), "kimi-usage-cache")
    safe = hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:16]
    return os.path.join(base, f"git-{safe}.json")


def _probe_git(cwd):
    """One refresh of the git badge data. Two short-timeout spawns;
    any failure yields None and the caller keeps the stale cache."""
    import subprocess
    # hide the console windows git would otherwise flash on Windows
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        st = subprocess.run(
            ["git", "status", "--porcelain=v1", "--branch"],
            cwd=cwd, capture_output=True, text=True, timeout=0.15, **kw)
        if st.returncode != 0:
            return None
        dirty, ahead, behind = False, 0, 0
        for line in st.stdout.splitlines():
            if line.startswith("##"):
                m = re.search(r"ahead (\d+)", line)
                if m:
                    ahead = int(m.group(1))
                m = re.search(r"behind (\d+)", line)
                if m:
                    behind = int(m.group(1))
            elif line:
                dirty = True
        added = deleted = 0
        if dirty:
            ns = subprocess.run(
                ["git", "diff", "--numstat", "HEAD"],
                cwd=cwd, capture_output=True, text=True, timeout=0.15, **kw)
            if ns.returncode == 0:
                for line in ns.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        if parts[0].isdigit():
                            added += int(parts[0])
                        if parts[1].isdigit():
                            deleted += int(parts[1])
        return {"dirty": dirty, "ahead": ahead, "behind": behind,
                "added": added, "deleted": deleted}
    except Exception:
        return None


@_memo
def git_status(cwd):
    """(dirty, ahead, behind, added, deleted) with a TTL file cache, so
    the 300ms command budget only pays for git spawns once per 15s."""
    path = _git_cache_path(cwd)
    cached = None
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        cached = c
        if time.time() - c.get("t", 0) < _GIT_CACHE_TTL:
            return c.get("v")
    except (OSError, json.JSONDecodeError):
        pass
    if _over_budget():
        # The wire parse already ate the 300ms spawn budget; keep the
        # stale badge rather than risk being taskkill'd mid-probe.
        return cached.get("v") if isinstance(cached, dict) else None
    v = _probe_git(cwd)
    if v is None and cached is not None:
        return cached.get("v")  # keep stale data on probe failure
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"t": time.time(), "v": v}, f)
    except OSError:
        pass
    return v


def git_badge(branch, status):
    """branch [+a -d ↑x ↓y], mirroring upstream formatGitBadgeBase."""
    if not status:
        return branch
    parts = []
    if status.get("added") or status.get("deleted"):
        diff = []
        if status.get("added"):
            diff.append(f"+{status['added']}")
        if status.get("deleted"):
            diff.append(f"-{status['deleted']}")
        parts.append(" ".join(diff))
    elif status.get("dirty"):
        parts.append("±")
    sync = ""
    if status.get("ahead"):
        sync += f"↑{status['ahead']}"
    if status.get("behind"):
        sync += f"↓{status['behind']}"
    if sync:
        parts.append(sync)
    return f"{branch} [{' '.join(parts)}]" if parts else branch


# --------------------------------------------------------------------------
# PR badge (gh pr view, detached)
# --------------------------------------------------------------------------

_PR_CACHE_TTL = 60.0   # mirrors upstream PULL_REQUEST_TTL_MS


def _pr_cache_path(cwd):
    base = os.path.join(kimi_home(), "kimi-usage-cache")
    safe = hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:16]
    return os.path.join(base, f"pr-{safe}.json")


def _hyperlink(text, url):
    """OSC 8 terminal hyperlink, mirroring upstream's toTerminalHyperlink
    (an unsafe URL degrades to plain text)."""
    if url.startswith("https://") or url.startswith("http://"):
        return f"\x1b]8;;{url}\x07{text}\x1b]8;;\x07"
    return text


@_memo
def _gh_path():
    return shutil.which("gh")


def _read_pr_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        if isinstance(c, dict) and isinstance(c.get("t"), (int, float)):
            return c
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _write_pr_cache(path, branch, value):
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"t": time.time(), "branch": branch, "v": value}, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _pr_from_gh_output(path):
    """Parse `gh pr view --json number,url` output; None when unusable
    (no PR for the branch, gh failed, or the file is still in flight)."""
    try:
        with open(path, encoding="utf-8") as f:
            pr = json.load(f)
        number = int(pr["number"])
        url = pr["url"]
        if not isinstance(url, str):
            return None
        return {"number": number, "url": url}
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _spawn_pr_lookup(cwd, out):
    """Fire `gh pr view` detached with stdout aimed at `out`; the result
    is adopted by a later run. gh needs a network round trip (upstream
    allows it 5s) — far beyond the 300ms command budget, so it is never
    waited on here. A killed run changes nothing: the detached gh still
    lands its answer for the next refresh."""
    gh = _gh_path()
    if gh is None:
        return
    import subprocess
    kw = ({"creationflags": 0x08000000} if os.name == "nt"
          else {"start_new_session": True})
    env = dict(os.environ, GH_NO_UPDATE_NOTIFIER="1", GH_PROMPT_DISABLED="1")
    try:
        f = open(out, "wb")
        try:
            subprocess.Popen([gh, "pr", "view", "--json", "number,url"],
                             cwd=cwd, stdin=subprocess.DEVNULL, stdout=f,
                             stderr=subprocess.DEVNULL, close_fds=True,
                             env=env, **kw)
        finally:
            f.close()
    except OSError:
        pass


@_memo
def pr_status(cwd, branch):
    """{"number", "url"} of the branch's open PR, or None.

    Like upstream's async PR cache, the stale value keeps rendering until
    the detached lookup lands. The cache timestamp is rewritten before
    spawning, so the once-per-minute refresh rate holds even with several
    runs in flight (a rare overlap just spawns a stray extra gh). A cached
    value is only valid for the branch it was fetched on, and the side
    file's name carries the branch hash so an answer for yesterday's
    branch is never adopted."""
    path = _pr_cache_path(cwd)
    bhash = hashlib.sha256(branch.encode("utf-8")).hexdigest()[:8]
    out = f"{path}.{bhash}.out"
    cached = _read_pr_cache(path)
    if cached is not None and cached.get("branch") != branch:
        cached = None
    value = cached.get("v") if cached else None
    try:
        finished = os.path.getmtime(out)
    except OSError:
        finished = None
    if finished is not None:
        v = _pr_from_gh_output(out)
        # gh writes its one-line JSON as it exits, so parseable means the
        # answer landed; an old unparseable file means the lookup failed
        # (no PR, gh error, killed). A young unparseable file is gh still
        # in flight — leave it and the cache alone.
        done = v is not None or time.time() - finished > 30
        if done:
            if cached is None or finished >= cached.get("t", 0):
                value = v
                _write_pr_cache(path, branch, value)
            try:
                os.unlink(out)   # fails harmlessly while gh holds it open
            except OSError:
                pass
            if cached is None or finished >= cached.get("t", 0):
                return value
    if cached is not None \
            and time.time() - cached.get("t", 0) < _PR_CACHE_TTL:
        return value
    _write_pr_cache(path, branch, value)     # herd guard, see docstring
    _spawn_pr_lookup(cwd, out)
    return value


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
                with_cwd=True, with_git=True, session_dir=None,
                goal_observed=None):
    """Rebuilt on every run (not cached). Reproduces the built-in footer
    line 1 slots in the original order and spacing (two-space
    separated): mode badges (bare words, only when active), goal badge,
    model display_name with thinking-effort suffix (rainbow-painted
    while /dance is on), background task badges, shortened cwd, git
    branch with dirty/ahead/behind stats and PR badge. with_cwd /
    with_git drop those slots for narrow terminals.

    swarm state and effort come from the session wire log, /dance state
    from the input history, goal/task badges from the session's
    state.json and task records, git stats and the PR badge from
    TTL-cached probes (the TUI snapshot omits all of these); only the
    rotating tips are skipped, which upstream never renders next to a
    custom line either. goal_observed is the wall-clock anchor for a
    live goal badge (see goal_badge)."""
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
    if session_dir:
        badge = goal_badge(session_dir, goal_observed)
        if badge:
            parts.append(badge)
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
        dance = dance_state(payload.get("cwd") or "")
        if dance:
            # The TUI throttles status-line runs to ~1/s, so we can't
            # replay upstream's 110ms frames. Stepping the phase by one
            # palette slot per run turns the same refresh cadence into a
            # slow gliding wave instead of a ~9-slot jump each second.
            # The hold freezes at the phase upstream's 3s flow lands on
            # (3000ms / 110ms ≈ 27), same as upstream's settle(hold).
            phase = (int(time.time()) if dance == "flow"
                     else int(_DANCE_FLOW_S * 1000 / 110))
            label = _rainbow_text(label, phase, _dance_palette())
        parts.append(label)
    if session_dir:
        parts.extend(task_badges(session_dir))
    cwd = shorten_cwd(payload.get("cwd") or "") if with_cwd else ""
    if cwd:
        parts.append(_paint(cwd, "2"))
    branch = payload.get("gitBranch") if with_git else None
    if branch:
        workdir = payload.get("cwd") or ""
        status = git_status(workdir)
        slot = _paint(git_badge(branch, status), "2")
        if status is not None:
            # The PR badge belongs to the git slot: painted in the theme's
            # primary color (approximated by cyan here) and hyperlinked,
            # same as upstream's formatFooterGitBadge.
            pr = pr_status(workdir, branch)
            if pr:
                slot += " " + _paint(
                    _hyperlink(f"[PR#{pr['number']}]", pr["url"]), "36")
        parts.append(slot)
    return "  ".join(parts)


# --------------------------------------------------------------------------
# per-session cache (parse cursors + the totals they produced)
# --------------------------------------------------------------------------

_CACHE_VERSION = 11


def _cache_path(session_id):
    base = os.path.join(kimi_home(), "kimi-usage-cache")
    os.makedirs(base, exist_ok=True)
    safe = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return os.path.join(base, f"{safe}.json")


def _load_cache(session_id):
    """Previous run's totals plus the per-file cursors that produced them.

    The cursors are self-validating (a byte offset per wire file), so no
    mtime comparison is needed — which also removes the race the old scheme
    had: it stamped the mtime *after* parsing, so anything appended while
    the parse was running counted as already accounted for and was lost.
    Raw stats are cached rather than the rendered line, because rendering
    depends on the live terminal width (window resize)."""
    try:
        with open(_cache_path(session_id), encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(c, dict) and c.get("v") == _CACHE_VERSION:
        return c
    return None


def _save_cache(session_id, session_total, sub_by_model, swarm_on, effort,
                files, goal_key=None, goal_observed=None):
    path = _cache_path(session_id)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": _CACHE_VERSION,
                       "session_total": session_total,
                       "sub_by_model": sub_by_model, "swarm": swarm_on,
                       "effort": effort, "files": files,
                       "goal_key": goal_key,
                       "goal_seen_at": goal_observed}, f)
        os.replace(tmp, path)   # a run killed at the 300ms cap can't corrupt it
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def main():
    global _RUN_STARTED
    _RUN_STARTED = time.monotonic()
    _debug("invoked")
    if plugin_disabled():
        # Exit non-zero: the TUI treats that as a failure and renders its own
        # footer again, which is exactly what a disabled plugin should leave
        # behind.
        _debug("plugin disabled or removed; restoring the built-in footer")
        restore_builtin_statusline()
        sys.exit(1)
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

    prior = _load_cache(session_id) if session_id else None
    session_total, sub_by_model, swarm_on, effort, files = \
        parse_session(session_dir, prior)

    # Wall-clock anchor for a live goal badge, mirroring upstream's
    # goalObservedAtMs: the moment this goal snapshot was first seen. The
    # per-session cache carries it across runs; without a session id the
    # badge falls back to the state file's mtime (see goal_badge).
    goal_key = _goal_key(_goal_snapshot(session_dir))
    goal_observed = None
    if goal_key is not None and session_id:
        seen = (prior or {}).get("goal_seen_at") \
            if (prior or {}).get("goal_key") == goal_key else None
        goal_observed = seen if isinstance(seen, (int, float)) \
            else time.time()
    # save once when the anchor is missing from an older cache, so the
    # clock keeps accruing from first sight instead of resetting per run
    anchor_unsaved = (goal_key is not None and session_id
                      and (prior or {}).get("goal_key") == goal_key
                      and not isinstance((prior or {}).get("goal_seen_at"),
                                         (int, float)))

    if session_id and (files != (prior or {}).get("files")
                       or goal_key != (prior or {}).get("goal_key")
                       or anchor_unsaved):
        _debug(f"swarm_on={swarm_on} effort={effort!r} "
               f"sub_models={list(sub_by_model)}")
        _save_cache(session_id, session_total, sub_by_model, swarm_on,
                    effort, files, goal_key, goal_observed)

    if effort is None:
        # No effort record in the wire yet (fresh session before the
        # first request): the built-in footer still shows the thinking
        # suffix from the model's configured default_effort.
        effort = model_default_efforts().get(payload.get("model") or "")

    # Rendering happens on every run (cache holds raw stats): the line is
    # fitted to the live terminal width, so a window resize takes effect
    # on the next refresh instead of after the wire files change.
    width = _terminal_width(payload)
    if width:
        # The footer renders inside a one-column gutter on each side
        # (CHROME_GUTTER upstream), so the line only gets width - 2 columns;
        # overshooting means the TUI cuts the tail off with "..." instead of
        # letting our own degradation ladder drop a slot.
        width = max(1, width - 2 * _CHROME_GUTTER)
    line = _fit_line(payload, session_total, sub_by_model,
                     model_display_names(), swarm_on, effort, width,
                     session_dir=session_dir, goal_observed=goal_observed)
    _debug(f"width={width} line={line!r}")
    print(line)


if __name__ == "__main__":
    force_utf8_stdout()
    try:
        main()
    except Exception as e:
        _debug(f"exception: {e}\n{traceback.format_exc()}")
        print("kimi-usage: 错误")
    sys.exit(0)
