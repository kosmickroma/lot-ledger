"""Filter profile definitions for the production scraper.

Pure data, no logic. Imported by run.py.

Per PRODUCTION_SCRAPER_SPEC v1.2 §5.
"""

DISTANCES_MI: list[float] = [0.25, 0.5, 1.0, 2.0, 5.0]


PROFILES: dict[str, dict] = {
    "seed_5y": {
        "months": 60,
        "distances_mi": DISTANCES_MI,
        "description": "One-time deep-history seed pass (60-month window).",
    },
    "refresh_6m": {
        # The catch-up sweep for the June->September 2026 gap (KK + 5.1, 9/06).
        # Propelio caps every call at 100 comps, so the window and the radius
        # trade off: a wide radius only works with a short window, and a long
        # window only works with a tight radius. Five pulls per address:
        #   1 mo @ 2 mi  - the freshest sales, unsaturated almost everywhere
        #   3 mo @ 2 mi  - KK's pick; saturates only in the densest core
        #   6 mo @ 1 mi  - the workhorse for dense areas
        #   6 mo @ 0.5mi - block level, for the densest cores
        #   6 mo @ 5 mi  - the rural sweep
        "months": 6,
        "distances_mi": [0.5, 1.0, 2.0, 5.0],
        "pulls": [(1, 2.0), (3, 2.0), (6, 1.0), (6, 0.5), (6, 5.0)],
        "description": "Catch-up sweep after a gap: 1mo@2mi, 3mo@2mi, 6mo@1mi, 6mo@0.5mi, 6mo@5mi.",
    },
    "refresh_fast": {
        # KK's three-pull version of the same sweep: ~40% of the calls, loses
        # some 3-6 month sales in the densest core to the 100 cap.
        "months": 6,
        "distances_mi": [2.0, 5.0],
        "pulls": [(1, 2.0), (3, 2.0), (6, 5.0)],
        "description": "Fast catch-up: 1mo@2mi, 3mo@2mi, 6mo@5mi.",
    },
    "monthly_1m": {
        "months": 1,
        "distances_mi": DISTANCES_MI,
        "description": "Ongoing production sweep (1-month window). Cron target.",
    },
}
