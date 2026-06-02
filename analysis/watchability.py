from __future__ import annotations

"""TV watchability classification for games.

A game is "watchable" if it airs on a national network (everyone can watch) or
on one of the regional/streaming networks the user has access to (configured via
the TV_NETWORKS env var → cfg["tv_networks"]).
"""


# National broadcasts — watchable by everyone in the US regardless of location.
# Normalized to lower-case for case-insensitive matching.
NATIONAL_NETWORKS: frozenset[str] = frozenset(
    n.lower()
    for n in (
        # NBA
        "ESPN", "ESPN2", "ESPN3", "ABC", "TNT", "truTV", "NBA TV",
        # NHL
        "TNT", "truTV", "ESPN", "ABC", "ESPN+", "Hulu",
        # MLB
        "FOX", "FS1", "FS2", "MLB Network", "TBS", "ESPN", "Apple TV+",
        "Apple TV", "Roku", "Peacock",
        # NFL
        "CBS", "FOX", "NBC", "ESPN", "ABC", "NFL Network", "Prime Video",
        "Amazon Prime Video", "Netflix",
        # Generic national streamers
        "Max", "Peacock",
    )
)


def _normalize(name: str) -> str:
    return (name or "").strip().lower()


def is_watchable(broadcasts: list[dict], user_networks: set[str]) -> bool:
    """Return True if any broadcast is national or matches a user network.

    broadcasts: list of {"network": str, "market": "national"|"home"|"away"}
    user_networks: set of network names the user can access (case-insensitive).

    Matching is bidirectional substring so "NBC Sports Bay Area" in the user's
    list matches a broadcast labeled "NBC Sports Bay Area" (and vice-versa for
    partial entries like "Bally Sports").
    """
    if not broadcasts:
        return False

    user_norm = {_normalize(n) for n in user_networks if _normalize(n)}

    for b in broadcasts:
        net = _normalize(b.get("network", ""))
        if not net:
            continue
        market = _normalize(b.get("market", ""))

        # National broadcast → always watchable.
        if market == "national" or net in NATIONAL_NETWORKS:
            return True

        # Regional broadcast → watchable only if the user has that network.
        for u in user_norm:
            if u and (u in net or net in u):
                return True

    return False


def watchable_label(broadcasts: list[dict]) -> str:
    """Return a short, de-duplicated network string for display, e.g. "ABC" or
    "TNT, NBC Sports Bay Area". National networks are listed first. Empty if no
    broadcast data is available."""
    if not broadcasts:
        return ""

    national: list[str] = []
    regional: list[str] = []
    seen: set[str] = set()

    for b in broadcasts:
        net = (b.get("network") or "").strip()
        if not net:
            continue
        key = net.lower()
        if key in seen:
            continue
        seen.add(key)
        market = (b.get("market") or "").strip().lower()
        if market == "national" or key in NATIONAL_NETWORKS:
            national.append(net)
        else:
            regional.append(net)

    return ", ".join(national + regional)
