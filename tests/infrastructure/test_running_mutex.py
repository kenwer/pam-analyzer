import sys

import pytest

from pam_analyzer.infrastructure import running_mutex
from pam_analyzer.infrastructure.running_mutex import MUTEX_NAME, hold_running_mutex

_SYNCHRONIZE = 0x00100000


@pytest.mark.skipif(sys.platform != "win32", reason="Windows mutex")
def test_mutex_is_visible_while_held_and_gone_after_close() -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    hold_running_mutex()
    handle = running_mutex._handle
    assert handle

    probe = kernel32.OpenMutexW(_SYNCHRONIZE, False, MUTEX_NAME)
    assert probe, "the installer could not see the mutex"
    kernel32.CloseHandle(probe)

    kernel32.CloseHandle(handle)
    assert not kernel32.OpenMutexW(_SYNCHRONIZE, False, MUTEX_NAME)


def test_no_mutex_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(running_mutex.sys, "platform", "darwin")
    monkeypatch.setattr(running_mutex, "_handle", None)
    hold_running_mutex()
    assert running_mutex._handle is None


def test_mutex_name_matches_the_installer_script() -> None:
    from pathlib import Path

    iss = Path(__file__).parents[2] / "packaging" / "windows" / "pam-analyzer.iss"
    text = iss.read_text(encoding="utf-8")
    assert f"RunningMutex = '{MUTEX_NAME}';" in text
    assert text.count(MUTEX_NAME) == 1, "every check should use the RunningMutex constant"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows mutex")
def test_other_instance_running_sees_a_second_process() -> None:
    import subprocess
    import time

    from pam_analyzer.infrastructure.running_mutex import other_instance_running

    hold_running_mutex()
    assert not other_instance_running()
    holder = subprocess.Popen([
        sys.executable, "-c",
        f"import ctypes, time; ctypes.windll.kernel32.CreateMutexW(None, False, {MUTEX_NAME!r}); time.sleep(30)",
    ])
    try:
        time.sleep(2)
        assert other_instance_running()
    finally:
        holder.kill()
        holder.wait()
