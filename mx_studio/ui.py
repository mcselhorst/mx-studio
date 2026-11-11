from __future__ import annotations

import signal
from pathlib import Path
from typing import Any

from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from .__main__ import APP_ID, ICON_THEME, autostart_path, desktop_entry
from .diagram import Diagram
from .hardware import DeviceStatus, Snapshot
from .models import (
    CONTROLS,
    DEVICE_NAMES,
    DEVICE_SETTINGS,
    DICTATION_ACTION,
    MONITOR_APPLICATION_STEPS,
    PRESETS,
    SETTING_CHOICES,
    SETTING_LABELS,
    THUMB_SPEED,
    Config,
    action_label,
    config_home,
    validate_action,
)
from .worker import Worker

DESCRIPTIONS = {
    "keyboard": "Customize the function row and dedicated shortcut keys.",
    "mouse": "Customize the wheels, side buttons and thumb gestures.",
}
DEVICE_ICONS = {"keyboard": "input-keyboard-symbolic", "mouse": "input-mouse-symbolic"}
SETTING_DETAILS = {
    "fn": "Choose what the F1–F12 row sends without holding Fn.",
    "backlight": "Choose an illumination mode reported by the keyboard.",
    "dpi": "Set how far the cursor travels for the same mouse movement.",
    "wheel": "Choose free-spin or the wheel’s ratchet and automatic switching mode.",
    "smartshift": "Set how fast the wheel must spin to switch modes. 50 keeps the ratchet on.",
    THUMB_SPEED: "Adjust how far the thumb wheel scrolls sideways.",
    "thumb-invert": "Reverse the horizontal wheel’s scroll direction.",
}
CUSTOM = "custom"
ACTION_IDS = tuple(PRESETS) + (CUSTOM,)
ACTION_LABELS = tuple(PRESETS.values()) + ("Custom keyboard shortcut…",)


def box(vertical: bool = False, spacing: int = 0, css: str = "") -> Any:
    orientation = Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL
    item = Gtk.Box(orientation=orientation, spacing=spacing)
    if css:
        item.add_css_class(css)
    return item


def label(text: str, css: str = "", *, wrap: bool = False) -> Any:
    item = Gtk.Label(label=text, xalign=0)
    if wrap:
        item.set_wrap(True)
        item.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if css:
        item.add_css_class(css)
    return item


def icon(name: str, size: int = 18) -> Any:
    item = Gtk.Image.new_from_icon_name(name)
    item.set_pixel_size(size)
    return item


def remove_children(container: Any) -> None:
    while child := container.get_first_child():
        container.remove(child)


def scroll(content: Any) -> Any:
    item = Gtk.ScrolledWindow()
    item.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    item.set_vexpand(True)
    item.set_child(content)
    return item


