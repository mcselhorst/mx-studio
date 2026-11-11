from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass, field
from pathlib import Path
from struct import pack, unpack_from
from time import monotonic
from typing import Any

from .applications import MonitorApplicationSwitcher
from .input import ControlEvents, InputOutput, OutputError, ThumbScroll, gesture_bindings
from .models import (
    BACKLIGHT_STEPS,
    CONTROLS,
    DEVICE_IDS,
    DEVICE_NAMES,
    DEVICE_SETTINGS,
    DICTATION_ACTION,
    GESTURES,
    MONITOR_APPLICATION_STEPS,
    SETTING_CHOICES,
    THUMB,
    THUMB_SPEED,
    WHEEL_TOGGLE_ACTION,
    Config,
    action_label,
)

LOG = logging.getLogger(__name__)
BOLT = 0xC548
REPROG = 0x1B04
THUMB_WHEEL = 0x2150
WIRELESS_STATUS = 0x1D4B
BATTERY = 0x1004
DPI = 0x2201
SMARTSHIFT = 0x2110
SMARTSHIFT_ENHANCED = 0x2111
FN_SWAP = 0x40A3
HOSTS = 0x1815
BACKLIGHT = 0x1982
MANUAL_BACKLIGHT = 3

DIVERTABLE = 0x20
RAW_XY_CAPABLE = 0x0100
DIVERTED = 0x01
PERSISTENT = 0x04
RAW_XY = 0x10


