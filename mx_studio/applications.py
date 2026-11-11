from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

ALL_WORKSPACES = 0xFFFFFFFF


@dataclass(frozen=True)
class Bounds:
    x: int
    y: int
    width: int
    height: int

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    def overlap(self, other: Bounds) -> int:
        width = min(self.x + self.width, other.x + other.width) - max(self.x, other.x)
        height = min(self.y + self.height, other.y + other.height) - max(self.y, other.y)
        return max(0, width) * max(0, height)


@dataclass(frozen=True)
class ApplicationWindow:
    xid: int
    application: str
    bounds: Bounds


def choose_application_window(
    windows: list[ApplicationWindow],
    monitors: list[Bounds],
    monitor: Bounds,
    stacking_order: list[int],
    active: int,
    step: int,
) -> int | None:
    # Preserve the window manager's initial mapping order for predictable cycling.
    stack_rank = {xid: rank for rank, xid in enumerate(stacking_order)}
    applications: dict[str, list[int]] = {}
    active_application = None
    for window in windows:
        if not window.bounds.overlap(monitor):
            continue
        if max(monitors, key=window.bounds.overlap) != monitor:
            continue
        applications.setdefault(window.application, []).append(window.xid)
        if window.xid == active:
            active_application = window.application
    if not applications:
        return None

    def rank(xid: int) -> int:
        return stack_rank.get(xid, -1)

    if active_application is None:
        return max((xid for group in applications.values() for xid in group), key=rank)
    if len(applications) == 1:
        return active
    order = list(applications)
    target = order[(order.index(active_application) + step) % len(order)]
    return max(applications[target], key=rank)


class MonitorApplicationSwitcher:
    def __init__(self) -> None:
        session = os.environ.get("XDG_SESSION_TYPE")
        if session == "wayland" or (session != "x11" and os.environ.get("WAYLAND_DISPLAY")):
            raise RuntimeError("Cursor-monitor switching requires an X11 desktop session.")
        try:
            from Xlib import X, Xatom, display, error
            from Xlib.protocol import event
        except ImportError as issue:
            raise RuntimeError("Cursor-monitor switching needs python3-xlib.") from issue

        self.x = X
        self.cardinal = Xatom.CARDINAL
        self.bad_window = error.BadWindow
        self.client_message = event.ClientMessage
        try:
            self.connection = display.Display()
        except error.DisplayConnectionError as issue:
            raise RuntimeError("Could not connect to the X11 desktop.") from issue
        self.root = self.connection.screen().root
        self.atoms: dict[str, int] = {}

    def _atom(self, name: str) -> int:
        if name not in self.atoms:
            self.atoms[name] = int(self.connection.intern_atom(name))
        return self.atoms[name]

    def _values(self, window: Any, name: str) -> list[int]:
        reply = window.get_full_property(self._atom(name), self.x.AnyPropertyType)
        return [int(value) for value in reply.value] if reply and reply.format == 32 else []

    def _monitors(self) -> list[Bounds]:
        get_monitors = getattr(self.root, "xrandr_get_monitors", None)
        if get_monitors:
            monitors = get_monitors(True).monitors
            if monitors:
                return [Bounds(m.x, m.y, m.width_in_pixels, m.height_in_pixels) for m in monitors]
        if self.connection.has_extension("XINERAMA") and self.connection.xinerama_is_active():
            return [
                Bounds(m.x, m.y, m.width, m.height)
                for m in self.connection.xinerama_query_screens().screens
            ]
        raise RuntimeError("Could not read the X11 monitor layout.")

    def _workspace_windows(self, desktop: int) -> list[ApplicationWindow]:
        windows = []
        normal_types = {self._atom(name) for name in (
            "_NET_WM_WINDOW_TYPE_NORMAL", "_NET_WM_WINDOW_TYPE_DIALOG",
        )}
        skip_taskbar = self._atom("_NET_WM_STATE_SKIP_TASKBAR")
        for xid in self._values(self.root, "_NET_CLIENT_LIST"):
            window = self.connection.create_resource_object("window", xid)
            try:
                workspaces = self._values(window, "_NET_WM_DESKTOP")
                if not workspaces or workspaces[0] not in (desktop, ALL_WORKSPACES):
                    continue
                if skip_taskbar in self._values(window, "_NET_WM_STATE"):
                    continue
                types = self._values(window, "_NET_WM_WINDOW_TYPE")
                if types and not normal_types.intersection(types):
                    continue
                geometry = window.get_geometry()
                coords = self.root.translate_coords(window, 0, 0)
                instance, window_class = window.get_wm_class() or ("", "")
                application = (window_class or instance).casefold() or f"window:{xid}"
                windows.append(ApplicationWindow(
                    xid, application, Bounds(coords.x, coords.y, geometry.width, geometry.height)
                ))
            except self.bad_window:
                # An application can close between listing windows and reading its properties.
                continue
        return windows

    def switch(self, step: int) -> None:
        if self._atom("_NET_ACTIVE_WINDOW") not in self._values(self.root, "_NET_SUPPORTED"):
            raise RuntimeError("The X11 window manager does not support activating applications.")
        pointer = self.root.query_pointer()
        monitors = self._monitors()
        monitor = next((m for m in monitors if m.contains(pointer.root_x, pointer.root_y)), None)
        if not pointer.same_screen or monitor is None:
            raise RuntimeError("Move the cursor onto a monitor to switch applications there.")
        workspaces = self._values(self.root, "_NET_CURRENT_DESKTOP")
        if not workspaces:
            raise RuntimeError("Could not read the current X11 workspace.")
        active = next(iter(self._values(self.root, "_NET_ACTIVE_WINDOW")), 0)
        target = choose_application_window(
            self._workspace_windows(workspaces[0]), monitors, monitor,
            self._values(self.root, "_NET_CLIENT_LIST_STACKING"), active, step,
        )
        if target is None:
            raise RuntimeError("There are no applications on the monitor under the cursor.")
        if target != active:
            self._activate(target, active)

    def _activate(self, target: int, active: int) -> None:
        # A property notification on a private window supplies a fresh X11 server timestamp.
        timestamp_window = self.root.create_window(
            0, 0, 1, 1, 0, 0, self.x.InputOnly, event_mask=self.x.PropertyChangeMask,
        )
        timestamp_atom = self._atom("_MX_STUDIO_ACTIVATION_TIME")
        timestamp_window.change_property(timestamp_atom, self.cardinal, 32, [0])
        self.connection.sync()
        if not self.connection.pending_events():
            raise RuntimeError("Could not get the X11 activation timestamp.")
        timestamp = self.connection.next_event().time
        message = self.client_message(
            window=target, client_type=self._atom("_NET_ACTIVE_WINDOW"),
            data=(32, [2, timestamp, active, 0, 0]),
        )
        self.root.send_event(message, event_mask=self.x.SubstructureNotifyMask | self.x.SubstructureRedirectMask)
        self.connection.flush()

    def close(self) -> None:
        self.connection.close()
