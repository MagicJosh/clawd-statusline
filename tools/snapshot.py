"""Render the status line to PNG and GIF for the README. Needs Pillow: pip install pillow

  python tools/snapshot.py            writes docs/*.gif and docs/*.png

Block and box glyphs are drawn as shapes, the way VS Code's terminal draws them, so the
images stay crisp whatever font is installed. Other text uses Consolas (or DejaVu Sans Mono).
"""
import os
import sys
import time

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import statusline as S  # noqa: E402

DOCS = os.path.join(ROOT, "docs")
CW, CH, SS = 9, 19, 4  # cell size in pixels, and the supersampling factor
PAGE = (39, 40, 34)  # Monokai background
INK = (248, 248, 242)
QUAD = {"▘": 1, "▝": 2, "▖": 4, "▗": 8, "▀": 3, "▄": 12, "▌": 5, "▐": 10, "▚": 9, "▞": 6,
        "▙": 13, "▛": 7, "▜": 11, "▟": 14, "█": 15}
LOWER = {"▂": 0.25, "▆": 0.75}


def fonts(scale):
    def load(names):
        for n in names:
            for d in ("C:/Windows/Fonts", "/usr/share/fonts/truetype/dejavu", "/Library/Fonts"):
                try:
                    return ImageFont.truetype(os.path.join(d, n), round(CH * SS * scale * 0.74))
                except OSError:
                    pass
        return ImageFont.load_default()
    mono = ("consola.ttf", "DejaVuSansMono.ttf")
    return {"": load(mono), "1": load(("consolab.ttf",) + mono), "3": load(("consolai.ttf",) + mono),
            "13": load(("consolaz.ttf",) + mono), "sym": load(("seguisym.ttf", "DejaVuSans.ttf"))}


FONTS = {1: fonts(1), 2: fonts(2)}


def missing(font, ch):
    """True when the font has no glyph for ch: it draws the same box as a surely missing one."""
    return bytes(font.getmask(ch)) == bytes(font.getmask("\U000F0000"))


def cells(line):
    """ANSI line -> [(char, fg, bg, attrs)] using the status line's own SGR parser."""
    f = b = None
    attr, out = "", []
    for part in S.ANSI.split(line):
        if part.startswith("\x1b"):
            f, b, attr = S.sgr(part, f, b, attr)
        else:
            out += [(ch, f, b, attr) for ch in part]
    return out


