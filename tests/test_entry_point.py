"""Startup guards in the entry module, each checked in a subprocess.

Both failure modes end the process outright, so neither can be observed from
inside the test process.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ENTRY = Path(__file__).parents[1] / "src" / "pam_analyzer" / "__main__.py"

# fcntl.h. Nuitka's --windows-console-mode=attach leaves stdout and stderr in
# this mode, where the CRT reads every buffer as UTF-16 and rejects an odd byte
# count.
O_U8TEXT = 0x40000

# STATUS_STACK_BUFFER_OVERRUN, which is what __fastfail reports. The CRT's
# default invalid parameter handler ends the process this way, raising nothing
# Python can catch and reaching no logging handler.
FASTFAIL = 0xC0000409

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows CRT behaviour")


def _run_script(body: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body), *args],
        capture_output=True,
        text=True,
        # A child left in the wide mode emits UTF-8 encoded CJK, which the
        # reader thread cannot decode as cp1252 and would raise on.
        errors="replace",
        timeout=120,
    )


def test_entry_module_is_inert_when_reexecuted_as_a_spawn_worker():
    """Re-executing the entry module under multiprocessing's worker name is a no-op.

    On a spawn start method a worker rebuilds the parent's state by executing
    the main module again, under the name __parents_main__. If the launch at
    the bottom of the module is not guarded, that worker starts a second GUI,
    which spawns workers of its own, and the machine dies. sys.frozen is not
    set in a Nuitka build, so multiprocessing.freeze_support() does not catch
    this. Run in a subprocess because the failure mode is os._exit().
    """
    result = _run_script(
        """
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location("__parents_main__", sys.argv[1])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        print("INERT")
        """,
        str(ENTRY),
    )
    assert "INERT" in result.stdout, (
        f"entry module ran its launch path when re-executed as a worker.\n"
        f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )


# _O_BINARY from fcntl.h. os.O_BINARY only exists on Windows, so it is spelled
# out here to keep the module importable where these tests are skipped.
O_BINARY = 0x8000
O_TEXT = 0x4000

WRITE_TO_STDERR = """
    import ctypes
    import msvcrt

    msvcrt.setmode(2, {mode})
    ctypes.CDLL("ucrtbase.dll")._write(2, {payload!r}, {count})
    """


@windows_only
@pytest.mark.parametrize(
    ("mode", "payload", "survives"),
    [
        (O_BINARY, b"odd", True),
        (O_U8TEXT, b"even", True),
        (O_U8TEXT, b"odd", False),
    ],
    ids=["binary-odd", "wide-even", "wide-odd"],
)
def test_only_an_odd_write_in_wide_mode_ends_the_process(mode, payload, survives):
    """Pin down which factor kills the process, so the guard test cannot go vacuous.

    The first two cases are the ones that matter. If either of them dies, this
    harness is wrong and every conclusion drawn from the third case is worthless.
    Only the combination of a wide mode and an odd byte count may be fatal.

    os.write would not show the fault at all, because CPython brackets its own
    CRT calls with _Py_BEGIN_SUPPRESS_IPH and installs a silent handler for
    them. ctypes reaches _write with the default handler in place, as Qt's
    fprintf does.
    """
    result = _run_script(
        WRITE_TO_STDERR.format(mode=mode, payload=payload, count=len(payload))
    )
    if survives:
        assert result.returncode == 0, (
            f"harness is broken: mode={mode:#x} with {len(payload)} bytes should be "
            f"survivable, got exit={result.returncode:#x}"
        )
    else:
        assert result.returncode & 0xFFFFFFFF == FASTFAIL, (
            f"expected the CRT to end the process with {FASTFAIL:#x}, "
            f"got exit={result.returncode:#x}"
        )


@windows_only
def test_entry_module_returns_the_std_descriptors_to_binary_mode():
    """The entry module clears the wide mode a console attach leaves behind.

    Without this the app dies on the first Qt log line of odd length, which in
    practice is one of the QtLocation lines emitted while the main window is
    built, so the window appears and the process vanishes.
    """
    result = _run_script(
        f"""
        import ctypes
        import importlib.util
        import msvcrt
        import sys

        msvcrt.setmode(2, {O_U8TEXT})

        spec = importlib.util.spec_from_file_location("__parents_main__", sys.argv[1])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        ctypes.CDLL("ucrtbase.dll")._write(2, b"odd", 3)
        """,
        str(ENTRY),
    )
    assert result.returncode == 0, (
        f"an odd length write still ended the process after the guard ran.\n"
        f"exit={result.returncode:#x}\nstderr={result.stderr}"
    )


@windows_only
def test_a_direct_restore_clears_the_wide_mode(tmp_path):
    """Sanity check on setmode itself, independent of the entry module.

    setmode returns the mode that was in effect before the call, which is also
    the only way to read the current one. Reported through a file because both
    std descriptors are in a mode that mangles or rejects ordinary writes.
    """
    report = tmp_path / "mode.txt"
    result = _run_script(
        f"""
        import msvcrt
        import sys
        from pathlib import Path

        msvcrt.setmode(2, {O_U8TEXT})
        msvcrt.setmode(2, {O_BINARY})
        Path(sys.argv[1]).write_text(hex(msvcrt.setmode(2, {O_BINARY})))
        """,
        str(report),
    )
    assert result.returncode == 0, f"probe died: exit={result.returncode:#x}"
    assert report.read_text() == hex(O_BINARY), (
        f"setmode could not restore binary mode on its own, fd 2 reads {report.read_text()}"
    )


@windows_only
def test_entry_module_leaves_stderr_in_binary_mode(tmp_path):
    """What the guard actually achieved, as a mode value rather than as a crash.

    The behavioural test above says only that the process died. This one says
    which mode fd 2 was left in, which is what tells the two candidate causes
    apart.
    """
    report = tmp_path / "mode.txt"
    result = _run_script(
        f"""
        import importlib.util
        import msvcrt
        import sys
        from pathlib import Path

        msvcrt.setmode(2, {O_U8TEXT})

        spec = importlib.util.spec_from_file_location("__parents_main__", sys.argv[1])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        Path(sys.argv[2]).write_text(hex(msvcrt.setmode(2, {O_BINARY})))
        """,
        str(ENTRY),
        str(report),
    )
    assert result.returncode == 0, f"probe died: exit={result.returncode:#x}"
    assert report.read_text() == hex(O_BINARY), (
        f"the guard left fd 2 in mode {report.read_text()}, wanted {hex(O_BINARY)}"
    )


@windows_only
@pytest.mark.parametrize(
    "restore",
    [
        pytest.param(
            f"msvcrt.setmode(2, {O_BINARY})",
            marks=pytest.mark.xfail(
                strict=True,
                reason="binary alone leaves the wide mode that the write path checks",
            ),
        ),
        f"msvcrt.setmode(2, {O_TEXT})",
        f"msvcrt.setmode(2, {O_TEXT})\n        msvcrt.setmode(2, {O_BINARY})",
    ],
    ids=["binary", "text", "text-then-binary"],
)
def test_which_restore_lets_a_narrow_odd_write_through(restore):
    """Pin the call sequence the guard relies on, and the simpler one that fails.

    setmode reporting _O_BINARY is not sufficient, so this asks the only
    question that matters: after this sequence, does an odd length narrow write
    survive? The strict xfail keeps anyone from collapsing the guard back to a
    single setmode call.
    """
    result = _run_script(f"""
        import ctypes
        import msvcrt

        msvcrt.setmode(2, {O_U8TEXT})
        {restore}
        ctypes.CDLL("ucrtbase.dll")._write(2, b"odd", 3)
        """)
    assert result.returncode == 0, (
        f"an odd length write still ended the process, exit={result.returncode:#x}"
    )



# Every check above drives a pipe, because subprocess captures the child's
# output. The compiled app writes to a console, and the CRT takes a different
# write path for one. These reproduce the console case.
CONSOLE_PROBE = '''
import ctypes
import ctypes.wintypes as wintypes
import importlib.util
import msvcrt
import sys
import traceback
from pathlib import Path

entry, report_path, marker, apply_guard = sys.argv[1:5]
report = Path(report_path)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ucrt = ctypes.CDLL("ucrtbase.dll")


class COORD(ctypes.Structure):
    _fields_ = (("X", ctypes.c_short), ("Y", ctypes.c_short))


kernel32.CreateFileW.argtypes = (
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
)
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.SetStdHandle.argtypes = (wintypes.DWORD, wintypes.HANDLE)
kernel32.ReadConsoleOutputCharacterW.argtypes = (
    wintypes.HANDLE,
    wintypes.LPWSTR,
    wintypes.DWORD,
    COORD,
    ctypes.POINTER(wintypes.DWORD),
)
ucrt._open_osfhandle.argtypes = (ctypes.c_void_p, ctypes.c_int)
ucrt._open_osfhandle.restype = ctypes.c_int

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE = ctypes.c_void_p(-1).value
STD_ERROR_HANDLE = 0xFFFFFFF4  # -12 as the DWORD the API takes
O_WRONLY = 0x0001
O_TEXT = 0x4000
O_U8TEXT = 0x40000


def open_conout():
    return kernel32.CreateFileW(
        "CONOUT$",
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )


kernel32.FreeConsole()
if not kernel32.AllocConsole():
    report.write_text("NO CONSOLE")
    raise SystemExit(0)

try:
    # subprocess gave this child pipes for its std handles and AllocConsole
    # leaves those in place, so GetStdHandle still answers with a pipe.
    # CONOUT$ is the only way to reach the console just allocated, and it is
    # what Nuitka opens too.
    write_handle = open_conout()
    read_handle = open_conout()
    if write_handle == INVALID_HANDLE or read_handle == INVALID_HANDLE:
        raise OSError("CONOUT$ could not be opened")

    # Nuitka's inheritAttachedConsole(), the stderr half, in the order it runs
    # in HelpersConsole.c.
    kernel32.SetStdHandle(STD_ERROR_HANDLE, write_handle)
    fd = ucrt._open_osfhandle(write_handle, O_WRONLY | O_TEXT)
    if fd < 0:
        raise OSError("_open_osfhandle rejected the console handle")
    ucrt._dup2(fd, 2)
    ucrt._close(fd)
    msvcrt.setmode(2, O_U8TEXT)

    if apply_guard == "1":
        spec = importlib.util.spec_from_file_location("__parents_main__", entry)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    # Narrow bytes through the CRT, which is how Qt emits its log lines.
    payload = marker.encode("ascii")
    ucrt._write(2, payload, len(payload))

    screen = ctypes.create_unicode_buffer(256)
    read = wintypes.DWORD(0)
    kernel32.ReadConsoleOutputCharacterW(
        read_handle, screen, 256, COORD(0, 0), ctypes.byref(read)
    )
    report.write_text(screen[: read.value].rstrip(), encoding="utf-8")
except BaseException:
    report.write_text("ERROR\\n" + traceback.format_exc(), encoding="utf-8")
'''

# Even length, so an unguarded run garbles the text instead of ending the
# process, which would say nothing about how it was rendered.
CONSOLE_MARKER = "NARROWTEXT"


def _run_console_probe(tmp_path, marker: str, apply_guard: bool):
    report = tmp_path / "screen.txt"
    result = _run_script(
        CONSOLE_PROBE, str(ENTRY), str(report), marker, "1" if apply_guard else "0"
    )
    text = report.read_text(encoding="utf-8") if report.exists() else ""
    if text == "NO CONSOLE":
        pytest.skip("no console could be allocated in this session")
    assert not text.startswith("ERROR"), f"probe failed before it could report:\n{text}"
    return result, text


@windows_only
@pytest.mark.parametrize("apply_guard", [False, True], ids=["unguarded", "guarded"])
def test_the_guard_clears_the_wide_mode_on_a_real_console(tmp_path, apply_guard):
    """Read back what the console rendered, rather than what a pipe received.

    The unguarded case has to render something, and that something has to be
    wrong. An empty screen means the write never reached the console, which
    would let the guarded case pass without testing anything.
    """
    result, screen = _run_console_probe(tmp_path, CONSOLE_MARKER, apply_guard)
    assert result.returncode == 0, f"probe died: exit={result.returncode:#x}"
    assert screen, "harness is broken: nothing reached the console screen buffer"
    if apply_guard:
        assert screen.startswith(CONSOLE_MARKER), (
            f"the guard did not restore narrow writes on a console, "
            f"the console shows {screen!r}"
        )
    else:
        assert not screen.startswith(CONSOLE_MARKER), (
            "harness is broken: an unguarded narrow write rendered correctly, "
            "so the wide mode was never in effect"
        )


@windows_only
def test_an_odd_length_narrow_write_survives_on_a_real_console(tmp_path):
    """The length that ends the process, checked against a console rather than a pipe."""
    result, screen = _run_console_probe(tmp_path, "NARROWODD", apply_guard=True)
    assert result.returncode == 0, (
        f"an odd length write still ended the process, exit={result.returncode:#x}"
    )
    assert screen.startswith("NARROWODD"), f"the console shows {screen!r}"


# Nuitka does not put the console on fds 1 and 2. In a GUI subsystem process
# those slots are already taken by _NO_CONSOLE_FILENO, freopen cannot reuse
# them, and the console lands on the first free descriptors instead, in
# practice 3 and 4. A guard over the literals 1 and 2 succeeds on the dead
# slots and leaves the live one wide.
HIGH_FD_PROBE = '''
import ctypes
import ctypes.wintypes as wintypes
import importlib.util
import msvcrt
import sys
import traceback
from pathlib import Path

entry, report_path = sys.argv[1:3]
report = Path(report_path)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ucrt = ctypes.CDLL("ucrtbase.dll")


class COORD(ctypes.Structure):
    _fields_ = (("X", ctypes.c_short), ("Y", ctypes.c_short))


kernel32.CreateFileW.argtypes = (
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
)
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.ReadConsoleOutputCharacterW.argtypes = (
    wintypes.HANDLE,
    wintypes.LPWSTR,
    wintypes.DWORD,
    COORD,
    ctypes.POINTER(wintypes.DWORD),
)
ucrt._open_osfhandle.argtypes = (ctypes.c_void_p, ctypes.c_int)
ucrt._open_osfhandle.restype = ctypes.c_int

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE = ctypes.c_void_p(-1).value
O_WRONLY = 0x0001
O_TEXT = 0x4000
O_U8TEXT = 0x40000


def open_conout():
    return kernel32.CreateFileW(
        "CONOUT$",
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )


original_stderr = sys.stderr

kernel32.FreeConsole()
if not kernel32.AllocConsole():
    report.write_text("NO CONSOLE")
    raise SystemExit(0)

try:
    write_handle = open_conout()
    read_handle = open_conout()
    if write_handle == INVALID_HANDLE or read_handle == INVALID_HANDLE:
        raise OSError("CONOUT$ could not be opened")

    # Deliberately not duplicated onto 2. This is the arrangement Nuitka
    # leaves behind, and the one the guard has to cope with.
    console_fd = ucrt._open_osfhandle(write_handle, O_WRONLY | O_TEXT)
    if console_fd in (0, 1, 2):
        raise OSError("wanted a descriptor above the standard three, got %d" % console_fd)
    msvcrt.setmode(console_fd, O_U8TEXT)

    class Console:
        """Stands in for the stream CPython builds from the CRT stderr FILE*.

        write and flush are no-ops rather than absent. CPython flushes
        sys.stderr while shutting down and exits 120 when that raises, which
        would mask the exit code this test is here to check.
        """

        def fileno(self):
            return console_fd

        def write(self, text):
            return len(text)

        def flush(self):
            pass

    sys.stderr = Console()

    def narrow(text):
        payload = text.encode("ascii")
        ucrt._write(console_fd, payload, len(payload))

    narrow("BEFOREMARK")

    spec = importlib.util.spec_from_file_location("__parents_main__", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    narrow("AFTERMARKS")
    narrow("ODDMARK")

    screen = ctypes.create_unicode_buffer(256)
    read = wintypes.DWORD(0)
    kernel32.ReadConsoleOutputCharacterW(
        read_handle, screen, 256, COORD(0, 0), ctypes.byref(read)
    )
    report.write_text(screen[: read.value].rstrip(), encoding="utf-8")
except BaseException:
    report.write_text("ERROR\\n" + traceback.format_exc(), encoding="utf-8")
finally:
    sys.stderr = original_stderr
'''


@windows_only
def test_the_guard_follows_the_console_off_the_standard_descriptors(tmp_path):
    """The console is on fd 3 or above, and the guard has to correct that one.

    A guard over the literals 1 and 2 passes every other test in this file,
    because a plain Python process has its console on 1 and 2. It still leaves
    the compiled app garbling every Qt line and dying on the first of odd
    length. BEFOREMARK proves the wide mode was really in effect, so a pass
    here cannot be vacuous.
    """
    report = tmp_path / "screen.txt"
    result = _run_script(HIGH_FD_PROBE, str(ENTRY), str(report))
    screen = report.read_text(encoding="utf-8") if report.exists() else ""
    if screen == "NO CONSOLE":
        pytest.skip("no console could be allocated in this session")
    assert not screen.startswith("ERROR"), f"probe failed before it could report:\n{screen}"
    assert result.returncode == 0, (
        f"the odd length write ended the process, exit={result.returncode:#x}"
    )
    assert screen, "harness is broken: nothing reached the console screen buffer"
    assert "BEFOREMARK" not in screen, (
        f"harness is broken: the pre-guard write rendered correctly, so the "
        f"wide mode was never in effect. The console shows {screen!r}"
    )
    assert "AFTERMARKS" in screen, (
        f"the guard did not follow the console to fd 3 or above. "
        f"The console shows {screen!r}"
    )
    assert "ODDMARK" in screen, (
        f"an odd length write did not reach the console. The console shows {screen!r}"
    )
