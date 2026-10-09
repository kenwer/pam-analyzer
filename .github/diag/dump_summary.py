"""Print what a Windows minidump says about a crash, using only the stdlib.

For each dump: the exception code and address, the module the faulting
instruction belongs to, a scan of the faulting thread's stack for values that
point into loaded modules (an approximate call stack without symbols), and the
loaded modules with their file versions.

Usage: python dump_summary.py DUMP [DUMP ...]
"""

import struct
import sys
from bisect import bisect_right
from pathlib import Path

THREAD_LIST, MODULE_LIST, EXCEPTION, MEMORY64_LIST = 3, 4, 6, 9
CONTEXT_RSP, CONTEXT_RIP = 0x98, 0xF8
STACK_SCAN_BYTES = 64 * 1024


class Dump:
    def __init__(self, data: bytes) -> None:
        self.data = data
        signature, _version, count, directory = struct.unpack_from("<4sIII", data, 0)
        if signature != b"MDMP":
            raise ValueError("not a minidump")
        self.streams = {}
        for i in range(count):
            kind, size, rva = struct.unpack_from("<III", data, directory + 12 * i)
            self.streams[kind] = (rva, size)
        self.modules = self._read_modules()
        self.ranges = self._read_memory64()

    def _read_modules(self) -> list[tuple[int, int, str, str]]:
        rva, _ = self.streams[MODULE_LIST]
        (count,) = struct.unpack_from("<I", self.data, rva)
        modules = []
        for i in range(count):
            off = rva + 4 + 108 * i
            base, size, _checksum, _stamp, name_rva = struct.unpack_from("<QIIII", self.data, off)
            ms, ls = struct.unpack_from("<II", self.data, off + 24 + 8)
            version = f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
            (name_len,) = struct.unpack_from("<I", self.data, name_rva)
            name = self.data[name_rva + 4 : name_rva + 4 + name_len].decode("utf-16-le")
            modules.append((base, size, name, version))
        return sorted(modules)

    def _read_memory64(self) -> list[tuple[int, int, int]]:
        """(virtual address, size, file offset) per captured range, sorted."""
        if MEMORY64_LIST not in self.streams:
            return []
        rva, _ = self.streams[MEMORY64_LIST]
        count, file_off = struct.unpack_from("<QQ", self.data, rva)
        ranges = []
        for i in range(count):
            start, size = struct.unpack_from("<QQ", self.data, rva + 16 + 16 * i)
            ranges.append((start, size, file_off))
            file_off += size
        return ranges

    def read(self, address: int, size: int) -> bytes:
        i = bisect_right(self.ranges, (address, 1 << 64, 0)) - 1
        if i < 0:
            return b""
        start, length, file_off = self.ranges[i]
        if address >= start + length:
            return b""
        size = min(size, start + length - address)
        return self.data[file_off + address - start : file_off + address - start + size]

    def locate(self, address: int) -> str | None:
        for base, size, name, _ in self.modules:
            if base <= address < base + size:
                return f"{Path(name.replace('\\', '/')).name}+0x{address - base:x}"
        return None

    def thread_context(self, thread_id: int) -> tuple[int, int] | None:
        """(rip, rsp) from the saved context of thread_id."""
        rva, _ = self.streams[THREAD_LIST]
        (count,) = struct.unpack_from("<I", self.data, rva)
        for i in range(count):
            off = rva + 4 + 48 * i
            (tid,) = struct.unpack_from("<I", self.data, off)
            if tid == thread_id:
                _ctx_size, ctx_rva = struct.unpack_from("<II", self.data, off + 40)
                (rsp,) = struct.unpack_from("<Q", self.data, ctx_rva + CONTEXT_RSP)
                (rip,) = struct.unpack_from("<Q", self.data, ctx_rva + CONTEXT_RIP)
                return rip, rsp
        return None


def summarize(path: Path) -> None:
    dump = Dump(path.read_bytes())
    print(f"==== {path.name} ({path.stat().st_size / 1e6:.0f} MB)")

    if EXCEPTION not in dump.streams:
        print("no exception stream")
        return
    rva, _ = dump.streams[EXCEPTION]
    thread_id, _align, code, _flags, _record, address, nparams, _align2 = struct.unpack_from(
        "<IIIIQQII", dump.data, rva
    )
    params = struct.unpack_from(f"<{min(nparams, 15)}Q", dump.data, rva + 40)
    print(f"exception 0x{code:08x} in thread 0x{thread_id:x}")
    print(f"  at 0x{address:x}  {dump.locate(address) or '(not in any module)'}")
    if code == 0xC0000005 and len(params) >= 2:
        kind = {0: "read", 1: "write", 8: "execute"}.get(params[0], str(params[0]))
        print(f"  access violation: {kind} of 0x{params[1]:x}")
    else:
        print("  parameters:", " ".join(f"0x{p:x}" for p in params))

    # The exception stream's own context record is the faulting one. The
    # thread list entry holds the same registers for a dump taken at the fault.
    ctx_size, ctx_rva = struct.unpack_from("<II", dump.data, rva + 8 + 152)
    if ctx_size:
        (rsp,) = struct.unpack_from("<Q", dump.data, ctx_rva + CONTEXT_RSP)
        (rip,) = struct.unpack_from("<Q", dump.data, ctx_rva + CONTEXT_RIP)
    else:
        rip, rsp = dump.thread_context(thread_id) or (0, 0)
    print(f"  rip 0x{rip:x}  {dump.locate(rip) or ''}")

    print("stack scan of the faulting thread (module pointers only, not a real unwind):")
    stack = dump.read(rsp, STACK_SCAN_BYTES)
    shown = 0
    for i in range(0, len(stack) - 7, 8):
        (value,) = struct.unpack_from("<Q", stack, i)
        where = dump.locate(value)
        if where:
            print(f"  rsp+0x{i:05x}  {where}")
            shown += 1
            if shown >= 80:
                break
    if not stack:
        print("  (stack memory not in the dump)")

    print("modules:")
    for base, size, name, version in dump.modules:
        print(f"  0x{base:016x} {size:>10} {version:<22} {name}")


def main() -> None:
    for arg in sys.argv[1:]:
        try:
            summarize(Path(arg))
        except Exception as exc:  # noqa: BLE001
            print(f"==== {arg}: could not parse: {exc!r}")
        print()


if __name__ == "__main__":
    main()
