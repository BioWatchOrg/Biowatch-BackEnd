"""Tests for sentinel.backfill."""

import pytest

from sentinel import backfill_sentinel, month_range


def test_month_range_crosses_years():
    assert month_range("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_month_range_single_month():
    assert month_range("2016-01", "2016-01") == ["2016-01"]


def test_month_range_full_history_length():
    assert len(month_range("2016-01", "2026-09")) == 10 * 12 + 9


@pytest.mark.parametrize(("first", "last"), [("2016", "2016-03"), ("2016-01", "2016-13")])
def test_month_range_rejects_non_monthly(first, last):
    with pytest.raises(ValueError, match="mensuel"):
        month_range(first, last)


def test_month_range_rejects_reversed_range():
    with pytest.raises(ValueError, match="plage vide"):
        month_range("2026-02", "2025-11")


def test_backfill_runs_every_month_in_order():
    calls = []
    failed = backfill_sentinel(
        "idf", "2016-01", "2016-03", 8, extract=lambda **kw: calls.append(kw), io_threads=4
    )
    assert failed == []
    assert [call["bucket"] for call in calls] == ["2016-01", "2016-02", "2016-03"]
    assert calls[0] == {
        "aoi_label": "idf",
        "bucket": "2016-01",
        "resolution": 8,
        "source_version": "2.0.0",
        "io_threads": 4,
    }


def test_backfill_continues_after_a_failed_month():
    done = []

    def extract(**kw):
        if kw["bucket"] == "2016-02":
            raise RuntimeError("tunnel SSH coupé")
        done.append(kw["bucket"])

    failed = backfill_sentinel("idf", "2016-01", "2016-03", 8, extract=extract)
    assert failed == ["2016-02"]
    assert done == ["2016-01", "2016-03"]


def test_backfill_does_not_swallow_keyboard_interrupt():
    def extract(**kw):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        backfill_sentinel("idf", "2016-01", "2016-02", 8, extract=extract)