@dataclass
class DeviceStatus:
    transport: str = ""
    battery: int | None = None
    controls: set[str] = field(default_factory=set)
    values: dict[str, int] = field(default_factory=dict)
    options: dict[str, list[int]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class Snapshot:
    devices: dict[str, DeviceStatus] = field(default_factory=dict)
    message: str = ""
    output_ready: bool = False
    receivers: int = 0


@dataclass(frozen=True)
class BacklightConfig:
    enabled: bool
    options: int
    supported: int
    level: int
    durations: bytes

    @classmethod
    def from_report(cls, report: bytes) -> BacklightConfig:
        if len(report) < 12:
            raise OSError("The keyboard returned incomplete backlight settings.")
        # The fourth timeout is optional. Preserve it along with the original three.
        durations = report[6:14] if len(report) >= 14 else report[6:12]
        return cls(bool(report[0] & 1), report[1], report[2], report[5], durations)

    @property
    def mode(self) -> int:
        return (self.options >> 3) & 3

    @property
    def setting(self) -> int:
        if not self.enabled:
            return 0
        return self.mode if self.mode in (1, MANUAL_BACKLIGHT) else 2

    def write_payload(self, *, enabled: bool, mode: int, level: int) -> bytes:
        # 0xFF leaves the lighting effect unchanged.
        return bytes((int(enabled), (self.options & 7) | (mode << 3), 0xFF, level)) + self.durations


def number(value: int | str | None) -> int:
    if isinstance(value, str):
        return int(value, 16)
    return value or 0


class HidDevice:
    def __init__(self, device: Any, feature_enum: Any, model: str, transport: str) -> None:
        self.device = device
        self.feature_enum = feature_enum
        self.model = model
        self.status = DeviceStatus(transport)
        self.events = ControlEvents()
        self.owned: set[int] = set()
        self.thumb_owned = False
        self.indexes: dict[int, int] = {}
        self.online = False
        self.dirty = True
        self.hardware_preferences: dict[str, int] = {}

    def feature(self, identifier: int) -> Any:
        if callable(self.feature_enum):
            return self.feature_enum(identifier)
        return self.feature_enum[identifier]

    def index(self, identifier: int) -> int:
        if identifier not in self.indexes:
            self.indexes[identifier] = int(self.device.features[self.feature(identifier)] or 0)
        return self.indexes[identifier]

    def request(self, identifier: int, function: int, *params: bytes | int) -> bytes:
        if not self.index(identifier):
            raise ValueError("This firmware does not expose the feature.")
        data = self.device.feature_request(self.feature(identifier), function, *params)
        if not data:
            raise OSError("The device did not reply.")
        return bytes(data)

    def initialize(self) -> None:
        self.indexes.clear()
        self.hardware_preferences.clear()
        self.status.controls.clear()
        self.status.values.clear()
        self.status.options.clear()
        self.status.errors.clear()
        self.events.reset_buttons()
        self.events.configure_thumb({}, None)
        if self.index(REPROG):
            capabilities = {}
            for key in self.device.keys:
                if key is None:
                    continue
                # Solaar 1.1.11 yields flag names and newer releases expose a Flag.
                flags = key.flags
                bits = getattr(flags, "value", None)
                if bits is not None:
                    capable = bool(bits & DIVERTABLE)
                    raw_xy = bool(bits & RAW_XY_CAPABLE)
                else:
                    names = set(flags)
                    capable = "divertable" in names
                    raw_xy = "raw XY" in names
                if capable:
                    capabilities[int(key.key)] = raw_xy
            for control in CONTROLS[self.model]:
                if control.cid in capabilities:
                    self.status.controls.add(control.id)
            if self.model == "mouse" and capabilities.get(0x00C3):
                self.status.controls.update(GESTURES)
            self.events.skip_first_motion = (
                self.device.features.get_feature_version(self.feature(REPROG)) >= 5
            )
        if self.model == "mouse" and self.index(THUMB_WHEEL):
            self.status.controls.update(THUMB)
        self.refresh_settings()

    def refresh_battery(self) -> None:
        with suppress(Exception):
            report = self.request(BATTERY, 0x10)
            self.status.battery = report[0] if 0 <= report[0] <= 100 else None

    def refresh_settings(self) -> None:
        self.refresh_battery()
        for name in DEVICE_SETTINGS[self.model]:
            if name == THUMB_SPEED:
                if "thumb-up" in self.status.controls:
                    self.status.options[name] = list(SETTING_CHOICES[name])
                continue
            try:
                value, choices = self.read_setting(name)
                self.status.values[name] = value
                self.status.options[name] = choices
                if name == "dpi":
                    self.events.dpi = value
            except Exception as error:
                LOG.debug("Cannot read %s on %s: %s", name, self.model, error)

    def read_setting(self, name: str) -> tuple[int, list[int]]:
        if name == "fn":
            host = self.request(HOSTS, 0)[3]
            return self.request(FN_SWAP, 0, host)[1], [0, 1]
        if name == "backlight":
            backlight = BacklightConfig.from_report(self.request(BACKLIGHT, 0))
            choices = [0]
            if backlight.supported & 0x08:
                choices.append(1)
            if backlight.supported & 0x20:
                choices.append(MANUAL_BACKLIGHT)
            if len(choices) == 1:
                choices.append(2)
            return backlight.setting, choices
        if name == "dpi":
            report = self.request(DPI, 0x10)
            value, default = unpack_from(">HH", self.request(DPI, 0x20), 1)
            return value or default, dpi_options(report)
        if name == "thumb-invert":
            return self.request(THUMB_WHEEL, 0x10)[1] & 1, [0, 1]
        feature, read, _write = self._wheel_feature()
        report = self.request(feature, read)
        if name == "wheel":
            return report[0], [1, 2]
        return min(50, report[1]), list(range(1, 51))

    def _wheel_feature(self) -> tuple[int, int, int]:
        if self.index(SMARTSHIFT_ENHANCED):
            return SMARTSHIFT_ENHANCED, 0x10, 0x20
        return SMARTSHIFT, 0, 0x10

    def set_setting(self, name: str, value: int) -> None:
        if value not in self.status.options.get(name, []):
            raise ValueError("This value is unavailable on the connected device.")
        if name == "fn":
            host = self.request(HOSTS, 0)[3]
            self.request(FN_SWAP, 0x10, host, value)
        elif name == "backlight":
            backlight = BacklightConfig.from_report(self.request(BACKLIGHT, 0))
            mode = backlight.mode if value == 0 else {1: 1, 2: 0, 3: MANUAL_BACKLIGHT}[value]
            payload = backlight.write_payload(
                enabled=value != 0,
                mode=mode,
                level=backlight.level if mode == MANUAL_BACKLIGHT else 0,
            )
            self.request(BACKLIGHT, 0x10, payload)
        elif name == "dpi":
            self.request(DPI, 0x30, pack(">BH", 0, value))
        elif name == "thumb-invert":
            current = self.request(THUMB_WHEEL, 0x10)
            self.request(THUMB_WHEEL, 0x20, current[0] & 1, value)
        else:
            feature, _read, write = self._wheel_feature()
            if name == "wheel":
                self.request(feature, write, value)
            else:
                self.request(feature, write, 0, 255 if value == 50 else value)
        actual, _choices = self.read_setting(name)
        self.status.values[name] = actual
        if actual != value:
            raise OSError(f"The device reported {actual} after the change.")
        if name == "dpi":
            self.events.dpi = actual

    def step_backlight(self, direction: int) -> None:
        backlight = BacklightConfig.from_report(self.request(BACKLIGHT, 0))
        if not backlight.supported & 0x20:
            raise ValueError("The keyboard firmware does not support software backlight levels.")
        levels, live_level = self.request(BACKLIGHT, 0x20)[:2]
        if levels < 2:
            raise ValueError("The keyboard firmware did not report adjustable backlight levels.")

        current = 0
        if backlight.enabled:
            current = backlight.level if backlight.mode == MANUAL_BACKLIGHT else live_level
        current = min(current, levels - 1)
        target = max(0, min(current + direction, levels - 1))
        if target == current:
            return

        payload = backlight.write_payload(enabled=True, mode=MANUAL_BACKLIGHT, level=target)
        self.request(BACKLIGHT, 0x10, payload)
        actual = BacklightConfig.from_report(self.request(BACKLIGHT, 0))
        self.status.values["backlight"] = actual.setting
        if actual.setting != MANUAL_BACKLIGHT or actual.level != target:
            raise OSError("The keyboard did not confirm the requested backlight level.")

    def toggle_wheel_mode(self) -> None:
        mode, choices = self.read_setting("wheel")
        if mode not in choices:
            raise OSError("The mouse returned an unknown scroll wheel mode.")
        self.status.options["wheel"] = choices
        self.set_setting("wheel", 2 if mode == 1 else 1)

    def reporting(self, cid: int) -> int:
        reply = self.request(REPROG, 0x20, pack(">H", cid))
        received, flags = unpack_from(">HB", reply)
        if received != cid:
            raise OSError("The device reported a different control.")
        return int(flags)

    def set_reporting(self, cid: int, enabled: bool, raw: bool, *, touch_raw: bool) -> None:
        expected = (DIVERTED if enabled else 0) | (RAW_XY if raw else 0)
        mask = DIVERTED | (RAW_XY if touch_raw else 0)
        flags = expected | (mask << 1)
        self.request(REPROG, 0x30, pack(">HBH", cid, flags, 0))
        if self.reporting(cid) & mask != expected:
            raise OSError("The device did not confirm the reporting mode.")

    def set_thumb(self, enabled: bool) -> None:
        report = self.request(THUMB_WHEEL, 0x10)
        self.request(THUMB_WHEEL, 0x20, int(enabled), report[1] & 1)
        if (self.request(THUMB_WHEEL, 0x10)[0] & 1) != int(enabled):
            raise OSError("The device did not confirm the thumb wheel mode.")

    def apply(self, config: Config, output_ready: bool) -> None:
        self.status.errors.clear()
        desired_settings = config.settings[self.model]
        for name in self.hardware_preferences.keys() - desired_settings.keys():
            del self.hardware_preferences[name]
        for name, value in desired_settings.items():
            if name == THUMB_SPEED:
                continue
            if self.hardware_preferences.get(name) != value:
                try:
                    if self.status.values.get(name) != value:
                        self.set_setting(name, value)
                    self.hardware_preferences[name] = value
                except Exception as error:
                    self.status.errors[name] = str(error)
        mappings = config.mappings[self.model]
        if not output_ready and mappings:
            self.status.errors["input"] = "Shortcut output is unavailable."
        for control in mappings.keys() - self.status.controls:
            self.status.errors[control] = "The connected firmware does not expose this control."
        speed = desired_settings.get(THUMB_SPEED)
        if speed is not None:
            if not output_ready:
                self.status.errors[THUMB_SPEED] = "Horizontal scroll output is unavailable."
                speed = None
            elif THUMB_SPEED not in self.status.options:
                self.status.errors[THUMB_SPEED] = "The connected firmware does not expose the thumb wheel."
                speed = None
        self.apply_mappings(
            {key: value for key, value in mappings.items() if key in self.status.controls}
            if output_ready else {},
            thumb_speed=speed,
        )

    def apply_mappings(self, mappings: dict[str, str], *, thumb_speed: int | None = None) -> None:
        gestures = gesture_bindings(mappings)
        thumb = {key: mappings[key] for key in THUMB if key in mappings}
        for control in CONTROLS[self.model]:
            cid = control.cid
            if cid is None:
                continue
            wants_raw = cid == 0x00C3 and bool(gestures)
            action = mappings.get(control.id, "")
            wanted = bool(action or wants_raw)
            if not wanted and cid not in self.owned:
                continue
            touch_raw = (
                cid == 0x00C3
                and any(key in self.status.controls for key in GESTURES[1:])
            )
            try:
                flags = self.reporting(cid)
                if cid not in self.owned and flags & (DIVERTED | PERSISTENT | RAW_XY):
                    raise ValueError("Already diverted by another app. Quit it and power-cycle the device.")
                expected = (DIVERTED if wanted else 0) | (RAW_XY if wants_raw else 0)
                if flags & (DIVERTED | (RAW_XY if touch_raw else 0)) != expected:
                    self.owned.add(cid)
                    try:
                        self.set_reporting(cid, wanted, wants_raw, touch_raw=touch_raw)
                    except Exception:
                        # A timed-out write can still have reached the device.
                        if wanted:
                            self.set_reporting(
                                cid, bool(flags & DIVERTED), bool(flags & RAW_XY),
                                touch_raw=touch_raw,
                            )
                            if not flags & DIVERTED:
                                self.owned.discard(cid)
                        raise
                if not wanted:
                    self.owned.discard(cid)
                self.events.set_button_action(cid, action)
                if cid == 0x00C3:
                    self.events.gestures = gestures
            except Exception as error:
                keys = GESTURES if cid == 0x00C3 else (control.id,)
                for key in keys:
                    self.status.errors[key] = str(error)
        self.apply_thumb(thumb, thumb_speed)

    def apply_thumb(self, mappings: dict[str, str], speed: int | None) -> None:
        scroll = None
        if speed is not None:
            try:
                native, diverted, direction = unpack_from(">HHB", self.request(THUMB_WHEEL, 0))
                if not native or not diverted:
                    raise ValueError("The mouse did not report its thumb-wheel resolution.")
                scroll = ThumbScroll(speed, native, diverted, 1 if direction & 1 else -1)
            except Exception as error:
                self.status.errors[THUMB_SPEED] = str(error)

        wanted = bool(mappings or scroll is not None)
        if not wanted and not self.thumb_owned:
            return
        try:
            diverted = bool(self.request(THUMB_WHEEL, 0x10)[0] & 1)
            if diverted and not self.thumb_owned:
                raise ValueError("Already diverted by another app. Quit it and power-cycle the mouse.")
            if diverted != wanted:
                self.thumb_owned = True
                try:
                    self.set_thumb(wanted)
                except Exception:
                    if not diverted:
                        self.set_thumb(False)
                        self.thumb_owned = False
                    raise
            self.thumb_owned = wanted
            self.events.configure_thumb(mappings, scroll)
        except Exception as error:
            for key in THUMB:
                self.status.errors[key] = str(error)
            if speed is not None or self.events.thumb_scroll is not None:
                self.status.errors[THUMB_SPEED] = str(error)

    def report(
        self, index: int, address: int, data: bytes,
        emit: Callable[[str], None], scroll_horizontal: Callable[[int], None],
    ) -> None:
        if index == self.indexes.get(WIRELESS_STATUS) and address == 0:
            if len(data) >= 3 and data[1]:
                self.dirty = True
            return
        if not self.online:
            return
        if index == self.indexes.get(REPROG):
            self.events.buttons(address, data, emit)
        elif index == self.indexes.get(THUMB_WHEEL) and address == 0:
            self.events.wheel(data, emit, scroll_horizontal)


def dpi_options(report: bytes) -> list[int]:
    values = []
    step = 0
    for offset in range(1, min(15, len(report) - 1), 2):
        value = unpack_from(">H", report, offset)[0]
        if value == 0:
            break
        if value >> 13 == 7:
            step = value & 0x1FFF
        else:
            values.append(value)
    if step and len(values) == 2:
        return list(range(values[0], values[1] + 1, step))
    return values


class Endpoint:
    def __init__(self, source: Any, info: Any) -> None:
        self.source = source
        self.info = info
        self.devices: dict[int, HidDevice] = {}
        self.changed = True
        self.fd = os.open(info.path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)

    def close(self) -> None:
        os.close(self.fd)
        self.source.close()

    def reports(self, emit: Callable[[str], None], scroll_horizontal: Callable[[int], None]) -> None:
        for _ in range(128):
            try:
                report = os.read(self.fd, 64)
            except BlockingIOError:
                break
            if not report:
                raise OSError("The HID endpoint disconnected.")
            if len(report) < 5 or report[0] not in (0x10, 0x11, 0x12):
                continue
            slot, index, address = report[1:4]
            if self.info.isDevice and slot in (0, 0xFF):
                slot = self.source.number
            device = self.devices.get(slot)
            if not self.info.isDevice and index in (0x40, 0x41):
                self.changed = True
                if device:
                    device.dirty = True
            elif device and address & 0x0F == 0 and 0 < index < 0x40:
                device.report(index, address, report[4:], emit, scroll_horizontal)
        else:
            # Read the rest of a busy queue before repeating. A release may be waiting.
            return

        now = monotonic()
        for device in self.devices.values():
            if device.online and not device.dirty:
                device.events.repeat_held(now, emit)


class Hardware:
    def __init__(self) -> None:
        distro_library = Path("/usr/share/solaar/lib")
        if distro_library.is_dir() and str(distro_library) not in sys.path:
            sys.path.append(str(distro_library))
        try:
            import evdev
            from logitech_receiver import base, device, hidpp20, receiver
        except ImportError as error:
            raise RuntimeError(
                "Hardware support needs Solaar and python3-evdev. See hardware setup."
            ) from error

        self.evdev = evdev
        self.base = base
        self.device_module = device
        self.receiver_module = receiver
        self.features = getattr(hidpp20, "SupportedFeature", None) or hidpp20.FEATURE
        self.endpoints: dict[str, Endpoint] = {}
        self.output: InputOutput | None = None
        self.hid_error = ""
        self.output_error = ""
        self.action_error = ""
        self.action_status_changed = False
        self.dictation_commands: set[subprocess.Popen[bytes]] = set()
        self.force = True

    def _open(self, info: Any) -> Any:
        # Ubuntu 24.04 ships Solaar 1.1.11. Later versions inject the transport.
        if info.isDevice:
            if hasattr(self.device_module, "create_device"):
                return self.device_module.create_device(self.base, info)
            return self.device_module.Device.open(info)
        if hasattr(self.receiver_module, "create_receiver"):
            return self.receiver_module.create_receiver(self.base, info)
        return self.receiver_module.Receiver.open(info)

    def scan(self) -> None:
        self.hid_error = ""
        present = set()
        nodes = set(Path("/dev").glob("hidraw*"))
        candidates = self.base.receivers_and_devices() if nodes else ()
        for info in candidates:
            pid = number(info.product_id)
            if Path(info.path) not in nodes:
                continue
            if number(info.vendor_id) != 0x046D:
                continue
            if info.isDevice:
                if number(info.bus_id) != 5 or pid not in DEVICE_IDS:
                    continue
            elif pid != BOLT or info.interface != 2:
                continue
            present.add(info.path)
            if info.path in self.endpoints:
                continue
            source = None
            try:
                source = self._open(info)
                if source is None:
                    raise OSError(f"Cannot open {info.path}.")
                self.endpoints[info.path] = Endpoint(source, info)
                self.force = True
            except Exception as error:
                if source is not None:
                    source.close()
                self.hid_error = str(error)
                LOG.debug("Opening %s failed: %s", info.path, error)
        for path in self.endpoints.keys() - present:
            self.endpoints.pop(path).close()
            self.force = True
        if not nodes:
            self.hid_error = "Linux HID device nodes are unavailable. Run the hardware check from your desktop."

    def discover(self, endpoint: Endpoint, *, reload_settings: bool = False) -> None:
        candidates = [endpoint.source] if endpoint.info.isDevice else list(endpoint.source)
        seen = set()
        for candidate in candidates:
            pid = number(candidate.product_id if endpoint.info.isDevice else candidate.wpid)
            model = DEVICE_IDS.get(pid)
            if not model:
                continue
            slot = candidate.number
            seen.add(slot)
            if slot not in endpoint.devices:
                transport = "Bluetooth" if endpoint.info.isDevice else "Logi Bolt"
                endpoint.devices[slot] = HidDevice(candidate, self.features, model, transport)
            device = endpoint.devices[slot]
            try:
                was_online = device.online
                device.online = bool(candidate.ping())
                if not device.online:
                    device.events.reset_buttons()
                    device.dirty = True
                elif not was_online or device.dirty:
                    device.initialize()
                    device.index(WIRELESS_STATUS)
                    device.dirty = False
                    self.force = True
                elif reload_settings:
                    device.refresh_settings()
                    device.hardware_preferences.clear()
                else:
                    device.refresh_battery()
            except Exception as error:
                device.online = False
                device.dirty = True
                device.events.reset_buttons()
                LOG.debug("Discovering %s failed: %s", model, error)
        for slot in endpoint.devices.keys() - seen:
            del endpoint.devices[slot]
        endpoint.changed = False

    def refresh(self, config: Config, *, reload_settings: bool = False) -> Snapshot:
        self.scan()
        if self.output is None:
            try:
                self.output = InputOutput(self.evdev)
                self.output_error = ""
                self.force = True
            except Exception as error:
                self.output_error = f"Shortcut output unavailable ({error}). See hardware setup."
        for endpoint in self.endpoints.values():
            try:
                self.discover(endpoint, reload_settings=reload_settings)
            except Exception as error:
                self.hid_error = str(error)
                LOG.debug("Cannot enumerate %s: %s", endpoint.info.path, error)
        if self.force:
            self.apply(config)
        return self.snapshot()

    def apply(self, config: Config) -> None:
        self.action_error = ""
        handled = set()
        for endpoint in self.endpoints.values():
            for device in endpoint.devices.values():
                if device.online:
                    if device.model in handled:
                        device.apply_mappings({})
                        continue
                    handled.add(device.model)
                    device.apply(config, self.output is not None)
        self.force = False

    def perform_action(self, action: str) -> None:
        if action == DICTATION_ACTION:
            self._toggle_dictation()
            return
        if action in MONITOR_APPLICATION_STEPS:
            self._switch_monitor_application(MONITOR_APPLICATION_STEPS[action])
            return
        if action in BACKLIGHT_STEPS:
            model = "keyboard"
        elif action == WHEEL_TOGGLE_ACTION:
            model = "mouse"
        else:
            if self.output:
                self.output.emit(action)
            return

        self.action_status_changed = True
        target = next(
            (device for endpoint in self.endpoints.values() for device in endpoint.devices.values()
             if device.model == model and device.online),
            None,
        )
        if target is None:
            self.action_error = f"Wake or connect your {DEVICE_NAMES[model]} to use {action_label(action)}."
            return
        try:
            if model == "keyboard":
                target.step_backlight(BACKLIGHT_STEPS[action])
            else:
                target.toggle_wheel_mode()
        except Exception as error:
            self.action_error = f"{action_label(action)} failed. {error}"
            LOG.debug("Device action %s failed", action, exc_info=True)
        else:
            self.action_error = ""

    def _switch_monitor_application(self, step: int) -> None:
        self.action_status_changed = True
        try:
            with closing(MonitorApplicationSwitcher()) as switcher:
                switcher.switch(step)
        except Exception as error:
            self.action_error = str(error)
            LOG.debug("Cursor-monitor application switch failed", exc_info=True)
        else:
            self.action_error = ""

    def _toggle_dictation(self) -> None:
        self.action_status_changed = True
        try:
            process = subprocess.Popen(
                ["handy", "--toggle-transcription"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError:
            self.action_error = "Install and open Handy to use Dictation. See the link in the Action panel."
        except OSError as error:
            self.action_error = f"Could not start Handy Dictation. {error}"
        else:
            self.dictation_commands.add(process)
            self.action_error = ""

    def scroll_horizontal(self, units: int) -> None:
        if self.output:
            self.output.scroll_horizontal(units)

    def pump(self) -> bool:
        self.action_status_changed = False
        for process in tuple(self.dictation_commands):
            result = process.poll()
            if result is not None:
                self.dictation_commands.remove(process)
                if result != 0:
                    self.action_error = f"Handy Dictation exited with status {result}. Open Handy and check its setup."
                    self.action_status_changed = True
        for path, endpoint in list(self.endpoints.items()):
            try:
                endpoint.reports(self.perform_action, self.scroll_horizontal)
            except OutputError as error:
                self.release_mappings()
                if self.output:
                    self.output.close()
                self.output = None
                self.output_error = str(error)
                self.force = True
            except OSError as error:
                LOG.debug("Reading %s failed: %s", path, error)
                self.endpoints.pop(path).close()
                self.force = True
        return self.action_status_changed

    def snapshot(self) -> Snapshot:
        status = Snapshot(
            message=" ".join(filter(None, (self.hid_error, self.output_error, self.action_error))),
            output_ready=self.output is not None,
            receivers=sum(not endpoint.info.isDevice for endpoint in self.endpoints.values()),
        )
        for endpoint in self.endpoints.values():
            for device in endpoint.devices.values():
                if device.online and device.model not in status.devices:
                    status.devices[device.model] = device.status
        return status

    def release_mappings(self) -> None:
        for endpoint in self.endpoints.values():
            for device in endpoint.devices.values():
                if device.online:
                    device.events.reset_buttons()
                    device.apply_mappings({})

    def close(self) -> None:
        self.release_mappings()
        for endpoint in self.endpoints.values():
            endpoint.close()
        self.endpoints.clear()
        if self.output:
            self.output.close()

    def diagnostic(self) -> tuple[str, bool]:
        self.scan()
        for endpoint in self.endpoints.values():
            self.discover(endpoint)
        status = self.snapshot()
        lines = [f"Accessible Logi Bolt receivers: {status.receivers}"]
        if status.message:
            lines.append(status.message)
        for model, label in DEVICE_NAMES.items():
            if model in status.devices:
                device = status.devices[model]
                lines.append(f"{label}: {device.transport}, {len(device.controls)} customizable controls")
            else:
                lines.append(f"{label}: no awake connection found")
        try:
            fd = os.open("/dev/uinput", os.O_WRONLY | os.O_CLOEXEC)
        except OSError as error:
            lines.append(f"/dev/uinput: {error.strerror}")
            writable = False
        else:
            os.close(fd)
            lines.append("/dev/uinput: writable")
            writable = True
        return "\n".join(lines), writable and status.devices.keys() == DEVICE_NAMES.keys()
