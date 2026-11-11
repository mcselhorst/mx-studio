from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Control:
    id: str
    name: str
    label: str
    cid: int | None = None


KEYBOARD = (
    Control("brightness-down", "Screen brightness down", "F1", 0x00C7),
    Control("brightness-up", "Screen brightness up", "F2", 0x00C8),
    Control("backlight-down", "Keyboard backlight down", "F3", 0x00E2),
    Control("backlight-up", "Keyboard backlight up", "F4", 0x00E3),
    Control("dictation", "Dictation", "F5", 0x0103),
    Control("emoji", "Emoji", "F6", 0x0108),
    Control("mic-mute", "Microphone mute", "F7", 0x011C),
    Control("previous", "Previous track", "F8", 0x00E4),
    Control("play-pause", "Play / pause", "F9", 0x00E5),
    Control("next", "Next track", "F10", 0x00E6),
    Control("mute", "Speaker mute", "F11", 0x00E7),
    Control("volume-down", "Volume down", "F12", 0x00E8),
    Control("volume-up", "Volume up", "Vol+", 0x00E9),
    Control("calculator", "Calculator", "Calc", 0x000A),
    Control("screenshot", "Screen capture", "Shot", 0x010A),
    Control("context-menu", "Context menu", "Menu", 0x00EA),
    Control("lock", "Lock screen", "Lock", 0x006F),
)
MOUSE = (
    Control("middle", "Wheel click", "Middle", 0x0052),
    Control("mode-shift", "Wheel mode shift", "Shift", 0x00C4),
    Control("back", "Back button", "Back", 0x0053),
    Control("forward", "Forward button", "Fwd", 0x0056),
    Control("thumb-up", "Thumb wheel up", "↑"),
    Control("thumb-down", "Thumb wheel down", "↓"),
    Control("gesture-tap", "Gesture button tap", "Tap", 0x00C3),
    Control("gesture-up", "Hold gesture button + move up", "↑"),
    Control("gesture-down", "Hold gesture button + move down", "↓"),
    Control("gesture-left", "Hold gesture button + move left", "←"),
    Control("gesture-right", "Hold gesture button + move right", "→"),
)
CONTROLS = {"keyboard": KEYBOARD, "mouse": MOUSE}
GESTURES = tuple(control.id for control in MOUSE if control.id.startswith("gesture-"))
THUMB = ("thumb-up", "thumb-down")
THUMB_SPEED = "thumb-speed"
BACKLIGHT_STEPS = {"backlight:down": -1, "backlight:up": 1}
DICTATION_ACTION = "dictation:toggle"
WHEEL_TOGGLE_ACTION = "wheel:toggle"
MONITOR_APPLICATION_STEPS = {"application:previous-on-monitor": -1, "application:next-on-monitor": 1}
DEVICE_NAMES = {"keyboard": "MX Keys S", "mouse": "MX Master 3S"}
DEVICE_IDS = {0xB378: "keyboard", 0xB034: "mouse"}

SETTING_LABELS = {
    "fn": "Function row",
    "backlight": "Backlight",
    "dpi": "Pointer sensitivity",
    "wheel": "MagSpeed wheel mode",
    "smartshift": "SmartShift threshold",
    THUMB_SPEED: "Horizontal scroll speed",
    "thumb-invert": "Reverse thumb wheel",
}
DEVICE_SETTINGS = {
    "keyboard": ("fn", "backlight"),
    "mouse": ("dpi", "wheel", "smartshift", THUMB_SPEED, "thumb-invert"),
}
SETTING_CHOICES: dict[str, dict[int, str]] = {
    "fn": {0: "F1–F12 first", 1: "Special keys first"},
    "backlight": {0: "Off", 1: "Automatic", 2: "On", 3: "Manual"},
    "dpi": {},
    "wheel": {1: "Free-spin", 2: "Ratchet / SmartShift"},
    "smartshift": {},
    THUMB_SPEED: {percent: f"{percent}%" for percent in (50, 75, 100, 125, 150, 200, 300)},
    "thumb-invert": {0: "Off", 1: "On"},
}

