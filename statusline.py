#!/usr/bin/env python3
"""clawd-statusline: an animated status line for Claude Code, in three styles.

  dashboard  context, 5-hour and 7-day bars in one column; on the right a panel and Clawd, the mascot
  chips      the same bars, with the model and the effort drawn as chips above them
  campfire   the numbers in night colours, and beside them a night scene where Clawd camps: all of it live data

Try them:
  python statusline.py --demo [STYLE]   animated preview with fake data
  python statusline.py --gallery        every style in fresh / calm / busy / critical states
  python statusline.py --use STYLE      switch the live status line to STYLE
  python statusline.py --selftest       edge-case check, exits 1 on failure

Live mode (no flags) reads Claude Code's status JSON on stdin; --style STYLE picks the style for that run.
Motion needs "refreshInterval" in the statusLine settings, set to the same value as REFRESH below.
"""
import json
import math
import os
import re
import sys
import time
from types import SimpleNamespace as NS

STYLE_FILE = os.path.join(os.path.expanduser("~"), ".claude", "statusline-style")
STATE_DIR = os.path.join(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp", "clawd-statusline")
REFRESH = 2  # seconds per live frame: keep equal to "refreshInterval" in settings.json, or blinks freeze

# Palette tuned for a dark Monokai terminal (#272822); the ramp is Monokai's own accents.
TEXT = (230, 228, 218)
MUTED = (150, 150, 138)
TRACK = (62, 64, 56)
BASE = (52, 54, 48)
INK = (30, 31, 26)
WHITE = (255, 255, 255)
CLAY = (217, 119, 87)
CYAN = (102, 217, 239)
RAMP = [(0, (78, 205, 196)), (40, (166, 226, 46)), (65, (230, 219, 116)), (82, (253, 151, 31)), (100, (249, 38, 114))]
GREEN, YELLOW, ORANGE, RED = (c for _, c in RAMP[1:])
PURPLE = (174, 129, 255)  # Monokai's purple, for effort only, so red keeps meaning "running out"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
HALF = ("▐", "▌")  # chip ends that every font has

ESC = "\x1b["
RST = ESC + "0m"
BOLD = ESC + "1m"
ITALIC = ESC + "3m"
NOBG = ESC + "49m"
ANSI = re.compile(r"(\x1b\[[0-9;]*m)")

WINDOWS = (("5h", "five_hour", 5 * 3600), ("7d", "seven_day", 7 * 86400))


# ---------------------------------------------------------------- helpers

def fg(c): return f"{ESC}38;2;{c[0]};{c[1]};{c[2]}m"
def bg(c): return f"{ESC}48;2;{c[0]};{c[1]};{c[2]}m"
def paint(c, text): return fg(c) + text + RST
def mix(a, b, t): return tuple(round(x + (y - x) * t) for x, y in zip(a, b))
def clamp(v, lo=0.0, hi=100.0): return max(lo, min(hi, v))
def breathe(tick, period): return (1 - math.cos(2 * math.pi * (tick % period) / period)) / 2
def cut(text, n): return text if len(text) <= n else text[:n - 1] + "…"


def ramp(p, stops=RAMP):
    p = clamp(p)
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if p <= p1:
            return mix(c0, c1, (p - p0) / (p1 - p0))
    return stops[-1][1]


def rnd(*key):
    """A repeatable random number in [0, 1) for a key of ints, so a frame always draws the same way."""
    h = 2166136261
    for v in key:
        h = ((h ^ (int(v) & 0xFFFFFFFF)) * 16777619) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 2246822519) & 0xFFFFFFFF
    return (h ^ (h >> 13)) / 2 ** 32


def noise(x, seed):
    """Smooth 1-D value noise in [0, 1): ridgelines that wander instead of jitter."""
    i = math.floor(x)
    f = x - i
    a, b = rnd(seed, i), rnd(seed, i + 1)
    return a + (b - a) * f * f * (3 - 2 * f)


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def tok(n):
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    return f"{n / 1e3:.0f}k" if n >= 1e3 else str(int(n))


def dur(secs):
    secs = max(0, int(secs))
    d, h, m = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m" if m >= 10 else f"{m}:{secs % 60:02d}"  # under 10 min it ticks every second


def vlen(s): return len(ANSI.sub("", s))


def fit(line, cols):
    """Cut a coloured line to cols visible cells so the terminal never wraps it."""
    if vlen(line) <= cols:
        return line
    out, n = [], 0
    for part in ANSI.split(line):
        if part.startswith("\x1b"):
            out.append(part)
            continue
        part = part[:max(0, cols - 1 - n)]
        out.append(part)
        n += len(part)
    return "".join(out) + RST + "…"


def sgr(code, f, b, attr):
    """Apply one SGR escape to the running (fg, bg, attrs) state. None means the terminal's default."""
    v = [int(n) if n else 0 for n in code[2:-1].split(";")]
    i = 0
    while i < len(v):
        n = v[i]
        if n == 0:
            f, b, attr = None, None, ""
        elif n in (1, 3):
            attr = "".join(sorted(set(attr + str(n))))
        elif n in (38, 48) and v[i + 1:i + 2] == [2]:
            c = tuple(v[i + 2:i + 5])
            f, b = (c, b) if n == 38 else (f, c)
            i += 4
        elif n == 39:
            f = None
        elif n == 49:
            b = None
        i += 1
    return f, b, attr


def git_branch(cwd):
    """Read .git/HEAD directly: no git process, so it is cheap enough to run every second."""
    p = os.path.abspath(cwd)
    while True:
        dot = os.path.join(p, ".git")
        try:
            if os.path.isfile(dot):  # worktree or submodule: .git is a pointer file
                with open(dot, encoding="utf-8") as f:
                    gd = f.read().strip().removeprefix("gitdir:").strip()
                dot = gd if os.path.isabs(gd) else os.path.join(p, gd)
            if os.path.isdir(dot):
                with open(os.path.join(dot, "HEAD"), encoding="utf-8") as f:
                    head = f.read().strip()
                return head.removeprefix("ref: refs/heads/") if head.startswith("ref:") else head[:7]
        except OSError:
            return None
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


# ---------------------------------------------------------------- state

