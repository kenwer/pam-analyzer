"""The species language list offered to the user is the intersection of what
the shipped models can localize into, not the union."""

from pam_analyzer.domain import shared_locales


class FakeRunner:
    def __init__(self, locales: list[str]) -> None:
        self._locales = locales

    def available_locales(self) -> list[str]:
        return list(self._locales)


def test_only_locales_every_model_ships_are_offered():
    runners = [
        FakeRunner(["de", "en_us", "it"]),
        FakeRunner(["ca", "de", "en_us"]),
    ]

    assert shared_locales(runners) == ("de", "en_us")


def test_result_is_sorted():
    runners = [FakeRunner(["fr", "de", "en_us"]), FakeRunner(["en_us", "de", "fr"])]

    assert shared_locales(runners) == ("de", "en_us", "fr")


def test_one_runner_offers_its_own_locales():
    assert shared_locales([FakeRunner(["de", "en_us"])]) == ("de", "en_us")


def test_no_runners_offers_nothing():
    assert shared_locales([]) == ()
