from pam_analyzer.domain import Detection, filter_top_per_aru_species


def _detection(campaign: str, aru: str, species: str, confidence: float) -> Detection:
    return Detection(
        campaign=campaign,
        aru=aru,
        week=1,
        species=species,
        scientific_name=species,
        confidence=confidence,
        start_time=0.0,
        end_time=3.0,
        rank=1,
        file=f"{campaign}/{aru}/{species}.wav",
    )


def test_filter_top_per_aru_species_keeps_highest_confidence():
    rows = [
        _detection("c", "ARU1", "Robin", 0.5),
        _detection("c", "ARU1", "Robin", 0.9),
        _detection("c", "ARU1", "Robin", 0.7),
        _detection("c", "ARU1", "Crow", 0.6),
        _detection("c", "ARU2", "Robin", 0.4),
    ]
    out = filter_top_per_aru_species(rows, max_per_pair=2)
    # Robin@ARU1: keep 0.9 and 0.7; Crow@ARU1: keep 0.6; Robin@ARU2: keep 0.4
    confidences = sorted(d.confidence for d in out)
    assert confidences == [0.4, 0.6, 0.7, 0.9]


def test_filter_top_per_aru_species_disabled_returns_all():
    rows = [_detection("c", "A", "S", 0.1) for _ in range(5)]
    assert filter_top_per_aru_species(rows, max_per_pair=0) == rows
