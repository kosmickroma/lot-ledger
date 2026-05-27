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
    "monthly_1m": {
        "months": 1,
        "distances_mi": DISTANCES_MI,
        "description": "Ongoing production sweep (1-month window). Cron target.",
    },
}
