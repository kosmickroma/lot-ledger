# scripts/marathon_campaign/smoke_phase5_pass_configs.py
#
# Role: Phase 5 CPU-only smoke test for density-based pass configuration selection.
#
# Connects to:
#   scripts/marathon_campaign/pass_configs.py - pass presets and selector helper

from __future__ import annotations

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.marathon_campaign.pass_configs import (  # noqa: E402
    PASSES_RURAL,
    PASSES_URBAN_SUBURBAN,
    passes_for_density_class,
)


def _ranges(passes: list[dict]) -> list[float]:
    return [float(item["range_mi"]) for item in passes]


def _print_check(name: str, ok: bool) -> bool:
    print(f"{name}: {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> None:
    all_ok = True

    urban = passes_for_density_class("urban")
    all_ok &= _print_check("urban_count_6", len(urban) == 6)
    all_ok &= _print_check("urban_has_0_25", 0.25 in _ranges(urban))
    all_ok &= _print_check("urban_has_10_0", 10.0 in _ranges(urban))

    suburban = passes_for_density_class("suburban")
    all_ok &= _print_check("suburban_count_6", len(suburban) == 6)
    all_ok &= _print_check("suburban_matches_urban", suburban == PASSES_URBAN_SUBURBAN)

    rural = passes_for_density_class("rural")
    rural_ranges = _ranges(rural)
    all_ok &= _print_check("rural_count_5", len(rural) == 5)
    all_ok &= _print_check("rural_no_0_25", 0.25 not in rural_ranges)
    all_ok &= _print_check("rural_has_0_5", 0.5 in rural_ranges)
    all_ok &= _print_check("rural_has_10_0", 10.0 in rural_ranges)

    unknown = passes_for_density_class("UNKNOWN")
    blank = passes_for_density_class("")
    none_val = passes_for_density_class(None)
    all_ok &= _print_check("unknown_defaults_6", len(unknown) == 6)
    all_ok &= _print_check("blank_defaults_6", len(blank) == 6)
    all_ok &= _print_check("none_defaults_6", len(none_val) == 6)

    copied = passes_for_density_class("urban")
    copied.append({"months": 24, "range_mi": 99.0, "label": "mutated"})
    all_ok &= _print_check("returned_list_is_copy", len(PASSES_URBAN_SUBURBAN) == 6 and len(copied) == 7)

    copied_rural = passes_for_density_class("rural")
    copied_rural.pop()
    all_ok &= _print_check("returned_rural_list_is_copy", len(PASSES_RURAL) == 5 and len(copied_rural) == 4)

    if not all_ok:
        raise SystemExit(1)

    print("smoke_phase5_pass_configs: PASS")


if __name__ == "__main__":
    main()
