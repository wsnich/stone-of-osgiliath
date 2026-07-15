r"""
Regression tests for the Discord singles filter (monitors/discord_monitor.py).

Pure-logic, no network/browser. Run directly:

    python test_discord_filter.py

Exit code 0 = all pass, 1 = at least one failure.

Focus: the sealed_words bypass must let genuine SEALED product through even
when its title carries a set-code token (e.g. "TDM-2025") that the singles
regex \b[a-z]{3,4}-?\d{1,4}\b would otherwise match, while still dropping
individual cards.
"""

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from monitors.discord_monitor import DiscordMonitor

CONFIG = {
    "discord": {
        # Keywords the messages below must hit to reach the singles stage
        # (the singles filter runs AFTER keyword matching).
        "keywords": [
            "marvel super heroes", "tarkir dragonstorm", "bloomburrow",
            "spider-man", "surging sparks", "jumpstart",
        ],
        "exclude_singles": True,
        "min_price": 0,
    },
    "products": [],
}

_mon = DiscordMonitor()


def _msg(content):
    return {
        "id": "1", "channel_id": "chan", "content": content,
        "author": {"username": "dealbot"}, "embeds": [],
        "timestamp": "2026-07-15T00:00:00Z",
    }


def run(name, content, expect_shown):
    """expect_shown=True means it should pass the filter; False means dropped."""
    result, audit = _mon.filter_message(_msg(content), CONFIG)
    shown = result is not None
    ok = shown == expect_shown
    verb = "shown" if shown else f"dropped ({audit.get('reason')})"
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {verb}")
    return ok


CASES = [
    # (name, content, expect_shown)

    # Individual cards — must be DROPPED even though they hit a keyword.
    ("single: MSH166 (reported false-negative)",
     "Epic Fight MSH166 Marvel Super Heroes $45", False),
    ("single: borderless w/ collector number",
     "Spider-Man (0209) Borderless $120", False),
    ("single: set-code + jumpstart set name (bare word must NOT bypass)",
     "Lightning Bolt LTR-146 (Jumpstart) Retro Frame $8", False),

    # Sealed product — must PASS despite carrying a set-code token.
    ("sealed: prerelease pack w/ TDM-2025 (the fix)",
     "Tarkir Dragonstorm Prerelease Pack TDM-2025 $29.99", True),
    ("sealed: collector booster box w/ BLB-999",
     "Bloomburrow Collector Booster Box BLB-999 $299", True),
    ("sealed: pokemon elite trainer box w/ SSP-100",
     "Surging Sparks Elite Trainer Box SSP-100 $59", True),
    ("sealed: jumpstart booster box",
     "Jumpstart Booster Box $110", True),
]


def main():
    print("Discord singles-filter regression tests\n")
    results = [run(n, c, e) for (n, c, e) in CASES]
    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
