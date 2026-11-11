from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from shutil import copyfile

from .models import config_home

APP_ID = "io.github.mxstudio.MXStudio"
PROJECT = Path(__file__).resolve().parent.parent
ICON_THEME = Path(__file__).with_name("icons")
APP_ICON = Path("hicolor", "256x256", "apps", f"{APP_ID}.png")


def desktop_entry(path: Path, *, background: bool = False) -> None:
    command = str(PROJECT / "mx-studio")
    if not Path(command).is_file():
        command = f"{sys.executable} -m mx_studio"
    else:
        command = command.replace("%", "%%")
        command = '"' + "".join("\\" + char if char in '"\\$`' else char for char in command) + '"'
        command = command.replace("\\", "\\\\")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Desktop Entry]\nType=Application\nName=MX Studio\n"
        "Comment=Customize your Logitech MX Keys S and MX Master 3S\n"
        f"Exec={command}{' --background' if background else ''}\n"
        "StartupWMClass=mx-studio\n"
        f"Icon={APP_ID}\nTerminal=false\nCategories=Settings;HardwareSettings;\n"
    )


def autostart_path() -> Path:
    return config_home() / "autostart" / f"{APP_ID}.desktop"


def main() -> int:
    parser = argparse.ArgumentParser(description="Customize your Logitech MX Keys S and MX Master 3S")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="check access without changing device settings")
    group.add_argument("--install-launcher", action="store_true", help="add MX Studio to the app menu")
    group.add_argument("--background", action="store_true", help="run saved remaps without opening a window")
    parser.add_argument("--debug", action="store_true", help="enable hardware diagnostics in the log")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING)
    if args.install_launcher:
        data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        icon = data / "icons" / APP_ICON
        icon.parent.mkdir(parents=True, exist_ok=True)
        copyfile(ICON_THEME / APP_ICON, icon)
        path = data / "applications" / f"{APP_ID}.desktop"
        desktop_entry(path)
        print(f"Launcher installed at {path}")
        return 0
    if args.check:
        from .hardware import Hardware

        try:
            hardware = Hardware()
            try:
                report, ready = hardware.diagnostic()
            finally:
                hardware.close()
        except Exception as error:
            print(f"Hardware check failed: {error}", file=sys.stderr)
            return 1
        print(report)
        return 0 if ready else 1
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from .ui import Studio
    except (ImportError, ValueError) as error:
        print(f"MX Studio needs GTK 4, python3-gi and python3-cairo ({error}).", file=sys.stderr)
        return 1
    app = Studio()
    return int(app.run(["mx-studio"] + (["--background"] if args.background else [])))


if __name__ == "__main__":
    sys.exit(main())
