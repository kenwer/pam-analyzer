"""Tests for domain/audio_import.py: birdnet_week boundaries and CardQueue behaviour."""

from datetime import datetime
from pathlib import Path

import pytest

from pam_analyzer.domain.audio_import import (
    CardQueue,
    DetectedCard,
    birdnet_week,
)

# birdnet_week

@pytest.mark.parametrize(
    "month, day, expected",
    [
        (1, 1, 1),    # Jan 1 -> week 1
        (1, 7, 1),    # Jan 7 -> week 1
        (1, 8, 2),    # Jan 8 -> week 2
        (1, 31, 5),   # Jan 31 -> week 5 (ceil(31/7)=5)
        (3, 1, 9),    # Mar 1 -> (2)*4+1 = 9
        (12, 31, 48), # Dec 31 -> min(48, 44+5) = 48
        (12, 1, 45),  # Dec 1 -> 44+1 = 45
    ],
)
def test_birdnet_week_boundaries(month, day, expected):
    dt = datetime(2024, month, day)
    assert birdnet_week(dt) == expected


def test_birdnet_week_capped_at_48():
    dt = datetime(2024, 12, 31)
    assert birdnet_week(dt) <= 48


# CardQueue

def _card(name: str) -> DetectedCard:
    return DetectedCard(name=name, mountpoint=Path("/mnt") / name, device=f"/dev/{name}")


def test_card_queue_offer_and_pop():
    q = CardQueue()
    q.offer([_card("A"), _card("B")])
    assert q.pop() == _card("A")
    assert q.pop() == _card("B")
    assert q.pop() is None


def test_card_queue_dedup():
    q = CardQueue()
    q.offer([_card("A"), _card("A")])
    assert q.pop() == _card("A")
    assert q.pop() is None


def test_card_queue_seen_across_offers():
    q = CardQueue()
    q.offer([_card("A")])
    q.pop()
    q.offer([_card("A")])  # already seen; should not be re-added
    assert q.pop() is None


def test_card_queue_clear_seen_allows_requeue():
    q = CardQueue()
    q.offer([_card("A")])
    q.pop()
    q.clear_seen()
    q.offer([_card("A")])
    assert q.pop() == _card("A")


def test_card_queue_reset():
    q = CardQueue()
    q.offer([_card("A"), _card("B")])
    q.reset()
    assert q.pending == []
    q.offer([_card("A")])  # should work after reset
    assert len(q.pending) == 1


def test_card_queue_pending_does_not_mutate():
    q = CardQueue()
    q.offer([_card("A")])
    snapshot = q.pending
    snapshot.clear()
    assert len(q.pending) == 1
