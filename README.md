# MX Studio

A GTK 4 desktop app for the **Logitech MX Keys S** keyboard and **MX Master 3S** mouse included in the MX Keys S Combo. It uses Solaar’s HID++ transport and the Linux kernel’s virtual input interface. It keeps one local profile for the combo and works with devices already paired over Logi Bolt or Bluetooth.

## What you can customize

| Device | Assignments | Hardware settings |
| --- | --- | --- |
| MX Keys S | The F1–F12 special actions, volume up, calculator, screen capture, context menu and lock. The special actions include dictation, emoji and microphone mute. | Function row mode and backlight mode |
| MX Master 3S | Wheel click, wheel mode button, back, forward, both thumb-wheel directions, gesture tap and four hold-and-move gestures. | DPI, MagSpeed wheel mode, SmartShift threshold, horizontal scroll speed and thumb-wheel inversion |

Choose a preset, enter a key chord such as `Ctrl+Alt+T`, or disable a control. Presets include microphone mute, Print for screenshots, and screen brightness up and down. The desktop or focused app decides how to handle those keys. Custom shortcuts use physical key positions interpreted by your current Linux keyboard layout.

Hold an assigned volume, screen brightness or keyboard backlight up/down control to adjust it continuously.

Under **MX Master 3S → Device settings → Horizontal scroll speed**, choose a speed from 50% to 300%, or **Device default** to clear the adjustment. The speed also applies when a thumb-wheel direction is assigned **Scroll left** or **Scroll right**.

On X11, assign **Previous app on cursor monitor** to the left gesture and **Next app on cursor monitor** to the right gesture. Each action cycles through apps on the monitor under the pointer in the current workspace. If focus is on another monitor, the first gesture focuses the frontmost app on the cursor’s monitor.

Assign **Keyboard backlight up** and **Keyboard backlight down** to a keyboard key or mouse control. They adjust the connected MX Keys S by one level and switch it to Manual brightness. To let the ambient light sensor set the level again, select **Automatic** under **Device settings → Backlight**. With **Keep device setting** selected, the keyboard retains the mode set by the actions.

If a special key does nothing with **Device default**, choose **MX Keys S → Assignments → Set up Linux shortcuts**. This fills unassigned keys for volume, playback, brightness, microphone mute, emoji, calculator, screenshots, context menu and screen lock. Existing assignments stay in place. Keys shared with F1–F12 may require Fn, depending on **Device settings → Function row**.

### Dictation

1. Install Handy’s Ubuntu `.deb`, open Handy, and download a speech model in its setup screen. Follow Handy’s pasting setup for your desktop. On X11, Handy uses `xdotool` to type into other apps. [Handy download](https://github.com/cjpais/Handy/releases/latest) · [Handy Linux setup](https://handy.computer/docs/linux#2-set-up-pasting)
2. Select **MX Keys S → F5 Dictation → Action → Dictation → Save assignment**. With Handy open, focus a text field and press F5 (or Fn+F5) once to start recording and again to stop and transcribe. MX Studio invokes Handy’s toggle command. [Handy CLI](https://handy.computer/docs/cli)

**Emoji picker** uses Ctrl+period in text fields that support it. For the MX Master 3S, **Toggle scroll wheel mode** switches the MagSpeed wheel between free-spin and ratchet. You can assign these actions, clicks, scrolling in four directions, and workspace switching to any supported control. [GTK emoji picker](https://docs.gtk.org/gtk4/signal.TextView.insert-emoji.html) · [SmartShift protocol](https://openlogi.org/hidpp/features/x2110-smartshift)

The app reads what the connected firmware exposes. The keyboard’s regular typing keys and Easy-Switch buttons, the mouse’s primary clicks, and device pairing stay under firmware or Linux control. The app targets a single combo, with one active connection per model. Pair through the desktop’s Bluetooth settings or use the factory-paired Bolt receiver. If you need to pair a new device to Bolt, use Solaar first.

### Product research

- Logitech identifies the combo as the MX Keys S, MX Master 3S and MX Palm Rest. Its keyboard has a full-size layout, three Easy-Switch channels, and ambient- and proximity-aware backlighting. Logi Options+ is available for Windows and macOS. [Combo specifications](https://www.logitech.com/en-gb/shop/p/mx-keys-s-combo.920-011613) · [Keyboard setup guide](https://support.logi.com/hc/en-001/articles/13750828691991-Getting-Started-MX-Keys-S)
- The MX Master 3S has a MagSpeed wheel that can run in ratchet or free-spin mode, a separate horizontal thumb wheel, forward and back buttons, and a gesture button under the thumb rest. [Mouse setup guide](https://hub.sync.logitech.com/mx-master-3s/post/mx-master-3s---setup-guide-logIbt80SWDODOk)
- The control diagram follows the documented QWERTY layout. The control IDs and available reporting modes were cross-checked against public Solaar hardware reports. [MX Keys S HID++ report](https://github.com/pwr-Solaar/Solaar/issues/2210) · [MX Master 3S HID++ report](https://github.com/pwr-Solaar/Solaar/issues/2824) · [Solaar’s HID++ rule documentation](https://pwr-solaar.github.io/Solaar/rules/) · [Linux uinput](https://docs.kernel.org/input/uinput.html)

## Install on Ubuntu 24.04

In a terminal on your Linux desktop, from this project directory:

```sh
sudo apt install python3-gi python3-cairo gir1.2-gtk-4.0 python3-evdev solaar
sudo install -m 0644 linux/72-mx-studio.rules /etc/udev/rules.d/72-mx-studio.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw --action=add
sudo udevadm trigger --subsystem-match=misc --sysname-match=uinput --action=add
./mx-studio --check
./mx-studio
```

If your kernel provides uinput as a module and the node is missing, run `sudo modprobe uinput` and repeat the uinput trigger. After changing rules, reconnect Bolt or Bluetooth if the device is still inaccessible. Run MX Studio as your desktop user. The udev rules use the active session’s ACLs to grant access to the Bolt receiver, both Bluetooth models, and the system’s uinput node.

Use `./mx-studio --install-launcher` to add **MX Studio** to your desktop’s app menu. Enable **Start at login** from the app menu to run remaps in the background at login. Closing the window keeps the remaps active. Use **Quit MX Studio** to release the buttons and exit. Quit Solaar or other remappers before assigning controls they already manage. If a device was left diverted after a forced shutdown, power-cycle it before reopening MX Studio.

### Try an assignment

1. Select **MX Keys S → Screen capture → Take screenshot → Save assignment**. Press the key marked for screen capture.
2. Select **MX Master 3S → Gesture button tap → Desktop overview → Save assignment**. Press the button in the thumb rest.
3. Select **Restore device default** to remove a remap. If any directional gesture is assigned, a default gesture tap sends the overview key. Directions with no assignment have no action. When remapping one thumb-wheel direction, the unassigned direction still scrolls horizontally.

Assignments are saved in `${XDG_CONFIG_HOME:-~/.config}/mx-studio/profile.json` and reapplied when the device reconnects. Hardware preferences are saved there too. **Keep device setting** stops MX Studio from applying that preference and displays the device’s current value.

## Verification

`./mx-studio --check` prints the connection type, discovered controls and uinput access. It does not apply the saved profile or send shortcuts. To exercise the whole path, run the two assignments above on the desktop.

For development, install the Python tooling with Poetry:

```sh
poetry install
poetry run ruff check .
poetry run mypy mx_studio
poetry run python -m unittest discover -s tests -v
```

Run the desktop app via `./mx-studio` to use the distribution’s GTK and Solaar packages.
