"""Mini black hole in the corner of the screen the mouse is on while Fedora listens with her window closed or in the background.

The ear starts this with the system python (GTK 4) and writes one JSON event per line on stdin:
{"t": "level", "v": 0..1}, {"t": "state", "v": "listen"|"think"|...}, {"t": "command", "text": ...}, {"t": "bye"}. EOF also closes it.
KWin places it in the top-left corner of the screen the mouse is on, on top, without focus (rule installed by `python3 orb.py --regra`).
"""
import json
import math
import subprocess
import sys
import threading
import time

APP_ID = "org.fedora.orb"
HOLE = 132                                   # scale of the drawing
SIZE = 184                                   # window: fits the widest glow (2 * 0.685 * HOLE) so its edge never shows
TEXT_W = 300                                 # room on the right for what she heard


def install_rule():
    """KWin window rule: top-left corner of the mouse's screen, always on top, no border, never takes focus, not in the taskbar."""
    rule = dict(Description="Fedora — mini buraco negro", wmclass=APP_ID, wmclassmatch="1", types="1",
                placement="6", placementrule="2",             # 6 = top-left corner of the screen with the mouse; 2 = force
                above="true", aboverule="2", noborder="true", noborderrule="2",
                acceptfocus="false", acceptfocusrule="2", fsplevel="4", fsplevelrule="2",
                skiptaskbar="true", skiptaskbarrule="2", skippager="true", skippagerrule="2",
                skipswitcher="true", skipswitcherrule="2")
    group = "fedora-orb"
    for k, v in rule.items():
        subprocess.run(["kwriteconfig6", "--file", "kwinrulesrc", "--group", group, "--key", k, v], check=True)
    have = subprocess.run(["kreadconfig6", "--file", "kwinrulesrc", "--group", "General", "--key", "rules"],
                          capture_output=True, text=True).stdout.strip()
    rules = [r for r in have.split(",") if r and r != group] + [group]
    subprocess.run(["kwriteconfig6", "--file", "kwinrulesrc", "--group", "General", "--key", "rules", ",".join(rules)], check=True)
    subprocess.run(["kwriteconfig6", "--file", "kwinrulesrc", "--group", "General", "--key", "count", str(len(rules))], check=True)
    subprocess.run(["gdbus", "call", "--session", "--dest", "org.kde.KWin", "--object-path", "/KWin",
                    "--method", "org.kde.KWin.reconfigure"], check=False, stdout=subprocess.DEVNULL)
    print("regra do KWin instalada")


def main():
    import cairo
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Gdk, Gio, GLib, Gtk, Pango, PangoCairo

    st = dict(level=0.0, shown=0.0, state="listen", closing=False, born=time.time(), text="")

    def draw_text(cr, x, cy):
        """What she heard, on a dark translucent pill to the right of the hole (max 3 lines)."""
        lay = PangoCairo.create_layout(cr)
        lay.set_font_description(Pango.FontDescription.from_string("Sans 11"))
        lay.set_width((TEXT_W - 32) * Pango.SCALE)
        lay.set_wrap(Pango.WrapMode.WORD_CHAR)
        lay.set_height(-3)
        lay.set_ellipsize(Pango.EllipsizeMode.END)
        lay.set_text(st["text"], -1)
        _ink, log = lay.get_pixel_extents()
        w, h, pad = log.width + 28, log.height + 18, 14
        y = cy - h / 2
        rad = min(14, h / 2)
        cr.new_sub_path()
        cr.arc(x + w - rad, y + rad, rad, -math.pi / 2, 0)
        cr.arc(x + w - rad, y + h - rad, rad, 0, math.pi / 2)
        cr.arc(x + rad, y + h - rad, rad, math.pi / 2, math.pi)
        cr.arc(x + rad, y + rad, rad, math.pi, 1.5 * math.pi)
        cr.close_path()
        cr.set_source_rgba(0.06, 0.05, 0.08, 0.72)
        cr.fill()
        cr.set_source_rgba(1, 0.93, 0.85, 0.95)
        cr.move_to(x + pad, y + 9)
        PangoCairo.show_layout(cr, lay)

    def draw(_area, cr, w, h, _data=None):
        t = time.time() - st["born"]
        cx, cy = SIZE / 2, h / 2
        lv = st["shown"]
        breath = 0.5 + 0.5 * math.sin(t * (4.0 if st["state"] == "think" else 2.2))
        r = HOLE * 0.17 * (1 + 0.10 * breath + 0.45 * lv)     # the hole swells with the voice
        glow = r * (2.0 + 0.6 * lv)
        g = cairo.RadialGradient(cx, cy, r * 0.9, cx, cy, glow)  # amber accretion glow
        g.add_color_stop_rgba(0, 1.0, 0.72, 0.30, 0.85)
        g.add_color_stop_rgba(0.35, 0.95, 0.50, 0.12, 0.35 + 0.3 * lv)
        g.add_color_stop_rgba(1, 0.6, 0.25, 0.05, 0)
        cr.set_source(g)
        cr.arc(cx, cy, glow, 0, 2 * math.pi)
        cr.fill()
        spin = t * (2.4 if st["state"] == "think" else 0.9)   # thin ring, faster while thinking
        cr.set_line_width(1.6)
        cr.set_source_rgba(1.0, 0.82, 0.5, 0.75)
        cr.arc(cx, cy, r * 1.25, spin, spin + math.pi * 1.2)
        cr.stroke()
        cr.set_source_rgb(0, 0, 0)                              # event horizon
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()
        if st["text"]:
            draw_text(cr, SIZE - 24, cy)

    def activate(app):
        css = Gtk.CssProvider()
        css.load_from_data(b"window, window.background, window.csd, decoration, drawingarea "
                           b"{ background: none; background-color: transparent; box-shadow: none; border: none; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = Gtk.ApplicationWindow(application=app, title="Fedora", decorated=False, resizable=False)
        win.set_default_size(SIZE + TEXT_W, SIZE)
        win.set_opacity(0)
        win.remove_css_class("background")
        area = Gtk.DrawingArea()
        area.set_draw_func(draw)
        win.set_child(area)

        def realized(_w):
            win.get_surface().set_input_region(cairo.Region())  # clicks go through to what is underneath
        win.connect("realize", realized)

        def tick(_w, _clock):
            st["shown"] += (st["level"] - st["shown"]) * 0.25
            st["level"] *= 0.92                                  # decays between level events
            op = win.get_opacity()
            if st["closing"]:
                op -= 0.08
                if op <= 0:
                    app.quit()
                    return False
            elif op < 1:
                op = min(1, op + 0.1)
            win.set_opacity(max(0, op))
            area.queue_draw()
            return True
        win.add_tick_callback(tick)
        win.present()

        def read():
            for line in sys.stdin:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                GLib.idle_add(handle, ev)
            GLib.idle_add(handle, dict(t="bye"))

        def handle(ev):
            if ev.get("t") == "level":
                st["level"] = max(st["level"], min(1.0, float(ev.get("v", 0)) * 2.5))
            elif ev.get("t") == "state":
                st["state"] = ev.get("v", "listen")
            elif ev.get("t") == "command":
                st["text"] = str(ev.get("text", "")).strip()
            elif ev.get("t") == "bye":
                st["closing"] = True
            return False
        threading.Thread(target=read, daemon=True).start()

    app = Gtk.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    install_rule() if "--regra" in sys.argv else main()