def draw_cell(d, img, x, y, ch, f, b, attr, scale=1):
    w, h = CW * SS * scale, CH * SS * scale
    x0, y0 = x * w, y * h
    f, b = f or INK, b or PAGE
    d.rectangle([x0, y0, x0 + w - 1, y0 + h - 1], fill=b)
    if ch == " ":
        return
    if ch in QUAD:
        m = QUAD[ch]
        for bit, (qx, qy) in ((1, (0, 0)), (2, (1, 0)), (4, (0, 1)), (8, (1, 1))):
            if m & bit:
                d.rectangle([x0 + qx * w // 2, y0 + qy * h // 2, x0 + (qx + 1) * w // 2 - 1, y0 + (qy + 1) * h // 2 - 1], fill=f)
    elif 0x1FB00 <= ord(ch) <= 0x1FB3B:  # sextants: 2 x 3 pixels, Unicode skips the two half blocks
        m = ord(ch) - 0x1FB00 + 1
        m += m >= 21
        m += m >= 42
        for i in range(6):
            if m >> i & 1:
                qx, qy = i % 2, i // 2
                d.rectangle([x0 + qx * w // 2, y0 + qy * h // 3, x0 + (qx + 1) * w // 2 - 1, y0 + (qy + 1) * h // 3 - 1], fill=f)
    elif ch in LOWER:
        d.rectangle([x0, y0 + round(h * (1 - LOWER[ch])), x0 + w - 1, y0 + h - 1], fill=f)
    elif ch in ("\ue0b4", "\ue0b6"):  # rounded powerline caps: half an ellipse that fills the cell
        cell = Image.new("L", (w, h), 0)
        cx = 0 if ch == "\ue0b4" else w
        ImageDraw.Draw(cell).ellipse([cx - w, 0, cx + w - 1, h - 1], fill=255)
        img.paste(f, (x0, y0, x0 + w, y0 + h), cell)
    elif ch in "┃│":
        t = w * (0.26 if ch == "┃" else 0.12)
        d.rectangle([x0 + (w - t) / 2, y0, x0 + (w + t) / 2, y0 + h - 1], fill=f)
    elif ch in "━─╸":
        t = h * (0.12 if ch == "━" or ch == "╸" else 0.06)
        d.rectangle([x0, y0 + (h - t) / 2, x0 + (w if ch != "╸" else w / 2) - 1, y0 + (h + t) / 2], fill=f)
    else:
        font = FONTS[scale].get(attr, FONTS[scale][""])
        if missing(font, ch):
            font = FONTS[scale]["sym"]
        d.text((x0 + w / 2, y0 + h * 0.54), ch, font=font, fill=f, anchor="mm")


def frame(rows, cols, first=0, scale=1):
    """Columns first to first+cols of the rows, as an image; scale 2 draws every cell twice as big."""
    pad = 1  # a cell of page around the picture
    img = Image.new("RGB", ((cols + 2 * pad) * CW * SS * scale, (len(rows) + 2 * pad) * CH * SS * scale), PAGE)
    d = ImageDraw.Draw(img)
    for y, line in enumerate(rows):
        for x, (ch, f, b, a) in enumerate(cells(line)[first:first + cols]):
            draw_cell(d, img, x + pad, y + pad, ch, f, b, a, scale)
    return img.resize((img.width // SS, img.height // SS), Image.LANCZOS)


def prompt(text, cols):
    """Claude Code's input box, so the status line is seen where it lives: right under it."""
    rule = S.paint((90, 92, 82), "─" * cols)
    return [rule, S.paint(INK, "> " + text) + S.bg(INK) + " " + S.RST, rule]


def label(text):
    return S.paint((117, 113, 94), text)


def payload(case, wall):
    if case != "hero":
        return S.scenario(case, wall)
    d = S.scenario("calm", wall)  # a clear night with a fuller sky
    d["context_window"] = {"context_window_size": 200000, "used_percentage": 58,
                           "current_usage": {"input_tokens": 116000, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
    d["rate_limits"] = {"five_hour": {"used_percentage": 34, "resets_at": wall + 2.9 * 3600},
                        "seven_day": {"used_percentage": 41, "resets_at": wall + 3.6 * 86400}}
    return d


def state(style, case, wall, tick, cols):
    s = S.read_state(payload(case, wall), wall)
    s.dir, s.branch, s.clock = "clawd-statusline", "main", tick  # the same folder in every picture
    return S.render(style, s, cols)


def gif(path, frames, ms=700):
    pal = [f.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in frames]
    pal[0].save(path, save_all=True, append_images=pal[1:], duration=ms, loop=0, optimize=False, disposal=1)


def at(y, mo, d, h, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def strips(style, shots, cols, tick=3):
    """Several status lines stacked in one picture, each under its label."""
    rows = []
    for name, case, wall in shots:
        rows += [label(name)] + state(style, case, wall, tick, cols) + [""]
    return frame(rows[:-1], cols)


def main(argv):
    os.makedirs(DOCS, exist_ok=True)
    only = argv[0] if argv else None
    night = at(2026, 9, 14, 22, 30)  # an ordinary night: no calendar extras in the hero
    out = lambda name: os.path.join(DOCS, name)
    jobs = {
        "campfire": lambda: gif(out("campfire.gif"), [frame(prompt("make it hellaaa prettyyy", 180) + state("campfire", "hero", night, t, 180), 180)
                                                      for t in range(30)]),
        "closeup": lambda: gif(out("campfire-closeup.gif"), [frame(state("campfire", "hero", night, t, 180), 64, 116, 2) for t in range(30)]),
        "dashboard": lambda: gif(out("dashboard.gif"), [frame(state("dashboard", "busy", night, t, 150), 150) for t in range(24)]),
        "chips": lambda: gif(out("chips.gif"), [frame(state("chips", "busy", night, t, 150), 150) for t in range(24)]),
        "states": lambda: strips("campfire", [("calm: clear sky, full woodpile, a waning moon", "calm", night),
                                              ("busy: clouds drift in, logs burn, the moon thins", "busy", night),
                                              ("critical: storm, rain on the fire, Clawd sounds the alarm", "critical", night),
                                              ("limit hit: the fire is out and Clawd sleeps in the tent until the reset", "asleep", night)],
                                 180).save(out("campfire-states.png")),
        "calendar": lambda: strips("campfire", [("dusk", "hero", at(2026, 9, 14, 18, 40)), ("dawn", "hero", at(2026, 9, 15, 6, 50)),
                                                ("Halloween week", "hero", at(2026, 10, 30, 22)), ("December", "hero", at(2026, 12, 18, 21)),
                                                ("New Year's Eve", "hero", at(2026, 12, 31, 23, 50)), ("summer nights", "hero", at(2026, 7, 20, 23))],
                                   180, tick=2).save(out("campfire-calendar.png")),
    }
    for name, job in jobs.items():
        if only is None or name.startswith(only):
            job()
            print("wrote", name)


if __name__ == "__main__":
    main(sys.argv[1:])
