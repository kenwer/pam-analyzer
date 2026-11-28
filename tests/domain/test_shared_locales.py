"""The species language list offered to the user is the intersection of what
the shipped models can localize into, not the union."""

from pam_analyzer.domain import shared_locales
from tests.conftest import FakeRunner as _BaseFakeRunner


class FakeRunner(_BaseFakeRunner):
    def __init__(self, locales: list[str]) -> None:
        self._locales = locales

    def available_locales(self) -> list[str]:
        return list(self._locales)


def test_only_locales_every_model_ships_are_offered():
    runners = [
        FakeRunner(["it", "en_us", "de"]),
        FakeRunner(["ca", "de", "en_us"]),
    ]
    # Sorted, whatever order the runners list them in

    assert shared_locales(runners) == ("de", "en_us")
