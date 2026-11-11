from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from struct import pack, unpack_from
from subprocess import DEVNULL
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mx_studio.applications import ApplicationWindow, Bounds, choose_application_window
from mx_studio.hardware import (
    BACKLIGHT,
    BATTERY,
    REPROG,
    SMARTSHIFT,
    SMARTSHIFT_ENHANCED,
    THUMB_WHEEL,
    Endpoint,
    Hardware,
    HidDevice,
    dpi_options,
)
from mx_studio.input import (
    REPEAT_DELAY,
    REPEAT_INTERVAL,
    ControlEvents,
    ThumbScroll,
    gesture_bindings,
)
from mx_studio.models import (
    CONTROLS,
    DICTATION_ACTION,
    KEYBOARD_ACTIONS,
    THUMB_SPEED,
    WHEEL_TOGGLE_ACTION,
    Config,
    shortcut_keys,
    validate_action,
)


class ProfileTests(unittest.TestCase):
    def test_linux_keyboard_shortcuts_fill_only_unassigned_available_controls(self) -> None:
        config = Config()
        config.mappings["keyboard"] = {"volume-down": "shortcut:Ctrl+J", "mute": "disabled"}
        config.mappings["mouse"]["back"] = "key:Copy"
        available = {"volume-down", "volume-up", "mute", "dictation"}
        shortcuts = config.missing_linux_keyboard_shortcuts(available)
        self.assertEqual(shortcuts, {"volume-up": "key:VolumeUp"})
        config.mappings["keyboard"].update(shortcuts)
        self.assertEqual(config.missing_linux_keyboard_shortcuts(available), {})
        self.assertEqual(config.mappings["keyboard"]["volume-down"], "shortcut:Ctrl+J")
        self.assertEqual(config.mappings["keyboard"]["mute"], "disabled")
        self.assertEqual(config.mappings["mouse"], {"back": "key:Copy"})

    def test_every_keyboard_control_has_an_action_and_dictation_is_opt_in(self) -> None:
        self.assertEqual(KEYBOARD_ACTIONS.keys(), {control.id for control in CONTROLS["keyboard"]})
        for action in KEYBOARD_ACTIONS.values():
            validate_action(action)
        shortcuts = Config().missing_linux_keyboard_shortcuts(None)
        self.assertEqual(shortcuts.keys(), KEYBOARD_ACTIONS.keys() - {"dictation"})

    def test_profile_round_trip_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings" / "profile.json"
            config = Config.load(path)
            config.mappings["keyboard"]["dictation"] = DICTATION_ACTION
            config.mappings["mouse"]["back"] = "backlight:down"
            config.settings["mouse"]["dpi"] = 1200
            config.settings["mouse"][THUMB_SPEED] = 150
            config.save(path)
            self.assertEqual(Config.load(path), config)
            config.mappings["keyboard"].clear()
            config.save(path)
            self.assertEqual(Config.load(path).mappings["keyboard"], {})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_bad_profile_is_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            text = '{"version": 1, "mappings": {"mouse": {"back": "arbitrary command"}}}'
            path.write_text(text)
            with self.assertRaises(ValueError):
                Config.load(path)
            self.assertEqual(path.read_text(), text)

    def test_shortcuts(self) -> None:
        self.assertEqual(
            shortcut_keys("Ctrl + Alt + T"), ("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_T")
        )
        self.assertEqual(shortcut_keys("Shift+Print"), ("KEY_LEFTSHIFT", "KEY_SYSRQ"))
        for invalid in ("Ctrl+Ctrl+T", "Left+T", "Ctrl+", "Ctrl+F25"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                shortcut_keys(invalid)


class EventTests(unittest.TestCase):
    def test_held_keys_fire_once_and_simultaneous_keys_fire(self) -> None:
        emitted: list[str] = []
        events = ControlEvents(actions={3: "key:Copy", 4: "key:Paste"})
        for state in ((3, 4, 0, 0), (3, 4, 0, 0), (4, 0, 0, 0), (3, 4, 0, 0)):
            events.buttons(0, pack(">4H", *state), emitted.append)
        self.assertEqual(emitted, ["key:Copy", "key:Paste", "key:Copy"])

    def test_held_adjustments_repeat_without_retriggering_tap_actions(self) -> None:
        emitted: list[str] = []
        events = ControlEvents(actions={
            1: "key:VolumeUp", 2: "key:BrightnessDown", 3: "backlight:up", 4: DICTATION_ACTION,
        })
        with patch("mx_studio.input.monotonic", return_value=0.0):
            events.buttons(0, pack(">4H", 1, 2, 3, 4), emitted.append)
        events.repeat_held(REPEAT_DELAY - 0.001, emitted.append)
        self.assertEqual(emitted, ["key:VolumeUp", "key:BrightnessDown", "backlight:up", DICTATION_ACTION])
        events.repeat_held(REPEAT_DELAY, emitted.append)
        events.repeat_held(REPEAT_DELAY + REPEAT_INTERVAL, emitted.append)
        self.assertEqual(emitted[4:], ["key:VolumeUp", "key:BrightnessDown", "backlight:up"] * 2)

    def test_repeat_stops_after_a_binding_change_or_device_reset(self) -> None:
        emitted: list[str] = []
        events = ControlEvents(actions={1: "key:VolumeDown", 2: "key:BrightnessUp"})
        with patch("mx_studio.input.monotonic", return_value=0.0):
            events.buttons(0, pack(">4H", 1, 2, 0, 0), emitted.append)
        events.set_button_action(1, "key:BrightnessDown")
        events.repeat_held(1.0, emitted.append)
        events.reset_buttons()
        events.repeat_held(2.0, emitted.append)
        self.assertEqual(emitted, ["key:VolumeDown", "key:BrightnessUp", "key:BrightnessUp"])

    def test_receiver_pump_repeats_held_button_and_reads_release_first(self) -> None:
        control_id = 0x00E9
        device = HidDevice(SimpleNamespace(), lambda value: value, "keyboard", "Logi Bolt")
        device.online = True
        device.dirty = False
        device.indexes[REPROG] = 9
        device.events.set_button_action(control_id, "key:VolumeUp")
        endpoint = Endpoint.__new__(Endpoint)
        endpoint.info = SimpleNamespace(isDevice=False)
        endpoint.devices = {1: device}
        endpoint.fd = 0
        header = bytes((0x11, 1, 9, 0))
        press = header + pack(">4H", control_id, 0, 0, 0)
        release = header + bytes(8)
        emitted: list[str] = []
        with (
            patch("mx_studio.input.monotonic", return_value=0.0),
            patch("mx_studio.hardware.monotonic", side_effect=[0.0, REPEAT_DELAY, 1.0]),
            patch("mx_studio.hardware.os.read", side_effect=[
                press, BlockingIOError(), BlockingIOError(), release, BlockingIOError(),
            ]),
        ):
            for _ in range(3):
                endpoint.reports(emitted.append, Mock())
        self.assertEqual(emitted, ["key:VolumeUp", "key:VolumeUp"])

    def test_raw_motion_uses_dominant_direction_on_release(self) -> None:
        emitted: list[str] = []
        events = ControlEvents(gestures={"gesture-left": "key:PreviousTab"})
        events.buttons(0, pack(">4H", 0xC3, 0, 0, 0), emitted.append)
        events.buttons(0x10, pack(">hh", 2600, 2600), emitted.append)
        events.buttons(0x10, pack(">hh", -530, 125), emitted.append)
        self.assertFalse(emitted)
        events.buttons(0, bytes(8), emitted.append)
        self.assertEqual(emitted, ["key:PreviousTab"])

    def test_gesture_tap_and_unassigned_direction(self) -> None:
        emitted: list[str] = []
        events = ControlEvents(gestures={"gesture-right": "key:NextTab"}, skip_first_motion=False)
        events.buttons(0, pack(">4H", 0xC3, 0, 0, 0), emitted.append)
        events.buttons(0, bytes(8), emitted.append)
        events.buttons(0, pack(">4H", 0xC3, 0, 0, 0), emitted.append)
        events.buttons(0x10, pack(">hh", -500, 0), emitted.append)
        events.buttons(0, bytes(8), emitted.append)
        self.assertEqual(emitted, ["key:Overview", ""])

    def test_tap_alone_uses_button_press(self) -> None:
        self.assertEqual(gesture_bindings({"gesture-tap": "key:Overview"}), {})

    def test_thumb_wheel_accumulates_and_keeps_other_direction(self) -> None:
        emitted: list[str] = []
        events = ControlEvents(thumb={"thumb-up": "key:VolumeUp"})
        for delta, phase in ((-6, 1), (-4, 2), (20, 1)):
            events.wheel(pack(">hBBB", delta, 0, 0, phase), emitted.append, Mock())
        self.assertEqual(emitted, ["key:VolumeUp", "pointer:right-scroll", "pointer:right-scroll"])

    def test_horizontal_speed_uses_device_resolution_and_keeps_fractional_ticks(self) -> None:
        emitted: list[int] = []
        events = ControlEvents(thumb_scroll=ThumbScroll(75, 18, 120, -1))
        action = Mock()
        for delta, phase in ((1, 1), (1, 1), (-1, 1), (-1, 2)):
            events.wheel(pack(">hBBB", delta, 0, 0, phase), action, emitted.append)
        self.assertEqual(emitted, [-13, -14, 13, 14])
        action.assert_not_called()

    def test_horizontal_speed_leaves_shortcut_counts_and_scales_explicit_scroll(self) -> None:
        actions: list[str] = []
        scroll: list[int] = []
        events = ControlEvents(
            thumb={"thumb-up": "key:VolumeUp", "thumb-down": "pointer:right-scroll"},
            thumb_scroll=ThumbScroll(200, 18, 120, -1),
        )
        events.wheel(pack(">hBBB", -10, 0, 0, 1), actions.append, scroll.append)
        events.wheel(pack(">hBBB", 10, 0, 0, 1), actions.append, scroll.append)
        self.assertEqual(actions, ["key:VolumeUp"])
        self.assertEqual(scroll, [360])

    def test_dpi_list_and_range(self) -> None:
        self.assertEqual(dpi_options(pack(">BHHHH", 0, 400, 800, 1200, 0)), [400, 800, 1200])
        self.assertEqual(dpi_options(pack(">BHHHH", 0, 400, 0xE032, 550, 0)), [400, 450, 500, 550])


class FakeFeatures(dict[int, int]):
    def get_feature_version(self, _feature: int) -> int:
        return 5


class FakeMouse:
    def __init__(self) -> None:
        self.features = FakeFeatures({REPROG: 9, BATTERY: 8, THUMB_WHEEL: 16})
        self.keys = [SimpleNamespace(key=0xC3, flags=iter(("mse", "divertable", "raw XY")))]
        self.flags = 0
        self.thumb = 0
        self.thumb_status = 0
        self.thumb_resolution = pack(">HHB", 18, 120, 0)
        self.writes = 0
        self.fail_confirmation = False

    def feature_request(self, feature: int, function: int, *params: int | bytes) -> bytes:
        if feature == REPROG:
            packet = params[0]
            assert isinstance(packet, bytes)
            cid = int(unpack_from(">H", packet)[0])
            assert cid == 0xC3
            if function == 0x30:
                flags = packet[2]
                mask = (flags >> 1) & 0x11
                self.flags = (self.flags & ~mask) | (flags & mask)
                self.writes += 1
                if self.fail_confirmation:
                    self.fail_confirmation = False
                    raise OSError("Lost confirmation")
            return pack(">HBH", cid, self.flags, 0)
        if feature == THUMB_WHEEL:
            if function == 0:
                return self.thumb_resolution
            if function == 0x20:
                value, inverted = params
                assert isinstance(value, int) and isinstance(inverted, int) and inverted in (0, 1)
                self.thumb = value
                self.thumb_status = (self.thumb_status & ~1) | inverted
                self.writes += 1
            return bytes((self.thumb, self.thumb_status))
        return bytes((75, 0, 0, 0))


class HardwareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mouse = FakeMouse()
        self.device = HidDevice(self.mouse, lambda feature: feature, "mouse", "Logi Bolt")
        self.device.initialize()

    def test_capabilities_from_solaar_111_and_restore_after_remap(self) -> None:
        self.assertIn("gesture-right", self.device.status.controls)
        self.assertEqual(self.device.status.battery, 75)
        config = Config()
        config.mappings["mouse"]["gesture-right"] = "key:NextTab"
        config.mappings["mouse"]["thumb-up"] = "key:VolumeUp"
        self.device.apply(config, output_ready=True)
        self.assertEqual((self.mouse.flags, self.mouse.thumb), (0x11, 1))
        self.assertEqual(self.device.events.gestures, {"gesture-right": "key:NextTab"})
        self.device.apply_mappings({})
        self.assertEqual((self.mouse.flags, self.mouse.thumb), (0, 0))
        self.assertFalse(self.device.events.gestures)

    def test_skip_diversion_when_uinput_is_unavailable(self) -> None:
        config = Config()
        config.mappings["mouse"]["gesture-tap"] = "key:Overview"
        self.device.apply(config, output_ready=False)
        self.assertEqual(self.mouse.writes, 0)
        self.assertIn("input", self.device.status.errors)

    def test_speed_diverts_and_device_default_restores_native_horizontal_scroll(self) -> None:
        config = Config()
        config.settings["mouse"][THUMB_SPEED] = 150
        self.mouse.thumb_status = 0b111  # inverted, touched, and in proximity
        self.device.apply(config, output_ready=True)
        self.assertEqual(self.mouse.thumb, 1)
        self.assertEqual(self.mouse.thumb_status & 1, 1)
        self.assertEqual(self.device.events.thumb_scroll, ThumbScroll(150, 18, 120, -1))
        config.settings["mouse"].clear()
        self.device.apply(config, output_ready=True)
        self.assertEqual(self.mouse.thumb, 0)
        self.assertEqual(self.mouse.thumb_status & 1, 1)
        self.assertIsNone(self.device.events.thumb_scroll)

    def test_speed_with_missing_resolution_or_output_leaves_wheel_native(self) -> None:
        config = Config()
        config.settings["mouse"][THUMB_SPEED] = 150
        self.device.apply(config, output_ready=False)
        self.assertIn("output is unavailable", self.device.status.errors[THUMB_SPEED])
        self.mouse.thumb_resolution = pack(">HHB", 18, 0, 0)
        self.device.apply(config, output_ready=True)
        self.assertIn("resolution", self.device.status.errors[THUMB_SPEED])
        self.assertEqual(self.mouse.writes, 0)

    def test_existing_diversion_is_left_alone(self) -> None:
        self.mouse.flags = 1
        self.device.apply_mappings({"gesture-tap": "key:Overview"})
        self.assertEqual(self.mouse.writes, 0)
        self.assertIn("Already diverted", self.device.status.errors["gesture-tap"])

    def test_timed_out_diversion_write_is_rolled_back(self) -> None:
        self.mouse.fail_confirmation = True
        self.device.apply_mappings({"gesture-tap": "key:Overview"})
        self.assertEqual(self.mouse.flags, 0)
        self.assertFalse(self.device.owned)
        self.assertIn("Lost confirmation", self.device.status.errors["gesture-tap"])


class FakeBacklight:
    def __init__(self) -> None:
        self.features = FakeFeatures({BACKLIGHT: 12})
        self.enabled = 1
        self.options = 0x0D
        self.supported = 0x28
        self.stored_level = 5
        self.live_level = 3
        self.durations = pack("<HHHH", 9, 11, 60, 120)
        self.writes: list[bytes] = []

    def feature_request(self, feature: int, function: int, *params: bytes) -> bytes:
        assert feature == BACKLIGHT
        if function == 0x20:
            return bytes((8, self.live_level))
        if function == 0x10:
            payload = params[0]
            self.writes.append(payload)
            self.enabled, self.options, _effect, self.stored_level = payload[:4]
            self.durations = payload[4:]
        return pack(
            "<BBBHB", self.enabled, self.options, self.supported, 1, self.stored_level
        ) + self.durations


class BacklightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.firmware = FakeBacklight()
        self.keyboard = HidDevice(self.firmware, lambda feature: feature, "keyboard", "Logi Bolt")

    def test_step_from_automatic_uses_live_level_and_preserves_timeouts(self) -> None:
        durations = self.firmware.durations
        self.keyboard.step_backlight(1)
        self.assertEqual(self.firmware.writes, [bytes((1, 0x1D, 0xFF, 4)) + durations])
        self.assertEqual(self.keyboard.status.values["backlight"], 3)

    def test_manual_levels_clamp_even_when_the_backlight_has_timed_out(self) -> None:
        self.firmware.options = 0x1D
        self.firmware.stored_level = 7
        self.firmware.live_level = 0
        self.keyboard.step_backlight(1)
        self.assertEqual(self.firmware.writes, [])
        self.keyboard.step_backlight(-1)
        self.assertEqual(self.firmware.stored_level, 6)
        self.firmware.stored_level = 1
        self.keyboard.step_backlight(-1)
        self.keyboard.step_backlight(-1)
        self.assertEqual([payload[3] for payload in self.firmware.writes], [6, 0])

    def test_up_enables_backlight_at_the_first_level_when_off(self) -> None:
        self.firmware.enabled = 0
        self.keyboard.step_backlight(1)
        self.assertEqual((self.firmware.enabled, self.firmware.stored_level), (1, 1))

    def test_mouse_button_routes_to_keyboard_and_reports_an_unsupported_keyboard(self) -> None:
        mouse = HidDevice(FakeMouse(), lambda feature: feature, "mouse", "Logi Bolt")
        mouse.online = True
        mouse.initialize()
        mouse.apply_mappings({"gesture-tap": "backlight:down"})
        self.keyboard.online = True

        endpoint = Mock(spec=Endpoint)
        endpoint.devices = {1: self.keyboard, 2: mouse}
        endpoint.reports.side_effect = lambda emit, scroll: mouse.report(
            9, 0, pack(">4H", 0xC3, 0, 0, 0), emit, scroll
        )
        hardware = Hardware.__new__(Hardware)
        hardware.endpoints = {"bolt": endpoint}
        hardware.dictation_commands = set()

        self.assertTrue(hardware.pump())
        self.assertEqual(self.firmware.stored_level, 2)
        self.assertEqual(hardware.action_error, "")

        mouse.events.pressed.clear()
        self.firmware.supported = 0x08
        self.assertTrue(hardware.pump())
        self.assertIn("does not support software backlight levels", hardware.action_error)
        self.assertEqual(len(self.firmware.writes), 1)


class MonitorApplicationTests(unittest.TestCase):
    def test_application_order_is_monitor_local_and_independent_of_stacking(self) -> None:
        left = Bounds(0, 0, 1920, 1080)
        right = Bounds(1920, 0, 1920, 1080)
        windows = [
            ApplicationWindow(1, "browser", left),
            ApplicationWindow(2, "browser", right),
            ApplicationWindow(3, "editor", left),
            ApplicationWindow(4, "editor", left),
            ApplicationWindow(5, "terminal", left),
            ApplicationWindow(6, "files", Bounds(1800, 0, 700, 400)),
        ]
        # Window 6 crosses the monitor boundary and belongs to the right monitor.
        stacking = [5, 3, 1, 4, 2, 6]
        for monitor, active, step, expected in (
            (left, 1, 1, 4), (left, 4, 1, 5), (left, 5, 1, 1), (left, 1, -1, 5),
            (left, 2, 1, 4), (right, 1, -1, 6), (right, 6, -1, 2),
        ):
            with self.subTest(monitor=monitor, active=active, step=step):
                self.assertEqual(
                    choose_application_window(windows, [left, right], monitor, stacking, active, step),
                    expected,
                )

    def test_empty_monitor_and_single_focused_app(self) -> None:
        left = Bounds(0, 0, 1920, 1080)
        right = Bounds(1920, 0, 1920, 1080)
        windows = [ApplicationWindow(1, "editor", left), ApplicationWindow(2, "editor", left)]
        self.assertIsNone(choose_application_window(windows, [left, right], right, [1, 2], 1, 1))
        self.assertEqual(choose_application_window(windows, [left, right], left, [1, 2], 1, 1), 1)


class AdditionalActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hardware = Hardware.__new__(Hardware)
        self.hardware.endpoints = {}
        self.hardware.dictation_commands = set()

    def test_monitor_application_actions_dispatch_from_left_and_right_gestures(self) -> None:
        mappings = {
            "gesture-left": "application:previous-on-monitor",
            "gesture-right": "application:next-on-monitor",
        }
        for action in mappings.values():
            validate_action(action)
        events = ControlEvents(gestures=gesture_bindings(mappings), skip_first_motion=False)
        emit = self.hardware.perform_action
        with patch("mx_studio.hardware.MonitorApplicationSwitcher") as factory:
            for delta in (-500, 500):
                events.buttons(0, pack(">4H", 0xC3, 0, 0, 0), emit)
                events.buttons(0x10, pack(">hh", delta, 0), emit)
                events.buttons(0, bytes(8), emit)
        self.assertEqual([args.args for args in factory.return_value.switch.call_args_list], [(-1,), (1,)])
        self.assertEqual(factory.return_value.close.call_count, 2)
        self.assertEqual(self.hardware.action_error, "")

    def test_dictation_uses_handys_toggle_command_and_reports_provider_failure(self) -> None:
        process = Mock()
        process.poll.side_effect = [None, 1]
        with patch("mx_studio.hardware.subprocess.Popen", return_value=process) as popen:
            self.hardware.perform_action(DICTATION_ACTION)
        popen.assert_called_once_with(
            ["handy", "--toggle-transcription"], stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
            start_new_session=True,
        )
        self.assertFalse(self.hardware.pump())
        self.assertTrue(self.hardware.pump())
        self.assertIn("exited with status 1", self.hardware.action_error)
        self.assertFalse(self.hardware.dictation_commands)

    def test_dictation_explains_missing_provider(self) -> None:
        with patch("mx_studio.hardware.subprocess.Popen", side_effect=FileNotFoundError):
            self.hardware.perform_action(DICTATION_ACTION)
        self.assertIn("Install and open Handy", self.hardware.action_error)

    def test_wheel_toggle_uses_live_mode_and_preserves_other_fields(self) -> None:
        for feature, index, getter, setter in (
            (SMARTSHIFT, 14, 0x00, 0x10), (SMARTSHIFT_ENHANCED, 15, 0x10, 0x20),
        ):
            with self.subTest(feature=feature):
                mode = 2

                def feature_request(identifier: int, function: int, *params: int) -> bytes:
                    nonlocal mode
                    self.assertEqual(identifier, feature)
                    if function == setter:
                        self.assertEqual(params, (3 - mode,))
                        mode = params[0]
                    else:
                        self.assertEqual(function, getter)
                        self.assertEqual(params, ())
                    return bytes((mode, 20, 35))

                features = FakeFeatures({SMARTSHIFT_ENHANCED: 0, feature: index})
                source = SimpleNamespace(features=features, feature_request=feature_request)
                mouse = HidDevice(source, lambda value: value, "mouse", "Logi Bolt")
                mouse.online = True
                endpoint = Mock(spec=Endpoint)
                endpoint.devices = {1: mouse}
                self.hardware.endpoints = {"bolt": endpoint}
                for expected in (1, 2):
                    self.hardware.perform_action(WHEEL_TOGGLE_ACTION)
                    self.assertEqual(self.hardware.action_error, "")
                    self.assertEqual(mouse.status.values["wheel"], expected)
