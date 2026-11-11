from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from multiprocessing import Pipe
from multiprocessing.connection import Connection
from threading import Thread
from time import monotonic

from .hardware import Hardware, Snapshot
from .models import Config

LOG = logging.getLogger(__name__)


class Worker:
    def __init__(self, config: Config, publish: Callable[[Snapshot], None]) -> None:
        # Solaar can load GTK 3 bindings. The hardware process has its own GObject runtime.
        self.channel, child = Pipe()
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "mx_studio.worker", str(child.fileno()),
                 str(logging.getLogger().getEffectiveLevel())],
                pass_fds=(child.fileno(),), start_new_session=True,
            )
        except Exception:
            self.channel.close()
            raise
        finally:
            child.close()
        self.publish = publish
        self.closing = False
        self.reader = Thread(target=self._receive, name="mx-hardware-status")
        self.reader.start()
        self.update(config)

    def _receive(self) -> None:
        try:
            while True:
                self.publish(self.channel.recv())
        except (EOFError, OSError):
            if not self.closing:
                self.publish(Snapshot(message="Hardware process exited. Quit and reopen MX Studio."))

    def update(self, config: Config) -> None:
        with suppress(OSError):
            self.channel.send(config)

    def refresh(self) -> None:
        with suppress(OSError):
            self.channel.send("refresh")

    def close(self) -> None:
        self.closing = True
        with suppress(OSError):
            self.channel.send(None)
        self.process.wait()
        self.reader.join()
        self.channel.close()


def run_worker(channel: Connection) -> None:
    hardware = None
    config = Config()
    next_scan = 0.0
    previous = None
    delay = 0.0
    try:
        while True:
            requested = changed = False
            if channel.poll(delay):
                while True:
                    command = channel.recv()
                    if command is None:
                        return
                    if command == "refresh":
                        requested = True
                    elif isinstance(command, Config):
                        config = command
                        changed = True
                    if not channel.poll():
                        break
            try:
                if hardware is None:
                    hardware = Hardware()
                action_status_changed = hardware.pump()
                scan = requested or monotonic() >= next_scan or any(
                    endpoint.changed or any(device.dirty and device.online
                                            for device in endpoint.devices.values())
                    for endpoint in hardware.endpoints.values()
                )
                if changed or requested:
                    hardware.force = True
                if scan:
                    status = hardware.refresh(config, reload_settings=requested)
                    next_scan = monotonic() + 7
                elif hardware.force:
                    hardware.apply(config)
                    status = hardware.snapshot()
                elif action_status_changed:
                    status = hardware.snapshot()
                else:
                    status = None
                delay = 0.025 if hardware.endpoints else 0.5
            except Exception as error:
                LOG.debug("Hardware operation failed", exc_info=True)
                status = Snapshot(message=str(error))
                delay = 4
            if status is not None and (status != previous or changed or requested):
                previous = deepcopy(status)
                channel.send(previous)
    except (EOFError, BrokenPipeError):
        pass
    finally:
        if hardware:
            hardware.close()
        channel.close()


if __name__ == "__main__":
    logging.basicConfig(level=int(sys.argv[2]))
    run_worker(Connection(int(sys.argv[1])))
