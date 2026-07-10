"""Tests for main._handle_no_props_today: the alert-once / retry-silently /
give-up-at-cutoff state machine that stops the 30-min scheduler from
spamming an identical email while it keeps retrying for lines to post."""
from __future__ import annotations

import sys
import types

# Stub nba_api so main.py's transitive imports succeed without the package.
for _mod in [
    "nba_api", "nba_api.stats", "nba_api.stats.static",
    "nba_api.stats.static.teams", "nba_api.stats.endpoints",
    "nba_api.stats.endpoints.leaguedashplayerstats",
    "nba_api.stats.endpoints.playergamelog",
    "nba_api.stats.endpoints.leaguedashteamstats",
    "nba_api.stats.endpoints.commonplayerinfo",
]:
    sys.modules.setdefault(_mod, types.ModuleType(_mod))

from datetime import date  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

import main  # noqa: E402


@pytest.fixture
def isolated_markers(tmp_path, monkeypatch):
    """Point the module-level marker paths at a scratch dir for each test."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(main, "_NO_PROPS_ALERT_MARKER", Path("logs/no_props_alerted.date"))
    monkeypatch.setattr(main, "_NO_PROPS_GAVE_UP_MARKER", Path("logs/no_props_gave_up.date"))
    return tmp_path


def _sender():
    calls = []

    def send(subject, html, plain):
        calls.append(subject)
        return True

    return send, calls


class TestHandleNoPropsToday:
    def test_first_call_alerts_and_retries(self, isolated_markers, monkeypatch):
        send, calls = _sender()
        monkeypatch.setattr(main, "_pacific_hour_and_date_str", lambda: (10, "2026-07-15"))
        rc = main._handle_no_props_today(send, date(2026, 7, 15), ["MLB"])
        assert rc == 1
        assert len(calls) == 1
        assert "yet" in calls[0]

    def test_second_call_same_day_stays_silent(self, isolated_markers, monkeypatch):
        send, calls = _sender()
        monkeypatch.setattr(main, "_pacific_hour_and_date_str", lambda: (10, "2026-07-15"))
        main._handle_no_props_today(send, date(2026, 7, 15), ["MLB"])
        rc = main._handle_no_props_today(send, date(2026, 7, 15), ["MLB"])
        assert rc == 1
        assert len(calls) == 1  # no second email

    def test_cutoff_sends_final_email_and_stops(self, isolated_markers, monkeypatch):
        send, calls = _sender()
        monkeypatch.setattr(main, "_pacific_hour_and_date_str", lambda: (10, "2026-07-15"))
        main._handle_no_props_today(send, date(2026, 7, 15), ["MLB"])
        monkeypatch.setattr(main, "_pacific_hour_and_date_str", lambda: (21, "2026-07-15"))
        rc = main._handle_no_props_today(send, date(2026, 7, 15), ["MLB"])
        assert rc == 0
        assert len(calls) == 2
        assert "ALL DAY" in calls[1]

    def test_after_giving_up_stays_silent_rest_of_day(self, isolated_markers, monkeypatch):
        send, calls = _sender()
        monkeypatch.setattr(main, "_pacific_hour_and_date_str", lambda: (21, "2026-07-15"))
        main._handle_no_props_today(send, date(2026, 7, 15), ["MLB"])  # triggers give-up directly
        rc = main._handle_no_props_today(send, date(2026, 7, 15), ["MLB"])
        assert rc == 0
        assert len(calls) == 1  # no repeat "gave up" email

    def test_next_day_resets_the_cycle(self, isolated_markers, monkeypatch):
        send, calls = _sender()
        monkeypatch.setattr(main, "_pacific_hour_and_date_str", lambda: (21, "2026-07-15"))
        main._handle_no_props_today(send, date(2026, 7, 15), ["MLB"])  # gives up for the 15th
        monkeypatch.setattr(main, "_pacific_hour_and_date_str", lambda: (9, "2026-07-16"))
        rc = main._handle_no_props_today(send, date(2026, 7, 16), ["MLB"])
        assert rc == 1  # fresh alert cycle on a new date
        assert len(calls) == 2

    def test_send_failure_does_not_mark_alerted(self, isolated_markers, monkeypatch):
        # If the alert email itself fails to send, don't silently suppress
        # future attempts to notify — retry sending the alert next time too.
        monkeypatch.setattr(main, "_pacific_hour_and_date_str", lambda: (10, "2026-07-15"))
        calls = []

        def failing_send(subject, html, plain):
            calls.append(subject)
            return False

        rc = main._handle_no_props_today(failing_send, date(2026, 7, 15), ["MLB"])
        assert rc == 1
        assert not main._NO_PROPS_ALERT_MARKER.exists()
        # Next call should retry sending since the marker was never written.
        rc2 = main._handle_no_props_today(failing_send, date(2026, 7, 15), ["MLB"])
        assert len(calls) == 2
