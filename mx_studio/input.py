from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from math import trunc
from struct import unpack_from
from time import monotonic
from typing import Any

from .models import (
    BACKLIGHT_STEPS,
    GESTURES,
    KEY_PRESETS,
    POINTER_BUTTONS,
    POINTER_SCROLL,
    shortcut_keys,
)

REPEAT_ACTIONS = frozenset({
    "key:VolumeDown", "key:VolumeUp", "key:BrightnessDown", "key:BrightnessUp", *BACKLIGHT_STEPS,
})
REPEAT_DELAY = 0.35
REPEAT_INTERVAL = 0.125
WHEEL_UNITS_PER_DETENT = 120


class OutputError(OSError):
    """The virtual input device failed to emit an event."""


class InputOutput:
    def __init__(self, evdev: Any) -> None:
        self.codes = evdev.ecodes
        self.horizontal_remainder = 0
        # A full keyboard capability set lets the compositor classify the virtual device.
        keys = [code for code in range(1, 249) if code in self.codes.KEY]
        self.keyboard = evdev.UInput({self.codes.EV_KEY: keys}, name="MX Studio shortcuts")
        try:
            self.pointer = evdev.UInput(
                {
                    self.codes.EV_KEY: [self.codes.ecodes[key] for key in POINTER_BUTTONS.values()],
                    self.codes.EV_REL: [
                        self.codes.REL_X, self.codes.REL_Y,
                        self.codes.REL_WHEEL, self.codes.REL_HWHEEL, self.codes.REL_HWHEEL_HI_RES,
                    ],
                },
                name="MX Studio wheel and clicks",
            )
        except Exception:
            self.keyboard.close()
            raise

    def emit(self, action: str) -> None:
        try:
            self._emit(action)
        except OSError as error:
            raise OutputError(f"Shortcut output failed ({error}).") from error

    def _emit(self, action: str) -> None:
        if not action or action == "disabled":
            return
        if action in POINTER_BUTTONS:
            self._tap(self.pointer, [self.codes.ecodes[POINTER_BUTTONS[action]]])
            return
        if action in POINTER_SCROLL:
            axis, delta = POINTER_SCROLL[action]
            if axis == "REL_HWHEEL":
                self._write_horizontal_scroll(delta * WHEEL_UNITS_PER_DETENT)
            else:
                self.pointer.write(self.codes.EV_REL, self.codes.ecodes[axis], delta)
                self.pointer.syn()
            return
        if action.startswith("shortcut:"):
            keys = shortcut_keys(action.removeprefix("shortcut:"))
        else:
            keys = KEY_PRESETS[action.removeprefix("key:")]
        self._tap(self.keyboard, [self.codes.ecodes[name] for name in keys])

    def scroll_horizontal(self, units: int) -> None:
        try:
            self._write_horizontal_scroll(units)
        except OSError as error:
            raise OutputError(f"Horizontal scroll output failed ({error}).") from error

    def _write_horizontal_scroll(self, units: int) -> None:
        if not units:
            return
        self.horizontal_remainder += units
        detents = trunc(self.horizontal_remainder / WHEEL_UNITS_PER_DETENT)
        self.pointer.write(self.codes.EV_REL, self.codes.REL_HWHEEL_HI_RES, units)
        if detents:
            self.pointer.write(self.codes.EV_REL, self.codes.REL_HWHEEL, detents)
        self.pointer.syn()
        self.horizontal_remainder -= detents * WHEEL_UNITS_PER_DETENT

    def _tap(self, device: Any, keys: list[int]) -> None:
        pressed = []
        try:
            for key in keys:
                device.write(self.codes.EV_KEY, key, 1)
                pressed.append(key)
            device.syn()
        finally:
            for key in reversed(pressed):
                device.write(self.codes.EV_KEY, key, 0)
            device.syn()

    def close(self) -> None:
        self.pointer.close()
        self.keyboard.close()


@dataclass(frozen=True)
class ThumbScroll:
    speed: int
    native_resolution: int
    diverted_resolution: int
    direction: int