PRESETS = {
    "": "Device default",
    "disabled": "Disable",
    DICTATION_ACTION: "Dictation",
    "key:Emoji": "Emoji picker",
    "key:ContextMenu": "Context menu",
    "key:LockScreen": "Lock screen",
    "key:Copy": "Copy",
    "key:Paste": "Paste",
    "key:Cut": "Cut",
    "key:Undo": "Undo",
    "key:Redo": "Redo",
    "key:NewTab": "New tab",
    "key:CloseTab": "Close tab",
    "key:PreviousTab": "Previous tab",
    "key:NextTab": "Next tab",
    "key:Overview": "Desktop overview",
    "key:SwitchApp": "Switch applications",
    "key:PreviousApplication": "Previous application",
    "key:NextApplication": "Next application",
    "application:previous-on-monitor": "Previous app on cursor monitor",
    "application:next-on-monitor": "Next app on cursor monitor",
    "key:PreviousWorkspace": "Previous workspace",
    "key:NextWorkspace": "Next workspace",
    "key:Screenshot": "Take screenshot",
    "key:Calculator": "Open calculator",
    "key:Terminal": "Open terminal",
    "key:MicMute": "Toggle microphone mute",
    "key:Mute": "Toggle speaker mute",
    "key:VolumeDown": "Volume down",
    "key:VolumeUp": "Volume up",
    "key:BrightnessDown": "Screen brightness down",
    "key:BrightnessUp": "Screen brightness up",
    "backlight:down": "Keyboard backlight down",
    "backlight:up": "Keyboard backlight up",
    "key:Previous": "Previous track",
    "key:PlayPause": "Play / pause",
    "key:Next": "Next track",
    "key:BrowserBack": "Browser back",
    "key:BrowserForward": "Browser forward",
    WHEEL_TOGGLE_ACTION: "Toggle scroll wheel mode",
    "pointer:left": "Left click",
    "pointer:right": "Right click",
    "pointer:middle": "Middle click",
    "pointer:back": "Mouse back button",
    "pointer:forward": "Mouse forward button",
    "pointer:up-scroll": "Scroll up",
    "pointer:down-scroll": "Scroll down",
    "pointer:left-scroll": "Scroll left",
    "pointer:right-scroll": "Scroll right",
}
KEYBOARD_ACTIONS = {
    "brightness-down": "key:BrightnessDown",
    "brightness-up": "key:BrightnessUp",
    "backlight-down": "backlight:down",
    "backlight-up": "backlight:up",
    "dictation": DICTATION_ACTION,
    "emoji": "key:Emoji",
    "mic-mute": "key:MicMute",
    "previous": "key:Previous",
    "play-pause": "key:PlayPause",
    "next": "key:Next",
    "mute": "key:Mute",
    "volume-down": "key:VolumeDown",
    "volume-up": "key:VolumeUp",
    "calculator": "key:Calculator",
    "screenshot": "key:Screenshot",
    "context-menu": "key:ContextMenu",
    "lock": "key:LockScreen",
}
KEY_PRESETS = {
    "Emoji": ("KEY_LEFTCTRL", "KEY_DOT"),
    "ContextMenu": ("KEY_LEFTSHIFT", "KEY_F10"),
    "LockScreen": ("KEY_LEFTMETA", "KEY_L"),
    "Copy": ("KEY_LEFTCTRL", "KEY_C"),
    "Paste": ("KEY_LEFTCTRL", "KEY_V"),
    "Cut": ("KEY_LEFTCTRL", "KEY_X"),
    "Undo": ("KEY_LEFTCTRL", "KEY_Z"),
    "Redo": ("KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_Z"),
    "NewTab": ("KEY_LEFTCTRL", "KEY_T"),
    "CloseTab": ("KEY_LEFTCTRL", "KEY_W"),
    "PreviousTab": ("KEY_LEFTCTRL", "KEY_LEFTSHIFT", "KEY_TAB"),
    "NextTab": ("KEY_LEFTCTRL", "KEY_TAB"),
    "Overview": ("KEY_LEFTMETA",),
    "SwitchApp": ("KEY_LEFTALT", "KEY_TAB"),
    "PreviousApplication": ("KEY_LEFTMETA", "KEY_LEFTSHIFT", "KEY_TAB"),
    "NextApplication": ("KEY_LEFTMETA", "KEY_TAB"),
    "PreviousWorkspace": ("KEY_LEFTMETA", "KEY_PAGEUP"),
    "NextWorkspace": ("KEY_LEFTMETA", "KEY_PAGEDOWN"),
    "Screenshot": ("KEY_SYSRQ",),
    "Calculator": ("KEY_CALC",),
    "Terminal": ("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_T"),
    "MicMute": ("KEY_MICMUTE",),
    "Mute": ("KEY_MUTE",),
    "VolumeDown": ("KEY_VOLUMEDOWN",),
    "VolumeUp": ("KEY_VOLUMEUP",),
    "BrightnessDown": ("KEY_BRIGHTNESSDOWN",),
    "BrightnessUp": ("KEY_BRIGHTNESSUP",),
    "Previous": ("KEY_PREVIOUSSONG",),
    "PlayPause": ("KEY_PLAYPAUSE",),
    "Next": ("KEY_NEXTSONG",),
    "BrowserBack": ("KEY_BACK",),
    "BrowserForward": ("KEY_FORWARD",),
}
POINTER_BUTTONS = {
    "pointer:left": "BTN_LEFT",
    "pointer:right": "BTN_RIGHT",
    "pointer:middle": "BTN_MIDDLE",
    "pointer:back": "BTN_SIDE",
    "pointer:forward": "BTN_EXTRA",
}
POINTER_SCROLL = {
    "pointer:up-scroll": ("REL_WHEEL", 1),
    "pointer:down-scroll": ("REL_WHEEL", -1),
    "pointer:left-scroll": ("REL_HWHEEL", -1),
    "pointer:right-scroll": ("REL_HWHEEL", 1),
}
MODIFIERS = {"CTRL": "LEFTCTRL", "ALT": "LEFTALT", "SHIFT": "LEFTSHIFT", "SUPER": "LEFTMETA"}
KEY_ALIASES = {
    "ESCAPE": "ESC", "RETURN": "ENTER", "PRINT": "SYSRQ", "PRINTSCREEN": "SYSRQ",
    "PAGEUP": "PAGEUP", "PAGEDOWN": "PAGEDOWN", "BACKSPACE": "BACKSPACE",
    "ENTER": "ENTER", "SPACE": "SPACE", "TAB": "TAB", "ESC": "ESC", "HOME": "HOME",
    "END": "END", "INSERT": "INSERT", "DELETE": "DELETE", "UP": "UP", "DOWN": "DOWN",
    "LEFT": "LEFT", "RIGHT": "RIGHT", "PERIOD": "DOT", "COMMA": "COMMA",
    "SLASH": "SLASH", "MINUS": "MINUS", "EQUAL": "EQUAL",
    "LEFTBRACKET": "LEFTBRACE", "RIGHTBRACKET": "RIGHTBRACE", "BACKSLASH": "BACKSLASH",
    "SEMICOLON": "SEMICOLON", "APOSTROPHE": "APOSTROPHE", "GRAVE": "GRAVE",
}