class Studio(Gtk.Application):  # type: ignore[misc]
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        )
        self.add_main_option(
            "background", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            "Keep remaps running without a window", None,
        )
        self.path = config_home() / "mx-studio" / "profile.json"
        self.config_error = ""
        try:
            self.config = Config.load(self.path)
        except (ValueError, OSError) as error:
            self.config = Config()
            self.config_error = f"Profile could not be read. Repair {self.path} and reopen MX Studio. {error}"
        self.snapshot = Snapshot()
        self.worker: Worker | None = None
        self.window: Any = None
        self.model = "keyboard"
        self.selected = {"keyboard": "dictation", "mouse": "gesture-tap"}
        self.nav: dict[str, Any] = {}
        self.nav_status: dict[str, Any] = {}
        self.rows: dict[str, tuple[Any, Any]] = {}
        self.setting_rows: dict[str, tuple[Any, Any, list[int | None], Any]] = {}
        self.loading = False
        self.pending = False
        self.changed_notice = ""

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_path(str(Path(__file__).with_name("style.css")))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        settings = Gtk.Settings.get_default()
        if settings:
            settings.set_property("gtk-application-prefer-dark-theme", False)
        self.add_action(self._action("quit", self._quit))
        self.add_action(self._action("setup", self._setup))
        self.add_action(self._action("refresh", self._refresh))
        auto = Gio.SimpleAction.new_stateful(
            "autostart", None, GLib.Variant.new_boolean(autostart_path().exists())
        )
        auto.connect("change-state", self._autostart)
        self.add_action(auto)
        self.set_accels_for_action("app.quit", ["<Primary>q"])
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._quit)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self._quit)
        self.worker = Worker(self.config, self._publish)
        self.hold()

    def do_command_line(self, command: Any) -> int:
        if not command.get_options_dict().contains("background"):
            self.activate()
        return 0

    def do_activate(self) -> None:
        if self.window is None:
            self._build()
        self.window.present()

    def do_shutdown(self) -> None:
        if self.worker:
            self.worker.close()
        Gtk.Application.do_shutdown(self)

    @staticmethod
    def _action(name: str, callback: Any) -> Any:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        return action

    def _quit(self, *_args: Any) -> bool:
        self.quit()
        return False

    def _refresh(self, *_args: Any) -> None:
        if self.worker:
            self.worker.refresh()
        if self.window:
            self.changed_notice = "Checking for devices…"
            self._update_status()
            GLib.timeout_add(2500, self._clear_notice)

    def _clear_notice(self) -> bool:
        if self.changed_notice == "Checking for devices…":
            self.changed_notice = ""
            self._update_status()
        return False

    def _autostart(self, action: Any, value: Any) -> None:
        try:
            path = autostart_path()
            if value.get_boolean():
                desktop_entry(path, background=True)
            else:
                path.unlink(missing_ok=True)
            action.set_state(value)
        except OSError as error:
            self.changed_notice = f"Unable to update start at login. {error}"
            self._update_status()

    def _publish(self, snapshot: Snapshot) -> None:
        GLib.idle_add(self._receive, snapshot)

    def _receive(self, snapshot: Snapshot) -> bool:
        self.snapshot = snapshot
        self.pending = False
        if self.changed_notice == "Checking for devices…":
            self.changed_notice = ""
        if self.window:
            self._update_status()
            self._update_row_status()
            self._update_settings()
            self._update_inspector_note()
        return False

    def _build(self) -> None:
        self.window = Gtk.ApplicationWindow(application=self, title="MX Studio")
        Gtk.IconTheme.get_for_display(self.window.get_display()).add_search_path(str(ICON_THEME))
        self.window.set_icon_name(APP_ID)
        self.window.set_default_size(1200, 798)
        self.window.set_hide_on_close(True)
        self.window.add_css_class("studio")
        titlebar = Gtk.HeaderBar()
        titlebar.add_css_class("titlebar")
        titlebar.set_show_title_buttons(False)
        titlebar.set_title_widget(label("MX Studio", "window-title"))
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh.set_tooltip_text("Check device connections")
        refresh.set_action_name("app.refresh")
        titlebar.pack_start(refresh)
        menu = Gio.Menu()
        menu.append("Hardware setup", "app.setup")
        menu.append("Start at login", "app.autostart")
        menu.append("Quit MX Studio", "app.quit")
        options = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        options.set_tooltip_text("MX Studio menu")
        window_controls = box(spacing=5)
        minimize = Gtk.Button.new_from_icon_name("window-minimize-symbolic")
        minimize.connect("clicked", lambda _button: self.window.minimize())
        maximize = Gtk.Button.new_from_icon_name("window-maximize-symbolic")
        maximize.connect("clicked", self._toggle_maximized)
        self.window.connect("notify::maximized", self._update_maximize_button, maximize)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close.connect("clicked", lambda _button: self.window.close())
        for button, name in ((minimize, "Minimize"), (maximize, "Maximize"), (close, "Close")):
            button.add_css_class("window-control")
            button.set_tooltip_text(name)
            button.update_property([Gtk.AccessibleProperty.LABEL], [name])
            window_controls.append(button)
        titlebar.pack_end(window_controls)
        titlebar.pack_end(options)
        self.window.set_titlebar(titlebar)

        shell = box()
        self.window.set_child(shell)
        shell.append(self._sidebar())

        workspace = box(True, 0, "workspace")
        workspace.set_hexpand(True)
        shell.append(workspace)
        header = box(True, 7, "page-header")
        workspace.append(header)
        headline = box(spacing=16)
        self.page_title = label("", "page-title")
        headline.append(self.page_title)
        self.device_pill = label("", "device-pill")
        self.device_pill.set_valign(Gtk.Align.CENTER)
        headline.append(self.device_pill)
        header.append(headline)
        self.description = label("", "subtitle", wrap=True)
        header.append(self.description)

        tabs = box(spacing=19, css="tabs")
        self.controls_tab = Gtk.ToggleButton(label="Assignments")
        self.settings_tab = Gtk.ToggleButton(label="Device settings")
        self.settings_tab.set_group(self.controls_tab)
        self.controls_tab.set_active(True)
        for tab, page in ((self.controls_tab, "controls"), (self.settings_tab, "settings")):
            tab.connect("toggled", self._tab, page)
            tabs.append(tab)
        workspace.append(tabs)
        self.pages = Gtk.Stack()
        self.pages.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.pages.set_vexpand(True)
        workspace.append(self.pages)

        content = box(css="body")
        left = box(True, 17, "control-area")
        left.set_hexpand(True)
        content.append(scroll(left))
        visual = box(True, 1, "visual")
        left.append(visual)
        visual_header = box(spacing=10, css="visual-heading")
        self.visual_title = label("", "visual-title")
        self.visual_title.set_hexpand(True)
        visual_header.append(self.visual_title)
        visual_header.append(label("Click a control to edit", "visual-hint"))
        visual.append(visual_header)
        self.diagram = Diagram(self._choose_control)
        visual.append(self.diagram.picture)
        row_header = box(spacing=8)
        self.list_title = label("", "section-title")
        self.list_title.set_hexpand(True)
        row_header.append(self.list_title)
        self.mapping_count = label("", "small")
        row_header.append(self.mapping_count)
        self.linux_shortcuts_button = Gtk.Button(label="Set up Linux shortcuts")
        self.linux_shortcuts_button.connect("clicked", self._set_up_linux_shortcuts)
        row_header.append(self.linux_shortcuts_button)
        left.append(row_header)
        self.control_list = box(True, 0, "control-list")
        left.append(self.control_list)
        content.append(self._inspector())
        self.pages.add_named(content, "controls")

        self.settings_content = box(True, 15, "settings-area")
        self.pages.add_named(scroll(self.settings_content), "settings")
        footer = box(spacing=10, css="footer")
        self.footer_icon = icon("dialog-information-symbolic", 16)
        footer.append(self.footer_icon)
        self.footer_message = label("", "small", wrap=True)
        self.footer_message.set_hexpand(True)
        footer.append(self.footer_message)
        self.setup_button = Gtk.Button(label="Hardware setup")
        self.setup_button.set_action_name("app.setup")
        self.setup_button.add_css_class("flat")
        footer.append(self.setup_button)
        workspace.append(footer)
        self._show_model("keyboard")

    def _toggle_maximized(self, _button: Any) -> None:
        if self.window.is_maximized():
            self.window.unmaximize()
        else:
            self.window.maximize()

    def _update_maximize_button(self, window: Any, _param: Any, button: Any) -> None:
        maximized = window.is_maximized()
        button.set_icon_name("window-restore-symbolic" if maximized else "window-maximize-symbolic")
        name = "Restore" if maximized else "Maximize"
        button.set_tooltip_text(name)
        button.update_property([Gtk.AccessibleProperty.LABEL], [name])

    def _sidebar(self) -> Any:
        sidebar = box(True, 7, "sidebar")
        sidebar.set_size_request(213, -1)
        sidebar.set_hexpand(False)
        brand = box(spacing=10, css="brand")
        brand.append(icon(APP_ID, 36))
        brand.append(label("MX Studio", "brand-name"))
        sidebar.append(brand)
        sidebar.append(label("Devices", "nav-heading"))
        for model, name in DEVICE_NAMES.items():
            button = Gtk.Button()
            button.add_css_class("nav-button")
            row = box(spacing=10)
            row.append(icon(DEVICE_ICONS[model], 21))
            text = box(True, 4)
            text.set_hexpand(True)
            text.append(label(name, "nav-title"))
            status = label("Looking for device", "nav-status")
            text.append(status)
            row.append(text)
            button.set_child(row)
            button.connect("clicked", lambda _button, value=model: self._show_model(value))
            sidebar.append(button)
            self.nav[model] = button
            self.nav_status[model] = status
        spacer = box()
        spacer.set_vexpand(True)
        sidebar.append(spacer)
        connection = box(spacing=8, css="connection")
        connection.append(icon("network-wireless-symbolic", 15))
        self.connection = label("Checking connections", "nav-status", wrap=True)
        connection.append(self.connection)
        sidebar.append(connection)
        return sidebar

    def _inspector(self) -> Any:
        panel = box(True, 15, "inspector")
        panel.set_size_request(264, -1)
        panel.set_vexpand(True)
        panel.append(label("Selected control", "small"))
        self.control_key = label("", "control-key")
        self.control_key.set_halign(Gtk.Align.START)
        panel.append(self.control_key)
        self.control_title = label("", "control-title", wrap=True)
        panel.append(self.control_title)
        self.inspector_note = label("", "small", wrap=True)
        self.inspector_note.set_max_width_chars(30)
        panel.append(self.inspector_note)
        panel.append(Gtk.Separator())
        action = box(True, 7)
        panel.append(action)
        action.append(label("Action", "field-label"))
        self.action = Gtk.DropDown.new_from_strings(ACTION_LABELS)
        self.action.set_enable_search(True)
        self.action.set_tooltip_text("Choose what this control does")
        self.action.connect("notify::selected", self._action_changed)
        action.append(self.action)
        self.action_help = label("", "small", wrap=True)
        self.action_help.set_max_width_chars(30)
        action.append(self.action_help)
        self.dictation_setup = Gtk.LinkButton.new_with_label(
            "https://handy.computer/docs/linux", "Set up Handy for Dictation"
        )
        self.dictation_setup.set_halign(Gtk.Align.START)
        action.append(self.dictation_setup)
        self.custom_box = box(True, 6)
        self.custom = Gtk.Entry(placeholder_text="Ctrl+Alt+T")
        self.custom.set_tooltip_text("Modifiers followed by a key, separated by +")
        self.custom.set_max_length(80)
        self.custom.connect("changed", self._action_changed)
        self.custom.connect("activate", self._save_mapping)
        self.custom_box.append(self.custom)
        self.custom_box.append(label("Use Ctrl, Alt, Shift or Super with a key.", "small", wrap=True))
        panel.append(self.custom_box)
        self.validation = label("", "error-text", wrap=True)
        self.validation.set_max_width_chars(30)
        panel.append(self.validation)
        self.save_button = Gtk.Button(label="Save assignment")
        self.save_button.add_css_class("primary")
        self.save_button.connect("clicked", self._save_mapping)
        panel.append(self.save_button)
        reset = Gtk.Button(label="Restore device default")
        reset.add_css_class("flat")
        reset.connect("clicked", self._reset_mapping)
        panel.append(reset)
        return scroll(panel)

    def _tab(self, button: Any, page: str) -> None:
        if button.get_active():
            self.pages.set_visible_child_name(page)

    def _show_model(self, model: str) -> None:
        if not self.window:
            return
        self.model = model
        for key, button in self.nav.items():
            if key == model:
                button.add_css_class("selected")
            else:
                button.remove_css_class("selected")
        self.page_title.set_text(DEVICE_NAMES[model])
        self.description.set_text(DESCRIPTIONS[model])
        self.visual_title.set_text("Keyboard layout" if model == "keyboard" else "Mouse controls")
        self.list_title.set_text("Shortcut keys" if model == "keyboard" else "Buttons, wheels and gestures")
        self._build_rows()
        self._build_settings()
        self._choose_control(self.selected[model])
        self._update_status()

    def _build_rows(self) -> None:
        remove_children(self.control_list)
        self.rows.clear()
        for control in CONTROLS[self.model]:
            if control.id == "gesture-up":
                section = label("Directional gestures", "list-heading")
                self.control_list.append(section)
            row = Gtk.Button()
            row.add_css_class("control-row")
            row.connect("clicked", lambda _button, value=control.id: self._choose_control(value))
            content = box(spacing=10)
            cap = label(control.label, "keycap")
            cap.set_xalign(0.5)
            content.append(cap)
            name = label(control.name, "row-name")
            name.set_hexpand(True)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            content.append(name)
            assignment = label("", "row-assignment")
            assignment.set_ellipsize(Pango.EllipsizeMode.END)
            assignment.set_max_width_chars(23)
            content.append(assignment)
            content.append(icon("go-next-symbolic", 13))
            row.set_child(content)
            self.control_list.append(row)
            self.rows[control.id] = row, assignment

    def _choose_control(self, control: str) -> None:
        current = next(item for item in CONTROLS[self.model] if item.id == control)
        self.selected[self.model] = control
        self.control_title.set_text(current.name)
        self.control_key.set_text(current.label)
        self.loading = True
        saved = self.config.mappings[self.model].get(control, "")
        value = saved if saved in PRESETS else CUSTOM
        self.action.set_selected(ACTION_IDS.index(value))
        self.custom.set_text(saved.removeprefix("shortcut:") if value == CUSTOM else "")
        self.loading = False
        self._action_changed()
        self._update_row_status()
        self._update_inspector_note()

    def _update_row_status(self) -> None:
        mapping = self.config.mappings[self.model]
        status = self.snapshot.devices.get(self.model)
        for control, (button, assignment) in self.rows.items():
            saved = mapping.get(control, "")
            text = action_label(saved)
            if status and control not in status.controls:
                text = "Unavailable"
            assignment.set_text(text)
            if status and control in status.errors:
                assignment.set_text("Check assignment")
            if control == self.selected[self.model]:
                button.add_css_class("selected-row")
            else:
                button.remove_css_class("selected-row")
        self.mapping_count.set_text(f"{len(mapping)} assigned")
        self.linux_shortcuts_button.set_visible(self.model == "keyboard")
        if self.model == "keyboard":
            available = status.controls if status else None
            shortcuts = self.config.missing_linux_keyboard_shortcuts(available)
            self.linux_shortcuts_button.set_sensitive(bool(shortcuts) and not self.config_error)
            self.linux_shortcuts_button.set_tooltip_text(
                "Fill unassigned keys with matching Linux shortcuts."
                if shortcuts else "All available Linux shortcuts are already assigned."
            )
        self.diagram.render(self.model, self.selected[self.model], set(mapping))

    def _update_inspector_note(self) -> None:
        status = self.snapshot.devices.get(self.model)
        control = self.selected[self.model]
        if status and control in status.errors:
            text = status.errors[control]
        elif status and control not in status.controls:
            text = "This control is unavailable on the connected firmware."
        elif not status:
            text = "Saved assignments apply when the device connects."
        elif control.startswith("gesture-") and control != "gesture-tap":
            text = "Hold the thumb-rest button, move in this direction, then release."
        elif control == "gesture-tap":
            text = "The tap opens the desktop overview when a direction is assigned and tap is set to default."
        elif control.startswith("thumb-"):
            text = "Assign an action to this direction of the side wheel."
        elif self.model == "keyboard" and control not in self.config.mappings["keyboard"]:
            text = "Device default uses the keyboard’s built-in behavior. Choose an action to send a Linux shortcut."
        elif self.model == "keyboard" and self.control_key.get_text().startswith("F"):
            text = "This assignment applies to the special-key function printed on the key."
        else:
            text = "The device default restores this control’s usual behavior."
        self.inspector_note.set_text(text)

    def _current_action(self) -> str:
        selected = self.action.get_selected()
        value = ACTION_IDS[selected]
        return f"shortcut:{self.custom.get_text().strip()}" if value == CUSTOM else value

    def _action_changed(self, *_args: Any) -> None:
        if self.loading:
            return
        value = self._current_action()
        self.custom_box.set_visible(value.startswith("shortcut:"))
        dictation = value == DICTATION_ACTION
        self.dictation_setup.set_visible(dictation)
        help_text = ""
        if dictation:
            help_text = "Open Handy with a speech model ready. Press once to start and again to stop."
        elif value == "key:Emoji":
            help_text = "Opens the emoji picker in a focused text field that supports Ctrl+period."
        elif value in MONITOR_APPLICATION_STEPS:
            help_text = "Cycles through apps on the monitor under your cursor in the current workspace. Requires X11."
        self.action_help.set_text(help_text)
        self.action_help.set_visible(bool(help_text))
        current = self.config.mappings[self.model].get(self.selected[self.model], "")
        error = ""
        try:
            validate_action(value)
        except ValueError as issue:
            if self.custom.get_text():
                error = str(issue)
            self.save_button.set_sensitive(False)
        else:
            self.save_button.set_sensitive(value != current and not self.config_error)
        self.validation.set_text(error)
        self.validation.set_visible(bool(error))

    def _save_mapping(self, *_args: Any) -> None:
        if self.save_button.get_sensitive():
            self._commit_mapping(self._current_action())

    def _reset_mapping(self, *_args: Any) -> None:
        self._commit_mapping("")

    def _set_up_linux_shortcuts(self, *_args: Any) -> None:
        if self.model != "keyboard" or self.config_error:
            return
        status = self.snapshot.devices.get("keyboard")
        shortcuts = self.config.missing_linux_keyboard_shortcuts(status.controls if status else None)
        if not shortcuts:
            return
        mappings = self.config.mappings["keyboard"]
        mappings.update(shortcuts)
        if self._persist():
            self._choose_control(self.selected["keyboard"])
        else:
            for control in shortcuts:
                del mappings[control]

    def _commit_mapping(self, value: str) -> None:
        if self.config_error:
            return
        control = self.selected[self.model]
        mappings = self.config.mappings[self.model]
        old = mappings.get(control, "")
        if value == old:
            self._choose_control(control)
            return
        if value:
            mappings[control] = value
        else:
            mappings.pop(control, None)
        if self._persist():
            self._choose_control(control)
        elif old:
            mappings[control] = old
        else:
            mappings.pop(control, None)

    def _persist(self) -> bool:
        try:
            self.config.save(self.path)
        except OSError as error:
            self.changed_notice = f"Could not save your profile. {error}"
            self._update_status()
            return False
        self.changed_notice = ""
        if self.worker:
            self.pending = self.model in self.snapshot.devices
            self.worker.update(self.config)
        self._update_status()
        return True

    def _build_settings(self) -> None:
        remove_children(self.settings_content)
        self.setting_rows.clear()
        for name in DEVICE_SETTINGS[self.model]:
            row = box(spacing=16, css="setting-row")
            text = box(True, 5)
            text.set_hexpand(True)
            text.append(label(SETTING_LABELS[name], "field-label"))
            text.append(label(SETTING_DETAILS[name], "small", wrap=True))
            note = label("", "small", wrap=True)
            text.append(note)
            row.append(text)
            dropdown = Gtk.DropDown()
            dropdown.set_size_request(187, -1)
            dropdown.set_valign(Gtk.Align.CENTER)
            dropdown.set_enable_search(True)
            dropdown.set_tooltip_text(SETTING_LABELS[name])
            dropdown.connect("notify::selected", self._setting_changed, name)
            row.append(dropdown)
            self.settings_content.append(row)
            self.setting_rows[name] = dropdown, note, [], None
        self._update_settings()

    def _update_settings(self) -> None:
        status = self.snapshot.devices.get(self.model)
        configured = self.config.settings[self.model]
        self.loading = True
        for name, (dropdown, note, previous, previous_value) in self.setting_rows.items():
            current = status.values.get(name) if status else None
            available = status.options.get(name, []) if status else []
            choices: list[int | None] = [None, *available]
            saved = configured.get(name)
            if saved is not None and saved not in choices:
                choices.append(saved)
            if choices != previous or current != previous_value:
                items = ["Device default" if name == THUMB_SPEED else "Keep device setting"]
                if current is not None:
                    items[0] += f" ({self._setting_value(name, current)})"
                items.extend(self._setting_value(name, value) for value in choices[1:] if value is not None)
                dropdown.set_model(Gtk.StringList.new(items))
                self.setting_rows[name] = dropdown, note, choices, current
            dropdown.set_selected(choices.index(saved))
            dropdown.set_sensitive(bool(available) and not self.config_error)
            if not status:
                message = "Connect the device to change this setting."
            else:
                message = status.errors.get(name, "") if available else "Unavailable on this firmware."
            note.set_text(message)
            note.set_visible(bool(message))
        self.loading = False

    @staticmethod
    def _setting_value(name: str, value: int) -> str:
        if name == "dpi":
            return f"{value} DPI"
        return SETTING_CHOICES[name].get(value, str(value))

    def _setting_changed(self, dropdown: Any, _param: Any, name: str) -> None:
        if self.loading:
            return
        choices = self.setting_rows[name][2]
        index = dropdown.get_selected()
        if index >= len(choices):
            return
        value = choices[index]
        settings = self.config.settings[self.model]
        previous = settings.get(name)
        if previous == value:
            return
        if value is None:
            settings.pop(name, None)
        else:
            settings[name] = value
        if not self._persist():
            if previous is None:
                settings.pop(name, None)
            else:
                settings[name] = previous
            self._update_settings()

    def _update_status(self) -> None:
        for model, status_label in self.nav_status.items():
            device = self.snapshot.devices.get(model)
            detail = self._device_detail(device) if device else "Offline"
            status_label.set_text(detail)
        receivers = self.snapshot.receivers
        connected = len(self.snapshot.devices)
        self.connection.set_text(
            f"{connected} of 2 devices connected" if connected else
            f"{receivers} Bolt receiver{'s' if receivers > 1 else ''} detected" if receivers else
            "No devices detected"
        )
        status = self.snapshot.devices.get(self.model)
        self.device_pill.set_text(self._device_detail(status) if status else "Offline")
        if status:
            self.device_pill.add_css_class("online")
        else:
            self.device_pill.remove_css_class("online")
        message = self.changed_notice or self.config_error or self.snapshot.message
        if not message:
            if self.pending:
                message = "Applying saved changes…"
            elif status and status.errors:
                message = "Some saved changes could not be applied. Select an affected control or setting for details."
            elif status:
                message = "Remaps keep running when you close the window."
            else:
                message = "Wake the device and select the Easy-Switch channel paired with this computer."
        self.footer_message.set_text(message)
        self.setup_button.set_visible(not self.snapshot.output_ready or not connected)

    @staticmethod
    def _device_detail(status: DeviceStatus) -> str:
        if status.battery is None:
            return status.transport
        return f"{status.transport} · {status.battery}%"

    def _setup(self, *_args: Any) -> None:
        if self.window is None:
            self.activate()
        dialog = Gtk.MessageDialog(
            transient_for=self.window, modal=True, buttons=Gtk.ButtonsType.CLOSE,
            text="Connect your MX devices",
            secondary_text=(
                "Use your paired Logi Bolt receiver or pair through Linux Bluetooth settings. "
                "Wake each device and choose the Easy-Switch channel for this computer.\n\n"
                "Install Solaar and python3-evdev and apply linux/72-mx-studio.rules "
                "using the Ubuntu setup commands in README.md. Quit Solaar while using "
                "MX Studio if both manage the same controls.\n\n"
                "Run ./mx-studio --check from your desktop terminal to verify device access."
            ),
        )
        dialog.connect("response", lambda item, _response: item.destroy())
        dialog.present()