@dataclass
class ControlEvents:
    actions: dict[int, str] = field(default_factory=dict)
    gestures: dict[str, str] = field(default_factory=dict)
    thumb: dict[str, str] = field(default_factory=dict)
    thumb_scroll: ThumbScroll | None = None
    dpi: int = 1000
    skip_first_motion: bool = True
    pressed: set[int] = field(default_factory=set)
    repeat_at: dict[int, float] = field(default_factory=dict)
    dx: int = 0
    dy: int = 0
    skip_motion: bool = False
    thumb_delta: int = 0
    thumb_scroll_remainder: int = 0

    def buttons(self, address: int, data: bytes, emit: Callable[[str], None]) -> None:
        if address == 0 and len(data) >= 8:
            current = {cid for cid in unpack_from(">4H", data) if cid}
            released = self.pressed - current
            for cid in released:
                self.repeat_at.pop(cid, None)
            now = monotonic()
            for cid in sorted(current - self.pressed):
                if cid == 0x00C3 and self.gestures:
                    self.dx = self.dy = 0
                    self.skip_motion = self.skip_first_motion
                elif cid in self.actions:
                    action = self.actions[cid]
                    emit(action)
                    if action in REPEAT_ACTIONS:
                        self.repeat_at[cid] = now + REPEAT_DELAY
            if 0x00C3 in released and self.gestures:
                emit(self._gesture_action())
            self.pressed = current
        elif address == 0x10 and len(data) >= 4 and 0x00C3 in self.pressed and self.gestures:
            if self.skip_motion:
                self.skip_motion = False
                return
            dx, dy = unpack_from(">hh", data)
            self.dx += dx
            self.dy += dy

    def repeat_held(self, now: float, emit: Callable[[str], None]) -> None:
        for cid, deadline in self.repeat_at.items():
            if now >= deadline:
                self.repeat_at[cid] = now + REPEAT_INTERVAL
                emit(self.actions[cid])

    def set_button_action(self, cid: int, action: str) -> None:
        if self.actions.get(cid, "") == action:
            return
        self.repeat_at.pop(cid, None)
        if action:
            self.actions[cid] = action
        else:
            self.actions.pop(cid, None)

    def reset_buttons(self) -> None:
        self.pressed.clear()
        self.repeat_at.clear()

    def _gesture_action(self) -> str:
        if max(abs(self.dx), abs(self.dy)) < self.dpi * 0.35:
            return self.gestures.get("gesture-tap", "key:Overview")
        if abs(self.dx) >= abs(self.dy):
            direction = "right" if self.dx > 0 else "left"
        else:
            direction = "down" if self.dy > 0 else "up"
        return self.gestures.get(f"gesture-{direction}", "")

    def configure_thumb(self, mappings: dict[str, str], scroll: ThumbScroll | None) -> None:
        if self.thumb != mappings or self.thumb_scroll != scroll:
            self.thumb = mappings
            self.thumb_scroll = scroll
            self.thumb_delta = 0
            self.thumb_scroll_remainder = 0

    def wheel(
        self, data: bytes, emit: Callable[[str], None], scroll_horizontal: Callable[[int], None]
    ) -> None:
        if len(data) < 5 or (not self.thumb and self.thumb_scroll is None):
            return
        if data[4] <= 1:
            self.thumb_delta = 0
        delta = int(unpack_from(">h", data)[0])
        control = "thumb-up" if delta < 0 else "thumb-down"
        scroll = self.thumb_scroll
        action = self.thumb.get(control)
        if scroll is not None and delta and action in (None, "pointer:left-scroll", "pointer:right-scroll"):
            horizontal_delta = (
                delta * scroll.direction if action is None else abs(delta) * POINTER_SCROLL[action][1]
            )
            self.thumb_delta = 0
            # Convert HID++ increments to the wheel's native detents and apply the speed.
            divisor = scroll.diverted_resolution * 100
            self.thumb_scroll_remainder += (
                horizontal_delta * scroll.native_resolution * scroll.speed
                * WHEEL_UNITS_PER_DETENT
            )
            units = trunc(self.thumb_scroll_remainder / divisor)
            self.thumb_scroll_remainder -= units * divisor
            if units:
                scroll_horizontal(units)
            return
        self.thumb_delta += delta
        # Keep a remainder so very small reports do not fire whole shortcuts.
        steps = min(8, abs(self.thumb_delta) // 10)
        if not steps:
            return
        up = self.thumb_delta < 0
        self.thumb_delta += steps * 10 if up else -steps * 10
        control = "thumb-up" if up else "thumb-down"
        default = "pointer:left-scroll" if up else "pointer:right-scroll"
        for _ in range(steps):
            emit(self.thumb.get(control, default))


def gesture_bindings(mappings: dict[str, str]) -> dict[str, str]:
    gestures = {key: mappings[key] for key in GESTURES if key in mappings}
    if any(key != "gesture-tap" for key in gestures):
        return gestures
    return {}
