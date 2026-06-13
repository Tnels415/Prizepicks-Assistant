"""Tests for injury severity tiering and cross-source name normalization."""
from __future__ import annotations

import pytest

from data.injury_client import classify_injury_severity
from data.draftkings_client import _normalize_name


class TestInjurySeverity:
    @pytest.mark.parametrize("text", ["Out", "OUT", "Inactive", "Injured Reserve", "Suspended"])
    def test_out_tier(self, text):
        assert classify_injury_severity(text) == "out"

    def test_doubtful(self):
        assert classify_injury_severity("Doubtful") == "doubtful"

    @pytest.mark.parametrize("text", ["Questionable", "Game-Time Decision", "GTD"])
    def test_questionable_tier(self, text):
        assert classify_injury_severity(text) == "questionable"

    @pytest.mark.parametrize("text", ["Probable", "Day-To-Day", "DTD"])
    def test_probable_tier(self, text):
        assert classify_injury_severity(text) == "probable"

    def test_none_and_empty(self):
        assert classify_injury_severity(None) == "none"
        assert classify_injury_severity("") == "none"

    def test_unknown_is_conservative(self):
        assert classify_injury_severity("Mystery Tag") == "questionable"


class TestNameNormalization:
    def test_accents_stripped(self):
        assert _normalize_name("Luka Dončić") == _normalize_name("Luka Doncic")

    def test_suffix_stripped(self):
        assert _normalize_name("P.J. Washington Jr.") == _normalize_name("PJ Washington")

    def test_case_and_spacing(self):
        assert _normalize_name("  LeBron   JAMES ") == "lebron james"
