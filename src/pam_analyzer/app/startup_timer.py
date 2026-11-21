"""Wall-clock phases of the app launch, for diagnosing slow startups."""

import time


class StartupTimer:
    """Records consecutive startup phases and formats them as one log line.

    Each phase runs from the previous mark to its own. A "bootstrap" phase
    from process creation to the timer's creation leads the summary. It
    covers the interpreter start, the frozen build's DLL loading and the outer
    entry module, none of which Python code can time from the inside.
    """

    def __init__(self) -> None:
        self._start_wall = time.time()
        self._start = self._last = time.perf_counter()
        self._phases: list[tuple[str, float]] = []

    def mark(self, phase: str) -> None:
        now = time.perf_counter()
        self._phases.append((phase, now - self._last))
        self._last = now

    def summary(self) -> str:
        phases = self._phases
        bootstrap = self._bootstrap()
        if bootstrap is not None:
            phases = [("bootstrap", bootstrap), *phases]
        total = sum(seconds for _, seconds in phases)
        details = ", ".join(f"{name} {seconds:.2f}" for name, seconds in phases)
        return f"Startup: {total:.2f} s ({details})"

    def _bootstrap(self) -> float | None:
        # Imported here as psutil is not cheap and the timer must load before anything else
        import psutil

        try:
            created = psutil.Process().create_time()
        except psutil.Error:
            return None
        # Clamped as create_time is rounded to clock ticks on Linux
        return max(0.0, self._start_wall - created)
