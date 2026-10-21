"""Service-scoped idle-sleep inhibition; never change permanent power settings."""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import select
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import TracebackType
from typing import Any

LOG = logging.getLogger(__name__)


def constrained_host() -> bool:
    """Containers and WSL cannot promise control over the physical host."""
    return (
        Path('/.dockerenv').exists() or Path('/run/.containerenv').exists()
        or bool(os.environ.get('container'))
        or 'microsoft' in platform.release().lower()
    )


class KeepAwake:
    """Best-effort OS assertion for the entire receiving-service lifetime.

    Enter/exit on the same thread (Windows assertions are thread-local).
    Helpers are tied to the parent PID or pipe EOF, including abrupt exits.
    """

    def __init__(self, mode: str | None = None) -> None:
        self.mode = (mode if mode is not None else
                     os.environ.get('XDSH_KEEP_AWAKE', 'off')).strip().lower()
        if self.mode not in {'on', 'off'}:
            raise ValueError('XDSH_KEEP_AWAKE must be on or off')
        self.status = 'off'
        self.process: subprocess.Popen[bytes] | None = None
        self._windows: Any = None
        self._previous = 0
        self._closed = threading.Event()
        self._monitor: threading.Thread | None = None

    def __enter__(self) -> KeepAwake:
        if self.mode == 'off':
            LOG.info('Keep-awake disabled')
            return self
        try:
            system = platform.system()
            if system == 'Linux' and constrained_host():
                raise OSError('Container/WSL requires host-side idle inhibition')
            if system == 'Windows':
                self._windows = ctypes.WinDLL('kernel32', use_last_error=True)  # type: ignore[attr-defined]
                function = self._windows.SetThreadExecutionState
                function.argtypes = [ctypes.c_uint32]
                function.restype = ctypes.c_uint32
                self._previous = int(function(0x80000001))
                if not self._previous:
                    raise OSError('Windows refused the idle-sleep assertion')
            elif system in {'Darwin', 'Linux'}:
                executable = shutil.which('caffeinate' if system == 'Darwin' else 'systemd-inhibit')
                if executable is None:
                    raise OSError('OS sleep-inhibition utility is unavailable')
                if system == 'Darwin':
                    command = [executable, '-i', '-w', str(os.getpid())]
                else:
                    # Only idle: do not intercept lid switches/manual suspend/shutdown.
                    command = [executable, '--what=idle', '--mode=block', '--who=XDSH',
                               '--why=XDSH receiving service', sys.executable, '-u', '-c',
                               'import sys; print("READY", flush=True); sys.stdin.buffer.read()']
                self.process = subprocess.Popen(
                    command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, close_fds=True,
                )
                if system == 'Linux':
                    assert self.process.stdout is not None
                    ready, _, _ = select.select([self.process.stdout], [], [], 3)
                    if not ready or self.process.stdout.readline() != b'READY\n':
                        raise OSError('logind refused or timed out acquiring idle inhibitor')
                else:
                    try:
                        self.process.wait(timeout=0.15)
                    except subprocess.TimeoutExpired:
                        pass
                    else:
                        raise OSError('caffeinate exited before acquiring protection')
                self._monitor = threading.Thread(target=self._watch, daemon=True)
            else:
                raise OSError('Unsupported operating system')
            self.status = 'requested'
            LOG.info('Keep-awake requested (%s); display sleep unchanged', system)
            if self._monitor is not None:
                self._monitor.start()
        except OSError as error:
            self.close()
            self.status = 'unavailable'
            LOG.warning('Keep-awake unavailable: %s; host may sleep', error)
        return self

    def _watch(self) -> None:
        assert self.process is not None
        self.process.wait()
        if not self._closed.is_set():
            self.status = 'unavailable'
            LOG.warning('Keep-awake helper exited; host may sleep')

    def close(self) -> None:
        self._closed.set()
        if self.process is not None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            if self.process.stdout is not None:
                self.process.stdout.close()
        if self._windows is not None and self._previous:
            if not self._windows.SetThreadExecutionState(self._previous):
                LOG.warning('Windows idle assertion release failed')
            self._previous = 0
        if self._monitor is not None:
            self._monitor.join(timeout=2)

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()
        self.status = 'released'
