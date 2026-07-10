"""Regression test: PropLineClient.fetch_props must retry the FULL
multi-source fetch (Bovada/DraftKings/Underdog/FanDuel), not just
PrizePicks. The old retry loop was gated on PrizePicks's own reason code,
which made it dead code once PrizePicks became chronically bot-blocked —
every other source only ever got tried once, no matter how many retries
were configured."""
from __future__ import annotations

import data.draftkings_client as dk
import data.external_props_client as ext
import data.prizepicks_client as pc


class _FakePP:
    _REASON_OK = "ok"
    _REASON_BLOCKED = "blocked"
    _REASON_NOT_POSTED = "not_posted"
    _REASON_ERROR = "error"

    def __init__(self):
        _FakePP.calls = getattr(_FakePP, "calls", 0) + 1

    def fetch_props(self, cfg):
        return [], _FakePP._REASON_BLOCKED


class _Empty:
    def fetch_props(self, cfg):
        return []


def _patch_sources(monkeypatch, bovada_cls):
    monkeypatch.setattr(pc, "PrizePicksLiveClient", _FakePP)
    monkeypatch.setattr(dk, "DraftKingsPropsClient", _Empty)
    monkeypatch.setattr(ext, "UnderdogPropsClient", _Empty)
    monkeypatch.setattr(ext, "FanDuelPropsClient", _Empty)
    monkeypatch.setattr(ext, "BovadaPropsClient", bovada_cls)
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)


SPORT_CFG = {"name": "MLB", "prop_stat_map": {"Points": "Points"}}


class TestMultiSourceRetry:
    def test_retries_all_sources_when_all_empty(self, monkeypatch):
        calls = {"n": 0}

        class AlwaysEmpty:
            def fetch_props(self, cfg):
                calls["n"] += 1
                return []

        _patch_sources(monkeypatch, AlwaysEmpty)
        client = pc.PropLineClient()
        result = client.fetch_props(SPORT_CFG)
        assert result == []
        # initial attempt + _RETRY_ATTEMPTS retries, all hitting Bovada
        assert calls["n"] == 1 + client._RETRY_ATTEMPTS

    def test_finds_props_on_a_later_retry(self, monkeypatch):
        # Regression case: a source that's empty on the first pass but has
        # props by the second retry must be picked up. The old design only
        # ever retried PrizePicks (which stays BLOCKED), so this scenario
        # always returned empty even though Bovada had props seconds later.
        calls = {"n": 0}

        class EmptyThenSucceeds:
            def fetch_props(self, cfg):
                calls["n"] += 1
                if calls["n"] >= 2:
                    return [{"player_name": "Test Player", "stat_type": "Points",
                              "line": 20.5, "prop_source": "Bovada"}]
                return []

        _patch_sources(monkeypatch, EmptyThenSucceeds)
        client = pc.PropLineClient()
        result = client.fetch_props(SPORT_CFG)
        assert len(result) == 1
        assert result[0]["player_name"] == "Test Player"

    def test_stops_retrying_once_a_source_succeeds(self, monkeypatch):
        calls = {"n": 0}

        class SucceedsImmediately:
            def fetch_props(self, cfg):
                calls["n"] += 1
                return [{"player_name": "P", "stat_type": "Points",
                          "line": 1.5, "prop_source": "Bovada"}]

        _patch_sources(monkeypatch, SucceedsImmediately)
        client = pc.PropLineClient()
        client.fetch_props(SPORT_CFG)
        assert calls["n"] == 1  # no wasted retries once it works