def read_state(d, now):
    """Normalise Claude Code's status JSON. Every field may be missing or null."""
    d = d if isinstance(d, dict) else {}

    def g(o, k):
        return o.get(k) if isinstance(o, dict) else None

    model = g(d.get("model"), "display_name") or g(d.get("model"), "id") or "Claude"
    cwd = g(d.get("workspace"), "current_dir") or d.get("cwd") or os.getcwd()
    cw = d.get("context_window")
    size = num(g(cw, "context_window_size")) or 200000
    cu = g(cw, "current_usage")
    used = sum(num(g(cu, k)) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    ctx = num(g(cw, "used_percentage"))
    if ctx is None and used:
        ctx = used * 100 / size
    if ctx is not None and not used:
        used = ctx * size / 100

    win = {}
    for name, key, span in WINDOWS:
        w = g(d.get("rate_limits"), key)
        u, resets = num(g(w, "used_percentage")), num(g(w, "resets_at"))
        if u is None:
            continue
        u = clamp(u)
        left = max(0.0, resets - now) if resets else None
        elapsed = clamp(1 - left / span, 0, 1) if left is not None else None
        proj = eta = None
        if elapsed and elapsed >= 0.05:  # earlier than that the average burn is noise
            proj = u / elapsed
            if proj > 100:  # at the burn rate so far you hit 100% before the reset
                eta = (100 - u) * elapsed * span / u
        win[name] = NS(used=u, left=left, elapsed=elapsed, proj=proj, eta=eta)

    pc = d.get("prompt_cache")
    cache = NS(warm=pc.get("warm") is not False, expires=num(pc.get("expires_at")),
               ttl=3600 if pc.get("ttl") == "1h" else 300) if isinstance(pc, dict) else None
    cost, pr = d.get("cost"), d.get("pr")
    secs = num(g(cost, "total_duration_ms"))
    return NS(model=str(model), effort=g(d.get("effort"), "level"),
              dir=os.path.basename(os.path.normpath(cwd)) or str(cwd), branch=git_branch(cwd),
              ctx=None if ctx is None else clamp(ctx), used=used, size=size, win=win,
              sid=str(d.get("session_id") or ""), session=secs / 1000 if secs else None,
              name=str(d.get("session_name") or ""), usd=num(g(cost, "total_cost_usd")),
              added=num(g(cost, "total_lines_added")) or 0, removed=num(g(cost, "total_lines_removed")) or 0,
              cache=cache, pr=(int(num(g(pr, "number"))), g(pr, "review_state")) if num(g(pr, "number")) else None,
              vim=g(d.get("vim"), "mode"), fast=d.get("fast_mode") is True,
              hist={}, clock=now, wall=now, scale=1, every=1, t=0, cols=200)


def update_hist(h, s, clock):
    """Append a sample whenever the context or the 5-hour usage moves. Returns True when something changed."""
    changed = False
    five = s.win.get("5h")
    for key, val in (("ctx", s.ctx), ("5h", five.used if five else None)):
        seq = h.setdefault(key, [])
        if val is not None and (not seq or abs(seq[-1][1] - val) >= 0.5):
            seq.append([round(clock, 1), round(val, 1)])
            del seq[:-24]
            changed = True
    return changed


def load_hist(s, now):
    if not s.sid:
        return {}
    path = os.path.join(STATE_DIR, re.sub(r"[^\w-]", "", s.sid)[:64] + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            h = json.load(f)
    except (OSError, ValueError):
        h = {}
    # shortcut: per-session files are never cleaned up, they are under 1 KB each. Upgrade when the temp dir complains.
    if update_hist(h, s, now):
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(h, f)
            os.replace(path + ".tmp", path)  # atomic, so a cancelled run never leaves half a file
        except OSError:
            pass
    return h


# ---------------------------------------------------------------- shared pieces

def pct(v, tick, width=4, stops=RAMP):
    """Percentage in its heat colour. Critical values pulse; the width never changes so nothing jitters."""
    text = " " + f"{v:.0f}%".rjust(width)
    if v >= 90 and tick % 2:
        return BOLD + paint(mix(ramp(v, stops), WHITE, 0.4), text)
    return paint(ramp(v, stops), text)


def gauge(p, width, tick, phase=0):
    """Half-cell precision bar from coloured spaces (font-proof). Cells take their colour from
    their position, so a fuller bar reaches further into the hot end. A sheen travels the fill."""
    units = round(clamp(p) / 100 * width * 2)
    sheen = (tick + phase) % (width + 10) - 5
    out = ""
    for i in range(width):
        c = ramp((i + 0.5) * 100 / width)
        if abs(i - sheen) <= 1:
            c = mix(c, WHITE, 0.5 if i == sheen else 0.2)
        fill = units - 2 * i
        if fill >= 2:
            out += bg(c) + " "
        elif fill == 1:
            out += bg(TRACK) + fg(c) + "▌"
        else:
            out += bg(TRACK) + " "
    return out + RST


def rgauge(w, width, tick, phase=0, gap=False):
    """A heat bar laid on the window's runway. The track is the whole window and the blinking ┃ is now.
    Fill left of now is the heat bar with its sheen; fill past now is budget borrowed from later in
    the window, drawn as hot stripes that march; · lights stream toward now through the empty future.
    With gap every cell is ▆ (the lower three quarters), so a quarter row of space opens above the
    bar. A terminal only moves in whole rows, this is the smallest step it has. The playhead keeps
    its full height and sticks up into the gap; half cells and · lights become shades."""
    units = round(w.used / 100 * width * 2)
    now_i = min(width - 1, int(w.elapsed * width))
    sheen = (tick + phase) % (width + 10) - 5
    out = ""
    for i in range(width):
        fill = units - 2 * i
        c = ramp((i + 0.5) * 100 / width)
        if i > now_i:
            c = mix(ORANGE, RED, (i - now_i) / max(1, width - now_i - 1))
            if (i - tick) % 3 == 0:
                c = mix(c, INK, 0.45)
        elif abs(i - sheen) <= 1:
            c = mix(c, WHITE, 0.5 if i == sheen else 0.2)
        if i == now_i:
            if fill >= 1:
                out += bg(c) + fg(INK if tick % 2 else mix(c, INK, 0.5)) + "┃"
            else:
                out += bg(TRACK) + fg(WHITE if tick % 2 else MUTED) + "┃"
        elif gap:
            back = TRACK if i < now_i else BASE
            col = c if fill >= 2 else mix(c, back, 0.5) if fill == 1 else back
            if fill <= 0 and i > now_i and (i + tick) % 3 == 0:
                col = mix(BASE, MUTED, 0.16)  # a whole cell is much louder than a ·, so it is a faint shade
            out += NOBG + fg(col) + "▆"
        elif fill >= 2:
            out += bg(c) + " "
        elif fill == 1:
            out += bg(TRACK if i < now_i else BASE) + fg(c) + "▌"
        elif i < now_i:
            out += bg(TRACK) + " "  # time passed with budget to spare
        else:
            out += bg(BASE) + ((fg(MUTED) + "·") if (i + tick) % 3 == 0 else " ")
    return out + RST


def verdict(w, tick, short=False):
    if w.used >= 100:
        return BOLD + paint(RED, "  ▲ limit hit")
    if w.proj is None:
        return paint(MUTED, "  too early" if short else "  too early to forecast")
    if w.eta is not None:  # dry before the reset; only shout when it is well before
        hot = w.eta < 0.5 * w.left
        return (BOLD if hot and tick % 2 else "") + paint(RED if hot else ORANGE, f"  ▲ {'dry' if short else 'runs dry'} in {dur(w.eta)}")
    return paint(GREEN if w.proj < 85 else YELLOW, f"  → ~{w.proj:.0f}%" + ("" if short else " at reset"))


def chip(text, frac, fill, tick, phase=0, sheen=True, caps=HALF):
    """The label is the bar: the first frac of its cells get the fill colour with dark text."""
    n = len(text)
    k = round(clamp(frac, 0, 1) * n)
    sheen = (tick + phase) % (n + 12) - 6 if sheen else None
    cells = "".join((bg(mix(fill, WHITE, 0.35) if i == sheen else fill) + fg(INK) if i < k else bg(BASE) + fg(TEXT)) + ch
                    for i, ch in enumerate(text))
    return fg(fill if k else BASE) + caps[0] + cells + RST + fg(fill if k >= n else BASE) + caps[1] + RST


def model_chip(s, tick, caps=HALF):
    return chip(f" {s.model} ", 1, mix(CLAY, WHITE, 0.22 * breathe(tick, 6)), tick, sheen=False, caps=caps)


def effort_chip(level, tick, caps=HALF):
    """Effort as a throttle: the chip fills further the harder the model is told to think.
    Fixed width, so changing effort never shifts the line. An unknown level shows full."""
    lv = str(level).lower()
    frac = (EFFORTS.index(lv) + 1) / len(EFFORTS) if lv in EFFORTS else 1
    return chip(f" {lv:^6} ", frac, PURPLE, tick, phase=3, caps=caps)


def who(s, tick, caps=HALF):
    return model_chip(s, tick, caps) + (" " + effort_chip(s.effort, tick, caps) if s.effort else "")


def where(s):
    return paint(TEXT, cut(s.dir, 24)) + (paint(MUTED, " ‹") + paint(CYAN, cut(s.branch, 24)) + paint(MUTED, "›") if s.branch else "")


def meters(s):
    rows = [("ctx", s.ctx, None, f" {tok(s.used)}/{tok(s.size)}")]
    for name, _, _ in WINDOWS:
        w = s.win.get(name)
        rows.append((name, w.used if w else None, w, ""))
    return rows


def severity(name, p, w):
    if name == "ctx":
        return (2, "compact soon") if p >= 85 else (1, "filling up") if p >= 70 else (0, "")
    if p >= 90:
        return 2, "nearly out"
    if w.eta is not None:
        return (2 if w.eta < 1800 else 1), f"dry in {dur(w.eta)}"
    return (1, f"resets {dur(w.left)}" if w.left is not None else "high") if p >= 75 else (0, "")


def worst(s):
    return max((severity(n, p, w)[0] for n, p, w, _ in meters(s) if p is not None), default=0)


def bars(s, tick, chips=False, width=None, pad=""):
    """The model, the effort, the folder and ctx on a head row, then a runway row per window with
    the forecast. chips gaps the window bars so they sit lower under the chips; width and pad let
    the dashboard line its bars up in one column."""
    if s.ctx is None:
        ctx = paint(MUTED, "ctx --")
    else:
        ctx = paint(MUTED, "ctx ") + gauge(s.ctx, (14, 10, 6)[s.t], tick) + pct(s.ctx, tick) + paint(MUTED, f" {tok(s.used)}/{tok(s.size)}" if s.t < 2 else "")
    rows = []
    width = width or (30, 20, 14)[s.t]
    for i, (name, _, _) in enumerate(WINDOWS):
        w = s.win.get(name)
        if not w:
            rows.append(paint(MUTED, f"{name}{pad} --  no rate-limit data yet"))
        elif w.elapsed is None:
            rows.append(paint(MUTED, name + pad + " ") + gauge(w.used, width, tick) + pct(w.used, tick))
        else:
            rows.append(paint(MUTED, name + pad + " ") + rgauge(w, width, tick, phase=i * 7, gap=chips) + pct(w.used, tick)
                        + verdict(w, tick, short=s.t > 0) + paint(MUTED, f" · {'resets ' if s.t == 0 else ''}{dur(w.left)}"))
    return [who(s, tick) + "  " + where(s) + "   " + ctx] + rows


# ---------------------------------------------------------------- dashboard

# Claude Code's own mascot, from the same parts its welcome screen swaps (read out of claude.exe 2.x),
# so check again if Claude Code redraws it. VS Code draws these quadrant glyphs itself only while
# terminal.integrated.gpuAcceleration is on; with it off they come from a fallback font, Claude Code's too.
ARMS = {"down": (" ▐", "", "▝▜", "█▀"), "up": ("▗▟", "▄", " ▜", "█▘"), "one-up": (" ▐", "▄", "▝▜", "█▘")}
EYES = {"open": "▛███▛█", "left": "▟███▟█", "right": "█▟███▟", "closed": "▂███▂█", "shut-right": "█▂███▂"}  # ▂ is a lid
FEET = {"both": " ▝▝   ▝▝ ", "left": " ▝▝      ", "right": "      ▝▝ "}
CLAWD_BG = (0, 0, 0)  # Claude Code's clawd_background: the color of the eyes and the lids


def pose(eyes="open", arms="down", feet="both"):
    l1, r1, l2, r2 = ARMS[arms]
    return ((l1 + EYES[eyes] + r1).ljust(9), l2 + "█████" + r2, FEET[feet])


CLAWD = pose()
POSES = {"idle": CLAWD, "blink": pose("closed"), "left": pose("left"), "right": pose("right"),
         "wave": pose(arms="one-up"), "tap": pose(feet="left"), "alarm": pose(arms="up")}
ACTS = (("blink",), ("left", "left", "right", "right"), ("wave", "idle", "wave"), ("tap", "idle", "tap", "idle"))


def sprite(y, row, color):
    """One row of the mascot. Solid cells are painted as background, which fills the whole cell in any
    terminal; a █ glyph leaves seams when VS Code has GPU acceleration off. In the eye cells the hole
    is drawn instead, a CLAWD_BG quadrant on the body color, and a shut eye is a CLAWD_BG ▂ lid."""
    out = ""
    for x, c in enumerate(row):
        hole = {"▛": "▗", "▟": "▘"}.get(c) if y == 0 and 2 <= x <= 7 else None
        if c == "█":
            out += bg(color) + " "
        elif hole or c == "▂":
            out += bg(color) + fg(CLAWD_BG) + (hole or c)
        else:
            out += NOBG + fg(color) + c
    return out + RST


def clawd(s, tick):
    """The mascot reads the room. While things are fine it sits still, and every 12 frames it does
    one small thing: blinks, looks around, waves, or taps its feet (humming ♪ if nothing is even
    busy). While anything is critical it throws its arms up with a red !. Once a limit is hit it
    naps, z by z, until the reset. Returns 3 rows of 10 cells: a column for ♪ ! z, then the sprite."""
    sev = worst(s)
    fx, color = [" "] * 3, CLAY
    if any(w.used >= 100 for w in s.win.values()):
        name, color = "blink", mix(CLAY, INK, 0.35)
        fx[1 - tick % 2] = paint(MUTED, "zZ"[tick % 2])  # a z, then a bigger Z one row up
    elif sev == 2:
        name = "alarm" if tick % 2 else "idle"
        if tick % 2:
            fx[0] = BOLD + paint(RED, "!")
    else:
        beat, act = tick % 12 - 6, ACTS[tick // 12 % len(ACTS)]
        on = 0 <= beat < len(act)
        name = act[beat] if on else "idle"
        if on and act[0] == "tap" and sev == 0:
            fx[1 - beat % 2] = paint(CYAN, "♪")  # the note drifts up
    return [f + sprite(y, row, color) for y, (f, row) in enumerate(zip(fx, POSES[name]))]


def v_dash(s, tick):
    """The bars in one column on the left, so ctx, 5h and 7d line up and compare at a glance.
    On the right, behind a divider, a panel (who, where, how long) with the mascot beside it.
    The bars stretch to fill a wide terminal; on a narrow one they shrink first, the panel goes last."""
    panel = [who(s, tick), where(s), paint(MUTED, "session ") + paint(TEXT, dur(s.session)) if s.session else ""]
    pw = max(map(vlen, panel))
    div = s.cols - pw - 14  # the divider's column; after it " ", the panel, "  ", the 10-cell mascot
    lo, reserve = (24, 16, 10)[s.t], (43, 31, 31)[s.t]  # reserve = the widest text after a bar
    width = min(48, div - 5 - reserve)  # sized from the reserve, not the text, so bars never twitch
    if width < lo:
        if s.t < 2:
            return ["~" * (s.cols + 1)]  # too wide on purpose: render tries the next, tighter layout
        panel, div, width = None, s.cols, max(lo, min(30, s.cols - 5 - reserve))
    if s.ctx is None:
        left = [paint(MUTED, "ctx --")]
    else:
        left = [paint(MUTED, "ctx ") + gauge(s.ctx, width, tick) + pct(s.ctx, tick) + paint(MUTED, f" {tok(s.used)}/{tok(s.size)}")]
    # labels align left: Claude Code strips spaces at the start of a row, so a row must never start with one
    left += bars(s, tick, width=width, pad=" ")[1:]
    if panel is None:
        return left
    side = [p + " " * (pw - vlen(p) + 2) + c for p, c in zip(panel, clawd(s, tick))]
    return [line + " " * (div - vlen(line)) + paint(mix(TRACK, MUTED, 0.3), "│") + " " + r for line, r in zip(left, side)]


# ---------------------------------------------------------------- campfire

# The numbers sit on the left, on the terminal's own background, in the colours of the night. On the
# right is a window onto the night where Clawd camps, and every part of the scene is live data:
#   stars     fill the sky as the context fills, and clear when it compacts
#   moon      the 7-day budget: full after the reset, it wanes as you spend it
#   woodpile  the 5-hour budget, a log for every 20% left
#   fire      the prompt cache: it roars after each request and burns down to embers as the cache expires
#   weather   clear while all is well, clouds when something needs watching, a storm when it is critical
#   sky       follows your clock, so dusk and dawn colour the horizon
# Once a limit is hit the fire goes out and Clawd sleeps in the tent until the reset.
# The scene is pixels, 2 across and 2 down per cell. A cell shows two of its colours, top over bottom
# (▀) or left beside right (▌): the only block glyphs every monospace font has, so the scene looks the
# same whether or not the terminal draws block glyphs itself. Small things (stars, sparks, snow, rain)
# are text glyphs on top, which the font draws finer than a pixel.

CREAM = (242, 236, 218)
LAV = (162, 156, 208)        # labels
DIM = (112, 108, 156)        # quiet words: separators, "resets", the session's name
GLASS = (44, 42, 84)         # the empty part of a bar
VIOLET = (178, 158, 246)     # effort
TEAL = (126, 196, 204)       # the branch
GOLD = (244, 200, 96)
EMBER = (244, 120, 72)
ALERT = (236, 76, 84)
NIGHT_INK = (16, 14, 34)
MOONLIT = (242, 238, 218)
NRAMP = [(0, (92, 156, 232)), (50, (160, 144, 240)), (75, GOLD), (90, EMBER), (100, ALERT)]  # cool while there is room, firelight as it runs out
SKY = ((0, (8, 9, 26), (34, 26, 72)), (4.5, (8, 9, 26), (34, 26, 72)), (5.5, (12, 14, 40), (76, 50, 106)),
       (6.5, (22, 26, 64), (156, 90, 102)), (7.5, (26, 38, 82), (186, 122, 100)), (9, (16, 26, 58), (52, 68, 116)),
       (16.5, (16, 26, 58), (52, 68, 116)), (17.5, (18, 20, 54), (124, 74, 108)), (18.5, (16, 16, 48), (178, 90, 88)),
       (19.5, (12, 12, 40), (124, 64, 100)), (20.5, (10, 10, 32), (72, 44, 94)), (22, (8, 9, 26), (34, 26, 72)),
       (24, (8, 9, 26), (34, 26, 72)))  # (hour, top of the sky, horizon)
STAR = ((226, 230, 255), (255, 236, 206), (190, 206, 255))
FLAME = ((255, 248, 210), (255, 204, 92), (255, 144, 48), (228, 76, 38))  # core to tip
LOG, BARK, RING = (126, 78, 48), (78, 46, 32), (214, 160, 104)
PUMPKIN = (226, 116, 30)
MALLOW = ((252, 248, 238), (246, 212, 150), (204, 136, 70), (76, 52, 44))  # raw, golden, toasted, on fire
FIRE, AHEAD, FOREST = (6, 7, 12, 13), (4, 5, 10, 11), (2, 3, 8, 9)
CAMPER = {  # Clawd's poses: the columns of its eyes, its left and right arm, its legs. Four eye columns
    "idle": (AHEAD, "down", "down", (3, 5, 10, 12)),  # are two eyes a cell across and half a cell tall,
    "blink": ((), "down", "down", (3, 5, 10, 12)),   # under a row of body, as on Clawd itself; two are
    "look": (FOREST, "down", "down", (3, 5, 10, 12)),  # wide-open eyes, a pixel across and a cell tall
    "wave": (AHEAD, "down", "up", (3, 5, 10, 12)),
    "roast": (FIRE, "down", "down", (3, 5, 10, 12)),
    "tap": (AHEAD, "down", "down", (2, 5, 10, 12)),
    "cheer": (AHEAD, "up", "up", (3, 5, 10, 12)),
    "alarm": ((4, 11), "up", "up", (3, 5, 10, 12)),
    "sleep": ((), "down", "down", (3, 5, 10, 12)),
}
CAMP_ACTS = (("blink",), ("look", "look", "idle", "look"), ("roast",) * 4, ("tap", "idle", "tap", "idle"),
             ("blink", "idle", "blink"), ("cheer", "cheer"))
FONT_SAFE = "▀▌━╸┃·∙‹›…▲♪♦"  # every non-ASCII glyph the campfire prints; Consolas has them all


def tube(p, width, w=None):
    """A thin bar, drawn the way rich draws one: ━ for the part used, ╸ for a half cell, then ━ in the
    night's own colour. The colour comes from the position, cool to warm, so a fuller bar reaches into
    the firelight. Thin bars leave air between the rows. On a window's bar ┃ marks how far through
    the window you are."""
    units = round(clamp(p) / 100 * width * 2)
    now = None if w is None or w.elapsed is None else min(width - 1, int(w.elapsed * width))
    out = ""
    for i in range(width):
        fill = units - 2 * i
        if i == now:
            out += fg(LAV) + "┃"
        elif fill >= 1:
            out += fg(ramp((i + 0.5) * 100 / width, NRAMP)) + "━╸"[fill == 1]
        else:
            out += fg(GLASS) + "━"
    return out + RST


def toast(s):
    """A note for 12 seconds after an event the scene also shows: a compaction, or a 5-hour reset."""
    for key, drop, note in (("ctx", 15, "♦ compacted · the sky clears"), ("5h", 30, "♦ fresh 5 hours · woodpile restocked")):
        seq = s.hist.get(key, [])
        if len(seq) >= 2 and seq[-2][1] - seq[-1][1] >= drop and s.clock - seq[-1][0] <= 12 * s.scale:
            return note


def camp_head(s, room):
    """Who and where: the model, the effort, the folder and branch, then the session's name, or a note,
    if at least 8 cells of it fit in room."""
    out = paint(CLAY, s.model)
    if s.effort:
        out += paint(DIM, " · ") + paint(VIOLET, str(s.effort).lower())
    if s.fast:
        out += paint(DIM, " · ") + paint(GOLD, "fast")
    if s.vim:
        out += paint(DIM, " · ") + paint(LAV, str(s.vim).lower())
    out += "   " + paint(CREAM, cut(s.dir, 24))
    if s.branch:
        out += paint(DIM, " ‹") + paint(TEAL, cut(s.branch, 24)) + paint(DIM, "›")
    if s.pr:
        n, state = s.pr
        out += "  " + paint({"approved": TEAL, "changes_requested": ALERT, "draft": DIM}.get(state, GOLD), f"#{n}")
    note, left = toast(s), room - vlen(out) - 3
    if left < 8:
        return out
    if note:
        return out + "   " + paint(GOLD, cut(note, left))
    return out + ("   " + ITALIC + paint(DIM, cut(s.name, left)) if s.name else "")


def camp_note(w, tick):
    """A window's note: when it resets, or a warning when it runs out first."""
    if w.used >= 100:
        return BOLD + paint(ALERT, "▲ limit hit") + (paint(DIM, f" · {dur(w.left)}") if w.left is not None else "")
    if w.eta is not None:  # dry before the reset; only shout when it is well before
        hot = w.eta < 0.5 * w.left
        return (BOLD if hot and tick % 2 else "") + paint(ALERT if hot else EMBER, f"▲ dry in {dur(w.eta)}")
    return paint(DIM, "resets ") + paint(LAV, dur(w.left)) if w.left is not None else ""


def camp_data(s, tick, bar):
    """The head row, then the context and each window: a label, a bar, the percentage and one note.
    The context's note is the cache, the fire's other half."""
    def row(label, p, w, note):
        if p is None:
            return paint(LAV, f"{label:<4}") + paint(DIM, "--")
        return paint(LAV, f"{label:<4}") + tube(p, bar, w) + pct(p, tick, stops=NRAMP) + "  " + note

    cache = paint(DIM, f"{tok(s.used)} of {tok(s.size)}")
    if s.cache:
        left = s.cache.expires - s.wall if s.cache.expires else None
        if s.cache.warm and (left is None or left > 0):
            cache = paint((240, 170, 104), "cache warm" + (f" {dur(left)}" if left else ""))
        else:
            cache = paint((126, 146, 196), "cache cold")
    rows = [camp_head(s, bar + 30), row("ctx", s.ctx, None, cache)]
    for name, _, _ in WINDOWS:
        w = s.win.get(name)
        rows.append(row(name, w.used if w else None, w, camp_note(w, tick) if w else ""))
    return rows


def v_camp(s, tick):
    """The numbers on the left, the scene in a window pinned to the right edge, 4 rows tall.
    A narrower terminal gets a smaller camp, then the numbers alone."""
    W, t = s.cols, s.t
    lo, least, most = ((20, 64, 100), (14, 40, 63), (8, 0, 0))[t]  # the shortest bar, the smallest and largest scene
    scene = max(0, min(most, W - 32 - lo))
    if t < 2 and scene < least:
        return ["~" * (W + 1)]  # too wide on purpose: render tries the next, tighter layout
    # 30 = the label, the percentage and the widest note; 2 = the gap before the scene
    bar = int(clamp(W - 30 - (scene + 2 if scene else 0), lo, 36))
    rows = [fit(r, bar + 30) for r in camp_data(s, tick, bar)]
    if not scene:
        return rows
    return [r + " " * (W - scene - vlen(r)) + c for r, c in zip(rows, camp_scene(s, tick, scene, t == 1))]


def avg(cs):
    return tuple(round(sum(c[i] for c in cs) / len(cs)) for i in range(3))


def dist(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def block(p):
    """The glyph and the two colours closest to a cell's four pixels (top left, top right, bottom left,
    bottom right): one colour, top over bottom (▀), or left beside right (▌). Returns (glyph, fg, bg)."""
    tl, tr, bl, br = p
    top, bot, left, right = avg((tl, tr)), avg((bl, br)), avg((tl, bl)), avg((tr, br))
    across = dist(tl, top) + dist(tr, top) + dist(bl, bot) + dist(br, bot)
    down = dist(tl, left) + dist(bl, left) + dist(tr, right) + dist(br, right)
    glyph, a, b = ("▀", top, bot) if across <= down else ("▌", left, right)
    return (" ", None, avg(p)) if dist(a, b) < 60 else (glyph, a, b)


class Canvas:
    """A scene of pixels, 2 across and 2 down per cell, with glyph cells laid on top.
    A glyph cell takes the colour of the pixels under it as its background."""

    def __init__(self, cols, rows, fill):
        self.w, self.h, self.cells = cols * 2, rows * 2, {}
        self.sky = [[fill(x, y) for x in range(self.w)] for y in range(self.h)]
        self.px = [row[:] for row in self.sky]

    def dot(self, x, y, c, a=1.0):
        x, y = int(x), int(y)
        if 0 <= x < self.w and 0 <= y < self.h and a > 0:
            self.px[y][x] = c if a >= 1 else mix(self.px[y][x], c, a)

    def under(self, r, c):
        return [self.px[2 * r + i // 2][2 * c + i % 2] for i in range(4)]

    def free(self, r, c):
        """True when cell (r, c) is untouched sky with no glyph beside it: room for a star."""
        return all((r, x) not in self.cells for x in range(c - 2, c + 3)) and \
            self.under(r, c) == [self.sky[2 * r + i // 2][2 * c + i % 2] for i in range(4)]

    def glyph(self, r, c, ch, color, attr=""):
        if 0 <= r < self.h // 2 and 0 <= c < self.w // 2:
            self.cells[r, c] = (ch, color, attr)

    def render(self):
        lines = []
        for r in range(self.h // 2):
            out, cf, cb, ca = [], None, None, ""
            for c in range(self.w // 2):
                if (r, c) in self.cells:
                    (ch, f, a), b = self.cells[r, c], avg(self.under(r, c))
                else:
                    (ch, f, b), a = block(self.under(r, c)), ""
                if a != ca:
                    out.append(RST + (BOLD if a else ""))
                    cf = cb = None
                    ca = a
                if f != cf and ch != " ":
                    out.append(fg(f))
                    cf = f
                if b != cb:
                    out.append(bg(b))
                    cb = b
                out.append(ch)
            lines.append("".join(out) + RST)
        return lines


def sky_at(hour):
    for (h0, t0, b0), (h1, t1, b1) in zip(SKY, SKY[1:]):
        if h0 <= hour <= h1:
            f = (hour - h0) / (h1 - h0)
            return mix(t0, t1, f), mix(b0, b1, f)
    return SKY[0][1], SKY[0][2]


def stars(cv, s, tick, day):
    """Stars are glyphs on free sky, at most one per column and at a fixed spot, so none ever jumps.
    More switch on as the context fills; a compaction clears the sky. Each frame a few twinkle."""
    dens = 0.07 + 0.3 * (s.ctx or 0) / 100
    for x in range(cv.w // 2):
        r = int(rnd(x, 2) * 2)
        if rnd(x, 1) >= dens or not cv.free(r, x):
            continue
        mag, tw = rnd(x, 3), rnd(x, tick, 4)
        glyph = "+" if mag > 0.86 and tw < 0.5 else "∙" if mag > 0.55 else "·"
        lum = (0.45 + 0.55 * mag) * (0.5 if day else 1) * (0.4 if tw < 0.12 else 1)
        cv.glyph(r, x, glyph, mix(avg(cv.under(r, x)), STAR[int(rnd(x, 5) * 3)], lum))


def moon(cv, cx, cy, ry, lit, color):
    """The moon, lit fraction lit (1 full, 0 new) and waning from the right, in a soft halo.
    A pixel is about half as wide as it is tall, so the disc is 2.1 times as many pixels across as down."""
    rx = ry * 2.1
    dark = mix((84, 92, 150), cv.px[0][min(cv.w - 1, int(cx))], 0.45)  # the unlit part: dim, but the disc stays whole
    edge = 2 * lit - 1  # the terminator, from -1 (new) to 1 (full), in half-widths of each row
    for y in range(cv.h):
        for x in range(max(0, int(cx - 3 * rx)), min(cv.w, int(cx + 3 * rx) + 1)):
            d = math.hypot((x + 0.5 - cx) / rx, (y + 0.5 - cy) / ry)
            if 1 <= d < 2:
                cv.dot(x, y, mix(color, (128, 132, 196), 0.5), 0.2 * (0.3 + 0.7 * lit) * (2 - d) ** 2)
            cover = light = 0
            for sy in range(3):
                for sx in range(3):
                    dx, dy = (x + (sx + 0.5) / 3 - cx) / rx, (y + (sy + 0.5) / 3 - cy) / ry
                    if dx * dx + dy * dy <= 1:
                        cover += 1
                        light += dx / math.sqrt(max(1 - dy * dy, 1e-6)) <= edge
            if cover >= 4:
                cv.dot(x, y, mix(dark, color, light / cover))


def peaks(x, seed, every, lo, hi, slope=0.55):
    """A mountain range: a peak in every stretch of `every` pixels, lo to hi tall, slopes falling
    slope pixels per pixel, with a little noise so the ridgeline is rugged, not ruled."""
    k = int(x // every)
    return max(lo + (hi - lo) * rnd(seed, j, 1) - abs(x - (j + rnd(seed, j)) * every) * slope
               for j in (k - 1, k, k + 1)) + 0.7 * noise(x / 2.5, seed + 1)


def ridge(cv, x0, x1, height, color, lit, rim=None, above=9):
    """A silhouette from pixel x0 to x1, height(x) pixels tall, stepping a whole cell at a time so its
    edges are ▀ steps, never slivers one pixel wide. Where it falls to the right, the slope faces the
    moon and takes colour lit; rim, if given, lines the top wherever it is higher than pixel row
    above (so snow caps only the peaks)."""
    for x in range(max(0, x0), min(cv.w, x1)):
        h = round(height(x - x % 2))
        top = cv.h - h
        side = lit if round(height(x - x % 2 + 2)) < h else color
        for y in range(max(0, top), cv.h):
            cv.dot(x, y, rim if rim and y == top < above else side if y < top + 2 else color)


def pine(cv, cx, height, color, rim, snow):
    """A spruce height pixels tall on a trunk at row 7, centred on cx, the middle of a cell, so its tip
    is one cell wide. Its tiers widen downwards, and the right edge, facing the moon, catches the light."""
    top = 7 - height
    for y in range(top, 7):
        k = y - top
        hw = 1 + 0.5 * k - 0.5 * (k > 0 and k % 2 == 0)
        for x in range(int(cx - hw) - 1, int(cx + hw) + 1):
            d = x + 0.5 - cx
            if abs(d) <= hw:
                c = rim if d > hw - 1 else color
                if snow and (k == 0 or k % 2 == 0 and abs(d) > hw - 1):
                    c = mix(c, (230, 238, 255), 0.7)
                cv.dot(x, y, c)
    cv.dot(cx - 1, 7, (34, 24, 22))
    cv.dot(cx, 7, (34, 24, 22))


def sprite_px(cv, x, art, palette, base):
    """Pixel art whose bottom row sits on pixel row base: each string a row, each character a palette key."""
    for dy, row in enumerate(art):
        for dx, ch in enumerate(row):
            if ch in palette:
                cv.dot(x + dx, base - len(art) + 1 + dy, palette[ch])


def fire_heat(s):
    """How hard the fire burns, 0 to 1. After a request the prompt cache is fresh and the fire roars;
    it burns down as the cache's time runs out and is embers once it has expired."""
    c = s.cache
    if not c:
        return 0.8  # no cache data: a steady, friendly fire
    if not c.warm:
        return 0.12
    if c.expires is None:
        return 0.8
    left = c.expires - s.wall
    return 0.12 if left <= 0 else 0.3 + 0.7 * clamp(left / c.ttl, 0, 1)


def logs_left(w):
    return 5 if w is None else math.ceil(5 * (100 - w.used) / 100 - 1e-9)


def campfire(cv, f, heat, tick):
    """A fire centred on the cell from pixel f: logs with glowing embers, flames that dance, sparks that
    fly up, smoke once it burns low."""
    ember = mix((96, 26, 14), (255, 128, 46), clamp(0.25 + 0.75 * heat * (0.6 + 0.4 * rnd(tick, 11)), 0, 1))
    for dx, c in enumerate((BARK, LOG, LOG, ember, ember, LOG, LOG, BARK)):
        cv.dot(f - 3 + dx, 7, c)
    sway = (-1, 0, 0, 1)[int(rnd(tick, 21) * 4)]  # the tips lean with the draught
    for dx, base in ((-3, 0.6), (-2, 1.6), (-1, 2.8), (0, 3.6), (1, 3.6), (2, 2.8), (3, 1.6), (4, 0.6)):
        h = heat * base * (0.8 + 0.35 * rnd(tick, dx, 22))
        for j in range(math.ceil(h)):
            u = (j + 0.5) / h
            c = FLAME[0] if u < 0.4 and dx in (0, 1) else FLAME[1] if u < 0.62 and -2 < dx < 3 else FLAME[2] if u < 0.85 else FLAME[3]
            cv.dot(f + dx + (sway if u > 0.7 else 0), 6 - j, c, min(1, h - j + 0.3))
    if heat > 0.35:
        for k in range(2):  # sparks, each alive for two frames
            age, gen = (tick + k) % 2, (tick + k) // 2
            c = f // 2 + (-2, -1, 1, 2)[int(rnd(k, gen, 23) * 4)] + age * (1 if rnd(k, gen, 24) > 0.5 else -1)
            cv.glyph(1 - age, c, "∙·"[age], ((255, 226, 140), (232, 120, 60))[age])
    if heat < 0.45:
        for age in range(5):
            cv.dot(f + (age + tick) % 3 // 2 + age // 3, 5 - age, (132, 130, 150), 0.45 * (1 - age / 5))


def glow(cv, f, heat, tick):
    """Firelight: everything near the fire takes its colour, and it flickers with the flames."""
    a0 = 0.42 * heat * (0.75 + 0.5 * rnd(tick, 11))
    for y in range(cv.h):
        for x in range(f - 20, f + 22):
            d = math.hypot((x - f - 0.5) / 20, (y - 6) / 5)
            if d < 1:
                cv.dot(x, y, (255, 148, 70), a0 * (1 - d) ** 2)


def camper(cv, x, name, body):
    """Clawd in pixels, 16 across from x (even, so every part fills whole half cells): a body 12 by 4,
    arms half a cell tall (a raised one a cell, up past its head), four legs on the ground and two
    eyes. Poses move only those."""
    eyes, arms, legs = CAMPER[name][0], CAMPER[name][1:3], CAMPER[name][3]
    rows = (2, 3) if len(eyes) == 2 else (3,)
    for cell in [k for k in cv.cells if x // 2 <= k[1] < x // 2 + 8]:
        del cv.cells[cell]  # rain, snow and sparks pass behind Clawd, never over its face
    for y in range(2, 6):
        for dx in range(2, 14):
            cv.dot(x + dx, y, CLAWD_BG if y in rows and dx in eyes else body)
    for side, arm in zip((0, 14), arms):
        up = side + (side == 0)  # a raised arm is the half of the cell beside the body
        for px, py in (((side, 4), (side + 1, 4)) if arm == "down" else ((up, y) for y in range(4))):
            cv.dot(x + px, py, body)
    for dx in legs:
        cv.dot(x + dx, 6, body)
        cv.dot(x + dx, 7, body)


def camp_scene(s, tick, cols, compact):
    """The scene, cols wide and 4 rows (8 pixels) tall; things stand on pixel row 7. It is laid out
    from the right: the moon over the lake, then the camp, then pines and mountains as far as there is room."""
    W = 2 * cols
    loc = time.localtime(s.wall)
    hour = loc.tm_hour + loc.tm_min / 60
    top, hor = sky_at(hour)
    cv = Canvas(cols, 4, lambda x, y: mix(top, mix(top, hor, 0.3 + 0.7 * (x / W) ** 1.5), (y / 7) ** 1.6))
    md = (loc.tm_mon, loc.tm_mday)
    halloween, winter = (10, 24) <= md <= (11, 1), (12, 1) <= md <= (12, 26)
    newyear = (md == (12, 31) and loc.tm_hour >= 21) or (md == (1, 1) and loc.tm_hour < 2)
    fireflies = loc.tm_mon in (6, 7, 8) and (loc.tm_hour >= 21 or loc.tm_hour < 4)
    asleep = any(w.used >= 100 for w in s.win.values())
    sev = worst(s)
    storm = sev == 2 and not asleep
    heat = 0.0 if asleep else fire_heat(s) * ((0.45 + 0.4 * rnd(tick, 12)) if storm else 1)  # rain on the fire
    flick = rnd(tick, 11)
    # pixel columns; each thing starts on a cell's left pixel, or centres on a cell's middle (tent, pines)
    if compact:
        lake, shore, wood, f, bush, pump, tent = W - 20, W - 23, W - 32, W - 42, None, None, None
    else:
        lake, shore, wood, f, bush, pump, tent = W - 30, W - 35, W - 66, W - 76, W - 50, W - 58, W - 111
    cl = f - 22  # Clawd's left edge, close enough to the fire to roast a marshmallow
    mx = (lake + W) / 2  # the moon hangs over the lake
    seven = s.win.get("7d")
    lit = 1 if seven is None else 1 - seven.used / 100
    moonc = (255, 214, 156) if halloween else MOONLIT  # a harvest moon at Halloween

    far = mix(mix(top, hor, 0.7), (52, 58, 112), 0.55)
    near = mix(top, (14, 18, 30), 0.6)
    grass = mix(near, (52, 92, 78), 0.45) if not winter else mix(near, (200, 212, 236), 0.6)
    tree = (16, 36, 44)  # dark teal: darker than the mountains, lighter than the night above them
    water = mix(mix(top, hor, 0.4), (6, 10, 30), 0.35)

    moon(cv, mx, 2, 2, lit, moonc)
    if sev or storm:  # weather: clouds drift in when something needs watching, a storm when it is critical
        for k in range(4 if storm else 2):
            cx = (rnd(k, 61) * W + 2 * tick) % W
            rx, cy = 8 + 8 * rnd(k, 62), 0.6 + 1.6 * rnd(k, 63)
            for x in range(int(cx - rx - 1), int(cx + rx + 2)):
                for y in range(4):
                    d = ((x + 0.5 - cx) / rx) ** 2 + ((y + 0.5 - cy) / 1.3) ** 2
                    c = (34, 34, 56) if storm else mix((70, 70, 110), MOONLIT, 0.14 if y < cy else 0)
                    cv.dot(x, y, c, clamp((1 - d) * 1.8, 0, 1) * (0.9 if storm else 0.66))
    if not asleep and tick % 37 == 5:  # a shooting star now and then, and Clawd waves at it
        sx0 = 10 + int(rnd(tick // 37, 71) * max(1, W - 30))
        for i in range(10):
            cv.dot(sx0 - i, i // 4, (255, 255, 236), 1 - i / 10)

    valley = lambda x: 1 - math.exp(-((x - mx) / 9) ** 2)  # the mountains part where the moon rises over the lake
    ridge(cv, 0, W, lambda x: 2 + max(0, peaks(x, 101, 14, 1.0, 3.8)) * valley(x),
          far, mix(far, MOONLIT, 0.08), mix(far, (230, 238, 255), 0.7) if winter else None, 4)
    camp0, camp1 = (tent - 10 if tent else cl - 4), (bush + 12 if bush else wood + 8)

    def hills(x):
        d = max(camp0 - x, x - camp1, 0) * clamp((lake - x) / 6, 0, 1)  # flat at the camp, down to the shore
        return 2 + 1.8 * noise(x / 12, 103) * clamp(d / 8, 0, 1)

    ridge(cv, 0, lake, hills, near, near, grass)
    for x in range(lake, W):  # the lake: the horizon's glow on the far water, the moon in strokes that shimmer
        k = x // 2  # a stroke is a whole cell wide, so the water reads as ▀ dashes, not a barcode
        for y in (6, 7):
            c = mix(water, mix(hor, (120, 120, 190), 0.3), (0.6, 0.15)[y - 6])
            near = 1 - abs(2 * k + 1 - mx) / (5, 8)[y - 6]
            if near > 0 and rnd(k, y, tick, 31) < 0.7:
                c = mix(c, moonc, (0.25 + 0.6 * lit) * (0.9, 0.6)[y - 6] * min(1, 0.3 + near))
            elif rnd(k, y, tick // 2, 32) < 0.06:
                c = mix(c, (150, 160, 214), 0.3)  # a ripple catches the light
            cv.dot(x, y, c)
    rim = mix(tree, (120, 150, 170), 0.18)  # moonlight on the needles
    for k, x in enumerate(range(5, (tent - 12 if tent else cl - 6), 9)):  # a forest up to the camp
        if rnd(k, 85) < 0.6:
            pine(cv, (x + int(rnd(k, 86) * 3)) | 1, 4 + int(rnd(k, 87) * 3), tree, rim, winter)
    pine(cv, shore, 6, tree, rim, winter)
    if tent is not None:  # a tent, its door open unless Clawd is asleep in it, and a flag that flutters
        for y in range(1, 8):
            hw = 1 + (y - 1) * 1.15
            for x in range(int(tent - hw) - 1, int(tent + hw) + 1):
                d = x + 0.5 - tent
                if abs(d) <= hw:
                    door = y >= 4 and abs(d) <= (y - 4) * 0.6 + 0.5
                    cv.dot(x, y, ((70, 128, 120) if asleep else (30, 18, 24)) if door else
                           (46, 96, 104) if d < 0 else (86, 152, 138))
        cv.dot(tent - 1, 0, (110, 96, 84))
        cv.dot(tent, 0, (110, 96, 84))
        for x, y in ((tent + 1, 0), (tent + 2, 0)) if tick % 2 else ((tent + 1, 0), (tent + 1, 1)):
            cv.dot(x, y, CLAY)
    for k, (dx, y) in enumerate(((0, 6), (2, 6), (4, 6), (2, 4), (4, 4))[:logs_left(s.win.get("5h"))]):
        for i in (0, 1):  # the woodpile, log ends over bark; the top logs go first
            cv.dot(wood + dx + i, y, mix(RING, BARK, 0.3 * (k % 2)))
            cv.dot(wood + dx + i, y + 1, BARK)
    glow(cv, f, heat, tick)
    campfire(cv, f, heat, tick)
    if halloween and pump is not None:  # a jack-o'-lantern with a candle inside
        face = mix((255, 226, 120), (232, 130, 40), 0.5 * flick)
        sprite_px(cv, pump, ("..gg..", "PPPPPP", "ffPPff", "PPffPP"), {"g": (96, 128, 60), "P": PUMPKIN, "f": face}, 7)
    if bush is not None:  # something in the bush, watching the fire. It blinks.
        eye = ((255, 96, 40) if halloween else (255, 206, 84)) if rnd(tick, 33) > 0.15 else tree
        sprite_px(cv, bush, ("..ssssss..", "##ee##ee##", "##########"),
                  {"s": mix(tree, (230, 238, 255), 0.6) if winter else tree, "#": tree, "e": eye}, 7)

    if halloween:  # bats cross the moon
        for k in range(2):
            cv.glyph(k, int(mx // 2) + (tick * 2 + k * 9) % 18 - 9, "v", (30, 18, 36))
    if winter:  # snow
        for k in range(cols // 5):
            cv.glyph((tick + int(rnd(k, 42) * 4)) % 4, int(rnd(k, 41) * cols) + (tick // 2 + k) % 2, "∙·"[k % 3 == 0], (232, 238, 255))
    if fireflies:
        for k in range(5):
            if rnd(k, tick, 83) < 0.5:
                cv.glyph(1 + int(rnd(k, tick // 3, 82) * 3), int(rnd(k, 81) * cols), "·", (206, 255, 120))
    if newyear:  # fireworks: a rocket, a burst, sparks that spread and fade
        for k in range(2):
            age, gen = (tick + k * 2) % 4, (tick + k * 2) // 4
            c0, hue = int(rnd(k, gen, 91) * cols), ((255, 92, 120), (120, 220, 255), (255, 220, 90), (180, 130, 255))[gen % 4]
            for r, dc, ch in (((1, 0, "·"),), ((0, 0, "*"),), ((0, -2, "∙"), (0, 2, "∙"), (1, -1, "·"), (1, 1, "·")),
                              ((1, -3, "·"), (1, 3, "·")))[age]:
                cv.glyph(r, c0 + dc, ch, mix(hue, top, 0.4 if age == 3 else 0))
    if storm:  # rain, and now and then lightning: the sky flashes and a bolt forks down
        for c in range(cols):
            if rnd(c, tick, 51) < 0.14:
                cv.glyph(int(rnd(c, tick, 52) * 4), c, "/", (132, 152, 212))
        if tick % 7 == 3:
            for row in cv.px:
                row[:] = [mix(c, (206, 212, 255), 0.2) for c in row]
            bc = int(rnd(tick, 54) * cols)
            for r in range(3):
                step = (-1, 0, 1)[int(rnd(tick, r, 55) * 3)]
                cv.glyph(r, bc, "/|\\"[step + 1], (246, 246, 255), "1")
                bc += step

    if asleep:  # Clawd sleeps in the tent with z's floating out, or by the cold fire on a small layout
        if tent is None:
            camper(cv, cl, "sleep", mix(CLAY, NIGHT_INK, 0.3))
        zc = (tent // 2 + 2) if tent is not None else cl // 2 + 8
        cv.glyph(1 - tick % 2, zc + tick % 2, "zZ"[tick % 2], LAV)
    else:
        fx = mallow = None
        if storm:
            name = "alarm" if tick % 2 else "idle"
            if tick % 2:
                fx = (0, cl // 2 + 4, "!", ALERT, "1")
        elif tick % 37 == 5:
            name = "wave"
        else:
            beat, act = tick % 10 - 4, CAMP_ACTS[tick // 10 % len(CAMP_ACTS)]
            on = 0 <= beat < len(act)
            name = act[beat] if on else "idle"
            if name == "roast":  # a marshmallow on a stick, golden, toasted, then on fire
                name, mallow = ("roast", MALLOW[beat]) if heat > 0.4 else ("idle", None)
            if on and act[0] == "tap" and sev == 0:
                fx = (1 - beat % 2, cl // 2 - 1, "♪", TEAL, "")
        camper(cv, cl, name, mix(CLAY, (255, 186, 116), 0.05 + 0.2 * heat * flick))  # firelight on Clawd
        if mallow:
            for x, y in ((16, 4), (17, 4), (18, 3), (19, 3)):
                cv.dot(cl + x, y, (122, 88, 62))
            for x in (20, 21):
                cv.dot(cl + x, 3, mallow)
                if mallow == MALLOW[-1]:
                    cv.dot(cl + x, 2, FLAME[1])
        if fx:
            cv.glyph(*fx)
    stars(cv, s, tick, 9 <= hour < 16.5)
    return cv.render()


# ---------------------------------------------------------------- dispatch

STYLES = {"dashboard": v_dash, "chips": lambda s, tick: bars(s, tick, chips=True), "campfire": v_camp}


def render(name, s, cols):
    """Try full, then tighter layouts (s.t = 0, 1, 2: shorter bars, fewer words) before cutting anything."""
    tick, s.cols = int(s.clock / s.scale / s.every), cols  # one tick per frame, so tick % 2 alternates
    for s.t in (0, 1, 2):
        lines = STYLES[name](s, tick)
        if all(vlen(line) <= cols for line in lines):
            break
    return [fit(line, cols) for line in lines]


def chosen():
    try:
        with open(STYLE_FILE, "rb") as f:  # PowerShell's `echo x > file` writes UTF-16, so strip BOM and NULs
            v = f.read().decode("utf-8", "ignore").replace("\x00", "").replace("\ufeff", "").strip().lower()
    except OSError:
        v = ""
    return v if v in STYLES else "dashboard"


def live(name):
    now = time.time()
    d = {}
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        d = json.loads(raw) if raw.strip() else {}
        if os.environ.get("STATUSLINE_FAKE"):  # see a full status line in a fresh session: STATUSLINE_FAKE=busy
            d = {**d, **{k: v for k, v in scenario(os.environ["STATUSLINE_FAKE"], now).items() if k != "workspace"}}
        s = read_state(d, now)
        s.every = REFRESH
        if name == "campfire":
            s.hist = load_hist(s, now)
        cols = (int(os.environ.get("COLUMNS") or 0) or 200) - 4
        print("\n".join(render(name, s, max(20, cols))))
    except Exception as e:  # a status line must never go blank: fall back to plain text and log
        model = d.get("model") if isinstance(d, dict) else None
        model = model.get("display_name", "Claude") if isinstance(model, dict) else "Claude"
        print(f"{model} | statusline error: {type(e).__name__} (see {STATE_DIR}/error.log)")
        try:
            import traceback
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(os.path.join(STATE_DIR, "error.log"), "a", encoding="utf-8") as f:
                f.write(time.ctime() + "\n" + traceback.format_exc() + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------- demo / gallery / selftest

class Sim:
    """Fake session. 1 real second = 6 simulated minutes, alternating calm and heavy phases,
    so every style passes through calm, busy, critical, a compaction and a 5h reset.
    Every 20 seconds there are 8 idle ones, so the cache cools and the fire burns down."""
    SCALE = 360

    def __init__(self):
        import random
        self.rng = random.Random(7)
        self.t, self.ctx, self.five, self.seven = 0, 12.0, 4.0, 62.0
        self.five_left, self.seven_left, self.cache_left = 4.6 * 3600, 2.9 * 86400, 3600.0

    def step(self):
        self.t += 1
        heavy = (self.t // 20) % 2 == 1
        if self.t % 20 < 12 and self.t % 2 == 0:  # a turn every other second, then a quiet spell
            self.ctx += self.rng.uniform(2, 8 if heavy else 4)
            self.cache_left = 3600.0
            if self.ctx > 92:
                self.ctx = self.rng.uniform(14, 22)  # auto-compact
        elif self.t % 20 >= 12:
            self.cache_left = max(0.0, self.cache_left - 480)
        self.five = min(100.0, self.five + self.rng.uniform(0.8, 4.8 if heavy else 1.4))
        self.five_left -= self.SCALE
        if self.five_left <= 0:
            self.five, self.five_left = 0.0, 5 * 3600.0
        self.seven = min(100.0, self.seven + self.rng.uniform(0.05, 0.35))
        self.seven_left -= self.SCALE
        if self.seven_left <= 0 or self.seven >= 100:
            self.seven, self.seven_left = 5.0, 7 * 86400.0

    def payload(self, now):
        return {"model": {"display_name": "Opus 5.5"}, "effort": {"level": "high"}, "session_id": "demo",
                "session_name": "night shift", "workspace": {"current_dir": os.getcwd()},
                "cost": {"total_duration_ms": self.t * self.SCALE * 1000, "total_cost_usd": self.t * 0.031,
                         "total_lines_added": self.t * 7, "total_lines_removed": self.t * 2},
                "prompt_cache": {"warm": self.cache_left > 0, "ttl": "1h", "expires_at": now + self.cache_left},
                "context_window": {"context_window_size": 200000, "used_percentage": self.ctx,
                                   "current_usage": {"input_tokens": self.ctx * 2000, "output_tokens": 900,
                                                     "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}},
                "rate_limits": {"five_hour": {"used_percentage": self.five, "resets_at": now + self.five_left},
                                "seven_day": {"used_percentage": self.seven, "resets_at": now + self.seven_left}}}


def read_key():
    try:
        import msvcrt
    except ImportError:
        return None
    if not msvcrt.kbhit():
        return None
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        return {"K": "left", "M": "right"}.get(msvcrt.getwch())
    return ch


def demo(name):
    import shutil
    os.system("")  # turns on ANSI handling in classic Windows consoles
    names, sim, hist = list(STYLES), Sim(), {}
    i, paused, prev, next_tick, frame, note = names.index(name), False, 0, 0.0, 0, ""
    sys.stdout.write("\x1b[?25l")
    try:
        while True:
            now = time.time()
            k = read_key()
            redraw = k is not None
            if k in ("q", "Q", "\x1b", "\x03"):
                break
            if k in ("right", "d", "n"):
                i = (i + 1) % len(names)
            elif k in ("left", "a", "p"):
                i = (i - 1) % len(names)
            elif k and k.isdigit() and 1 <= int(k) <= len(names):
                i = int(k) - 1
            elif k == " ":
                paused = not paused
            elif k == "\r":
                use(names[i])
                note = f"  live status line now uses {names[i]}"
            if now >= next_tick:
                if not paused:
                    sim.step()
                frame += 1
                next_tick, redraw = now + 1, True
            if redraw:
                cols = shutil.get_terminal_size().columns - 1
                s = read_state(sim.payload(now), now)
                s.clock, s.scale = frame * Sim.SCALE, Sim.SCALE  # animation keeps running while the sim is paused
                update_hist(hist, s, s.clock)
                s.hist = hist
                five, seven = s.win["5h"], s.win["7d"]
                lines = [paint(MUTED, f"clawd-statusline  {names[i]}   ←/→ switch  1-{len(names)} jump  Enter use live  space pause  q quit") + paint(GREEN, note), ""]
                lines += render(names[i], s, cols)
                lines += ["", paint(TRACK, f"sim  ctx {s.ctx:.0f}%  5h {five.used:.0f}% resets {dur(five.left)}  7d {seven.used:.0f}%"
                                           f"  1s = 6 sim minutes{'  [paused]' if paused else ''}")]
                lines = [fit(line, cols) for line in lines]
                sys.stdout.write((f"\x1b[{prev}F\x1b[J" if prev else "") + "\n".join(lines) + "\n")
                sys.stdout.flush()
                prev = len(lines)
            time.sleep(0.04)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?25h" + RST + "\n")


def scenario(name, now):
    """Fixed payloads for the gallery, the snapshots and the selftest."""
    def rl(five, five_left, seven, seven_left):
        return {"five_hour": {"used_percentage": five, "resets_at": now + five_left},
                "seven_day": {"used_percentage": seven, "resets_at": now + seven_left}}
    base = {"model": {"display_name": "Opus 5.5"}, "effort": {"level": "high"}, "session_id": "", "session_name": "night shift",
            "workspace": {"current_dir": os.getcwd()}, "prompt_cache": {"warm": True, "ttl": "1h", "expires_at": now + 2900},
            "cost": {"total_duration_ms": 4_300_000, "total_cost_usd": 3.42, "total_lines_added": 156, "total_lines_removed": 23}}
    ctx = lambda p: {"context_window_size": 200000, "used_percentage": p,
                     "current_usage": {"input_tokens": p * 2000, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
    return {
        "fresh": {**base, "context_window": {"context_window_size": 200000, "used_percentage": None, "current_usage": None}},
        "calm": {**base, "context_window": ctx(23), "rate_limits": rl(8, 3.85 * 3600, 31, 4.2 * 86400)},
        "busy": {**base, "context_window": ctx(61), "rate_limits": rl(52, 2.15 * 3600, 64, 2.8 * 86400)},
        "critical": {**base, "context_window": ctx(91), "rate_limits": rl(83, 2.0 * 3600, 93, 0.75 * 86400)},
        "asleep": {**base, "context_window": ctx(48), "prompt_cache": {"warm": False},
                   "rate_limits": rl(100, 1.6 * 3600, 71, 1.9 * 86400)},
    }[name]


def gallery():
    os.system("")
    now = time.time()
    for name in STYLES:
        print(BOLD + paint(TEXT, f"\n{name}"))
        for case in ("fresh", "calm", "busy", "critical", "asleep"):
            s = read_state(scenario(case, now), now)
            print(paint(TRACK, f"  {case}"))
            for line in render(name, s, 160 if name != "campfire" else 180):
                print("    " + line)


def selftest():
    now = time.time()
    payloads = [{}, None, "junk", {"model": None, "context_window": None, "rate_limits": None},
                {"context_window": {"used_percentage": None, "current_usage": None}},
                {"context_window": {"used_percentage": "53", "context_window_size": 1_000_000}},
                {"rate_limits": {"five_hour": {"used_percentage": 140, "resets_at": now - 50}}},
                {"rate_limits": {"five_hour": {"used_percentage": 40}, "seven_day": {"used_percentage": 99, "resets_at": "x"}}},
                {"rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": now + 60}}},
                {"workspace": {"current_dir": "C:\\"}, "model": {"id": "claude-opus-5-5"}},
                {"effort": {"level": "some-future-level"}}, {"effort": {"level": None}}, {"effort": "max"},
                {"prompt_cache": {"warm": True, "expires_at": "soon"}, "pr": {"number": "x"}, "cost": {"total_cost_usd": "free"}},
                {"prompt_cache": [], "pr": {"number": 7, "review_state": "approved"}, "vim": {"mode": "INSERT"}, "fast_mode": True,
                 "session_name": "a very long session name that will not fit anywhere at all" * 2}]
    payloads += [scenario(n, now) for n in ("fresh", "calm", "busy", "critical", "asleep")]
    fails = 0
    for name in STYLES:
        for n, d in enumerate(payloads):
            for cols in (230, 200, 120, 80, 30):
                for tick in range(0, 40, 3):
                    try:
                        s = read_state(d, now)
                        s.clock = now + tick * REFRESH
                        lines = render(name, s, cols)
                        assert lines and all(isinstance(l, str) and vlen(l) <= cols for l in lines), "too wide"
                        # Claude Code strips leading spaces per row, which would shift that row left
                        assert not any(ANSI.sub("", l)[:1].isspace() for l in lines), "a row starts with a space"
                        if name == "campfire":  # 4 rows; with room for the scene, it is pinned to the right edge
                            assert len(lines) == 4 and (cols < 86 or {vlen(l) for l in lines} == {cols}), "scene off the right edge"
                            # a glyph the terminal's font lacks shows as a ? box
                            assert {c for l in lines for c in ANSI.sub("", l) if ord(c) > 127} <= set(FONT_SAFE), "a glyph outside FONT_SAFE"
                        if cols >= 80:  # every number we have must survive a narrow terminal
                            text = ANSI.sub("", "\n".join(lines))
                            have = [m for m, v in (("ctx", s.ctx), ("5h", s.win.get("5h")), ("7d", s.win.get("7d"))) if v is not None]
                            assert all(re.search(m + r"[^\d\n]*\d", text, re.I) for m in have), "a metric was cut off"
                    except Exception as e:
                        fails += 1
                        print(f"FAIL {name} payload#{n} cols={cols} tick={tick}: {type(e).__name__}: {e}")
    s = read_state(scenario("critical", now), now)
    oct28 = time.mktime((2026, 10, 28, 21, 0, 0, 0, 0, -1))
    camp_at = lambda case, wall, tick=0: (lambda st: (setattr(st, "clock", tick), "\n".join(render("campfire", st, 200)))[1])(read_state(scenario(case, wall), wall))
    checks = [(dur(4500), "1h15m"), (dur(2 * 86400 + 20 * 3600 + 59), "2d20h"), (dur(272), "4:32"), (tok(108000), "108k"),
              (tok(1_000_000), "1.0M"), (round(s.win["5h"].eta), round((100 - 83) * 0.6 * 18000 / 83)),
              (vlen(fit(paint(RED, "x" * 50), 10)), 10),
              (len({vlen(effort_chip(lv, 0)) for lv in EFFORTS}), 1),  # changing effort never shifts the line
              # the gap bar keeps its width, and only the playhead fills its whole row
              ((lambda g: (vlen(g), g.count(ESC + "48;2")))(rgauge(s.win["5h"], 20, 0, gap=True)), (20, 1)),
              ({vlen(l) for l in render("dashboard", read_state(scenario("calm", now), now), 120)}, {120}),  # divider and panel line up
              ({len(r) for p in POSES.values() for r in p}, {9}),  # every pose is the same size
              # the mascot reads the room: arms up and a ! when critical, z's when a limit is hit
              (clawd(s, 1)[0], BOLD + paint(RED, "!") + sprite(0, POSES["alarm"][0], CLAY)),
              # solid cells become background and each eye a hole, so no seams show between glyphs
              (ANSI.sub("", sprite(0, CLAWD[0], CLAY)), " ▐▗   ▗  "),
              ("Z" in ANSI.sub("", "".join(clawd(read_state({"rate_limits": {"five_hour": {"used_percentage": 100}}}, now), 1))), True),
              ([effort_chip(lv, 0).endswith(fg(PURPLE) + "▌" + RST) for lv in ("medium", "max", "auto")], [False, True, True]),
              # campfire: the woodpile is the 5-hour budget, a log per 20% left
              ([logs_left(NS(used=u)) for u in (0, 10, 21, 60, 99, 100)] + [logs_left(None)], [5, 5, 4, 2, 1, 0, 5]),
              # the fire is the prompt cache: roaring when fresh, embers once expired or cold
              ([round(fire_heat(NS(cache=c, wall=now)), 2) for c in (None, NS(warm=True, expires=now + 3600, ttl=3600),
                NS(warm=True, expires=now - 1, ttl=3600), NS(warm=False, expires=None, ttl=300))], [0.8, 1.0, 0.12, 0.12]),
              # the bars are themed: none of the daytime greys survive on the night sky
              (any(fg(c) in camp_at("busy", now) or bg(c) in camp_at("busy", now) for c in (MUTED, TRACK, BASE)), False),
              # Clawd (spotted by its eyes) sits by the fire, unless a limit is hit: then z's drift out of the tent
              ([any(e(CLAWD_BG) in camp_at(c, now, 1) for e in (fg, bg)) for c in ("calm", "asleep")], [True, False]),
              ("Z" in ANSI.sub("", camp_at("asleep", now, 1)), True),
              # the calendar: a jack-o'-lantern in Halloween week, none in July
              ([fg(PUMPKIN) in camp_at("calm", d) or bg(PUMPKIN) in camp_at("calm", d)
                for d in (oct28, time.mktime((2026, 7, 2, 21, 0, 0, 0, 0, -1)))], [True, False]),
              # the numbers never shift: each percentage keeps its column whatever it says
              (len({tuple(r.index("%") for r in ANSI.sub("", camp_at(c, now)).split("\n")[1:]) for c in ("calm", "busy", "critical")}), 1),
              # a scene cell shows its four pixels as one colour, top over bottom, or left beside right
              ([block(p)[0] for p in ((CLAY,) * 4, (CLAY, CLAY, GOLD, GOLD), (CLAY, GOLD, CLAY, GOLD))], [" ", "▀", "▌"])]
    for got, want in checks:
        if got != want:
            fails += 1
            print(f"FAIL check: got {got!r}, want {want!r}")
    print("selftest:", "PASS" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


def use(name):
    with open(STYLE_FILE, "w", encoding="utf-8") as f:
        f.write(name + "\n")


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252, which cannot encode block glyphs
    arg = lambda i: argv[i].lower() if len(argv) > i and argv[i].lower() in STYLES else None
    if argv[:1] == ["--demo"]:
        return demo(arg(1) or chosen())
    if argv[:1] == ["--gallery"]:
        return gallery()
    if argv[:1] == ["--selftest"]:
        return selftest()
    if argv[:1] == ["--use"]:
        if not arg(1):
            return print("usage: --use " + "|".join(STYLES)) or 2
        use(arg(1))
        return print(f"status line style set to {arg(1)}")
    return live(arg(1) if argv[:1] == ["--style"] else chosen())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
