"""PyInstaller runtime hook for Windows.

1. Prepend _MEIPASS to PATH so TensorFlow's self_check.py can find its DLLs
   via ctypes.WinDLL() (which searches %PATH%, not sys.path).

2. Reattach stdout/stderr to the parent console when the app is launched from
   a terminal. The build uses --noconsole (GUI subsystem) so Windows never
   opens a console window on double-click, but that also detaches the standard
   streams. AttachConsole(-1) reconnects them to the parent process's console
   when one exists, so output remains visible when the app is run from cmd.
"""

import os
import sys

if sys.platform == 'win32' and hasattr(sys, '_MEIPASS'):
    os.environ['PATH'] = sys._MEIPASS + os.pathsep + os.environ.get('PATH', '')

    import ctypes

    if ctypes.windll.kernel32.AttachConsole(-1):  # -1 = ATTACH_PARENT_PROCESS
        sys.stdout = open('CONOUT$', 'w')
        sys.stderr = open('CONOUT$', 'w')