def shortcut_keys(shortcut: str) -> tuple[str, ...]:
    parts = [part.strip().upper() for part in shortcut.split("+")]
    modifiers = parts[:-1]
    if any(modifier not in MODIFIERS for modifier in modifiers):
        raise ValueError("Use Ctrl, Alt, Shift or Super before the final key.")
    if len(set(modifiers)) != len(modifiers):
        raise ValueError("Use each modifier once.")
    key = parts[-1]
    if re.fullmatch(r"[A-Z0-9]|F(?:[1-9]|1[0-9]|2[0-4])", key) is None:
        if key not in KEY_ALIASES:
            raise ValueError("Use a letter, number, F-key or named key such as Print, Space or Tab.")
        key = KEY_ALIASES[key]
    return tuple(f"KEY_{MODIFIERS[part]}" for part in modifiers) + (f"KEY_{key}",)


def validate_action(action: str) -> None:
    if action in PRESETS:
        return
    if action.startswith("shortcut:"):
        shortcut_keys(action.removeprefix("shortcut:"))
        return
    raise ValueError(f"Unknown action {action!r}.")


def action_label(action: str) -> str:
    return PRESETS.get(action, action.removeprefix("shortcut:"))


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


@dataclass
class Config:
    mappings: dict[str, dict[str, str]] = field(
        default_factory=lambda: {device: {} for device in CONTROLS}
    )
    settings: dict[str, dict[str, int]] = field(
        default_factory=lambda: {device: {} for device in CONTROLS}
    )

    def missing_linux_keyboard_shortcuts(self, available: set[str] | None) -> dict[str, str]:
        return {
            control: action
            for control, action in KEYBOARD_ACTIONS.items()
            if action != DICTATION_ACTION
            and control not in self.mappings["keyboard"]
            and (available is None or control in available)
        }

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return cls()
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ValueError("The profile has an unsupported format.")
        config = cls()
        for device, controls in CONTROLS.items():
            mappings = cls._section(raw, "mappings", device)
            settings = cls._section(raw, "settings", device)
            for control in controls:
                action = mappings.get(control.id, "")
                if not isinstance(action, str):
                    raise ValueError(f"Invalid action for {control.name}.")
                validate_action(action)
                if action:
                    config.mappings[device][control.id] = action
            for name in DEVICE_SETTINGS[device]:
                if name not in settings:
                    continue
                value = settings[name]
                if type(value) is not int or not valid_setting(name, value):
                    raise ValueError(f"Invalid value for {SETTING_LABELS[name]}.")
                config.settings[device][name] = value
        return config

    @staticmethod
    def _section(raw: dict[str, Any], name: str, device: str) -> dict[str, Any]:
        group = raw.get(name, {})
        section = group.get(device, {}) if isinstance(group, dict) else None
        if not isinstance(section, dict):
            raise ValueError(f"Invalid {device} {name} in the profile.")
        return section

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(
                    {"version": 1, "mappings": self.mappings, "settings": self.settings},
                    handle, indent=2,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def valid_setting(name: str, value: int) -> bool:
    if name == "dpi":
        return 100 <= value <= 8000
    if name == "smartshift":
        return 1 <= value <= 50
    return value in SETTING_CHOICES[name]
