from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from math import pi
from typing import Any

import cairo
from gi.repository import Gdk, GLib, Gtk

from .models import KEYBOARD

WIDTH, HEIGHT = 720, 236
Rect = tuple[float, float, float, float]


def rounded(ctx: Any, x: float, y: float, w: float, h: float, r: float) -> None:
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, pi / 2)
    ctx.arc(x + r, y + h - r, r, pi / 2, pi)
    ctx.arc(x + r, y + r, r, pi, 3 * pi / 2)
    ctx.close_path()


def color(ctx: Any, hex_color: str) -> None:
    ctx.set_source_rgb(*(int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4)))


def fill(ctx: Any, rect: Rect, radius: float, shade: str) -> None:
    rounded(ctx, *rect, radius)
    color(ctx, shade)
    ctx.fill()


def centered(ctx: Any, label: str, x: float, y: float, size: float, shade: str) -> None:
    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(size)
    ext = ctx.text_extents(label)
    color(ctx, shade)
    ctx.move_to(x - ext.width / 2 - ext.x_bearing, y - ext.height / 2 - ext.y_bearing)
    ctx.show_text(label)


class Diagram:
    def __init__(self, select: Callable[[str], None]) -> None:
        self.picture = Gtk.Picture()
        self.picture.set_size_request(-1, 205)
        self.picture.set_can_shrink(True)
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.picture.set_tooltip_text("Click a highlighted control to select it.")
        self.picture.set_cursor_from_name("pointer")
        self.areas: dict[str, Rect] = {}
        self.select = select
        click = Gtk.GestureClick()
        click.connect("released", self._clicked)
        self.picture.add_controller(click)

    def _clicked(self, _event: Any, _count: int, x: float, y: float) -> None:
        w, h = self.picture.get_width(), self.picture.get_height()
        scale = min(w / WIDTH, h / HEIGHT)
        if scale <= 0:
            return
        px = (x - (w - WIDTH * scale) / 2) / scale
        py = (y - (h - HEIGHT * scale) / 2) / scale
        for key, (left, top, width, height) in self.areas.items():
            if left <= px <= left + width and top <= py <= top + height:
                self.select(key.removesuffix("-dot"))
                return

    def render(self, model: str, selected: str, mapped: set[str]) -> None:
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
        ctx = cairo.Context(surface)
        self.areas.clear()
        if model == "keyboard":
            self._keyboard(ctx, selected, mapped)
        else:
            self._mouse(ctx, selected, mapped)
        buffer = BytesIO()
        surface.write_to_png(buffer)
        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(buffer.getvalue()))
        self.picture.set_paintable(texture)

    def _keyboard(self, ctx: Any, selected: str, mapped: set[str]) -> None:
        fill(ctx, (22, 45, 678, 153), 15, "D9E2F0")
        fill(ctx, (19, 36, 682, 157), 15, "263346")
        fill(ctx, (21, 38, 678, 151), 13, "344359")
        fill(ctx, (667, 43, 11, 2), 1, "6BE3BB")
        key_w, key_h, step = 27, 20, 30

        def key(label: str, x: float, y: float, width: float = key_w, control: str = "") -> None:
            active = control == selected
            rect = (x, y, width, key_h)
            fill(ctx, (x, y + 2, width, key_h), 4, "202D3D")
            shade = "3465EB" if active else "485D7C" if control else "2A384C"
            fill(ctx, rect, 4, shade)
            centered(ctx, label, x + width / 2, y + key_h / 2, 8, "FFFFFF" if control else "99AAC1")
            if control:
                self.areas[control] = rect
            if control in mapped:
                fill(ctx, (x + width - 5, y + 2, 3, 3), 1.5, "91FFD9")

        key("esc", 31, 51, 31)
        for i, control in enumerate(KEYBOARD[:13]):
            key(control.label, 65 + i * 31.5, 51, 28.5, control.id)
        for i in range(3):
            key(str(i + 1), 482 + i * step, 51)
        for i, control in enumerate(KEYBOARD[13:]):
            key(control.label, 569 + i * step, 51, key_w, control.id)

        typing: tuple[list[tuple[str, float]], ...] = (
            [("`", 1), *[(n, 1) for n in "1234567890-="], ("backspace", 2)],
            [("tab", 1.5), *[(n, 1) for n in "QWERTYUIOP[]"], ("\\", 1.5)],
            [("caps", 1.75), *[(n, 1) for n in "ASDFGHJKL;'"], ("enter", 2.25)],
            [("shift", 2.25), *[(n, 1) for n in "ZXCVBNM,./"], ("shift", 2.75)],
            [("ctrl", 1.25), ("super", 1.25), ("alt", 1.25), ("", 6.25), ("alt", 1.25),
             ("fn", 1.25), ("ctrl", 2.5)],
        )
        for row, keys in enumerate(typing):
            x = 31.0
            for label, units in keys:
                width = units * 29.4
                key(label, x, 74 + row * 22, width - 3)
                x += width
        for row, labels in enumerate((
            ("ins", "home", "pgup"), ("del", "end", "pgdn"), ("", "", ""),
            ("", "up", ""), ("left", "down", "right"),
        )):
            for i, label in enumerate(labels):
                if label:
                    key(label, 482 + i * step, 74 + row * 22)
        for row, num_labels in enumerate((
            ("num", "/", "*", "-"), ("7", "8", "9", "+"), ("4", "5", "6", ""),
            ("1", "2", "3", "enter"), ("0", "", ".", ""),
        )):
            for i, label in enumerate(num_labels):
                if label:
                    key(label, 569 + i * step, 74 + row * 22, 57 if label == "0" else key_w)

    def _mouse(self, ctx: Any, selected: str, mapped: set[str]) -> None:
        fill(ctx, (272, 198, 191, 23), 11, "D8E1F1")
        ctx.move_to(339, 20)
        ctx.curve_to(377, 7, 433, 20, 452, 72)
        ctx.curve_to(470, 121, 476, 187, 426, 204)
        ctx.curve_to(399, 216, 326, 216, 277, 197)
        ctx.curve_to(250, 187, 259, 155, 279, 133)
        ctx.curve_to(297, 114, 290, 36, 339, 20)
        ctx.close_path()
        gradient = cairo.LinearGradient(286, 56, 462, 187)
        gradient.add_color_stop_rgb(0, 0.27, 0.33, 0.42)
        gradient.add_color_stop_rgb(1, 0.10, 0.15, 0.24)
        ctx.set_source(gradient)
        ctx.fill()
        color(ctx, "26364E")
        ctx.set_line_width(2)
        ctx.move_to(378, 17)
        ctx.line_to(378, 87)
        ctx.move_to(300, 100)
        ctx.curve_to(353, 84, 406, 83, 455, 95)
        ctx.stroke()
        fill(ctx, (363, 30, 28, 49), 11, "203049")
        fill(ctx, (368, 33, 18, 42), 7, "6D7C96")
        for y in range(40, 72, 5):
            fill(ctx, (370, y, 14, 1.3), 0.5, "9DA9BE")
        fill(ctx, (367, 93, 21, 13), 6, "7889A7")
        fill(ctx, (297, 86, 16, 33), 7, "8A9AB5")
        fill(ctx, (287, 125, 10, 23), 4, "596D8D")
        fill(ctx, (291, 149, 10, 20), 4, "596D8D")
        fill(ctx, (272, 178, 37, 9), 4, "536989")

        def point(name: str, label: str, origin: tuple[int, int], rect: Rect) -> None:
            x, y = origin
            left, top, w, h = rect
            active = name == selected or (name == "gesture-tap" and selected.startswith("gesture-"))
            line = "3465EB" if active else "9AACCB"
            color(ctx, line)
            ctx.set_line_width(1.6)
            ctx.move_to(x, y)
            ctx.line_to(left + w / 2, top + h / 2)
            ctx.stroke()
            ctx.arc(x, y, 4.5, 0, 2 * pi)
            color(ctx, "759AFF")
            ctx.fill()
            fill(ctx, rect, 8, "3465EB" if active else "FFFFFF")
            centered(ctx, label, left + w / 2, top + h / 2, 11, "FFFFFF" if active else "3D5374")
            if name in mapped:
                fill(ctx, (left + w - 8, top + 4, 4, 4), 2, "1EAF82")
            self.areas[name] = rect
            self.areas[name + "-dot"] = (x - 8, y - 8, 16, 16)

        point("middle", "Wheel click", (377, 50), (463, 37, 100, 26))
        point("mode-shift", "Mode shift", (377, 100), (481, 91, 98, 26))
        point("thumb-up", "Thumb wheel up", (304, 91), (157, 55, 118, 26))
        point("thumb-down", "Thumb wheel down", (303, 111), (139, 87, 122, 26))
        point("forward", "Forward", (291, 133), (155, 120, 96, 26))
        point("back", "Back", (296, 156), (169, 151, 81, 26))
        point("gesture-tap", "Gesture button", (285, 183), (115, 182, 130, 26))
