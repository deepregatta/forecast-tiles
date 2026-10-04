"""Prepare a single cache-rule update and rollback offline; never call a provider.

Input is a privately captured rule or Rulesets API response. Keep the input and
generated files outside the public repository. Only the browser TTL changes.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from decimal import ROUND_CEILING, Decimal
import hashlib
import json
from pathlib import Path

FORECAST_EXPRESSION = '(http.host eq "forecast.deepregatta.com")'
READ_METHODS_RULE = {
    "action": "block",
    "description": "Forecast public endpoint accepts reads only",
    "enabled": True,
    "expression": (
        '(http.host eq "forecast.deepregatta.com" '
        'and not http.request.method in {"GET" "HEAD" "OPTIONS"})'
    ),
}
WRITE_FIELDS = {"action", "action_parameters", "description", "enabled", "expression", "ref"}
READ_ONLY_FIELDS = {"id", "last_updated", "version"}


def rule_body(rule: dict) -> dict:
    """Refuse unknown top-level fields rather than silently drop live settings."""
    unknown = set(rule) - WRITE_FIELDS - READ_ONLY_FIELDS
    if unknown:
        raise ValueError("Unreviewed rule fields: " + ", ".join(sorted(unknown)))
    return deepcopy({key: value for key, value in rule.items() if key in WRITE_FIELDS})


def prepare(document: dict, rule_id: str) -> dict:
    source = document.get("result", document)
    if "rules" in source:
        matches = [rule for rule in source["rules"] if rule.get("id") == rule_id]
    else:
        matches = [source] if source.get("id") == rule_id else []
    if len(matches) != 1:
        raise ValueError("Expected exactly one rule with the reviewed ID")
    rule = matches[0]
    if (
        rule.get("expression") != FORECAST_EXPRESSION
        or rule.get("enabled") is not True
        or rule.get("action") != "set_cache_settings"
    ):
        raise ValueError("Forecast rule scope/action/activation drifted; review it first")
    params = rule.get("action_parameters", {})
    if params.get("cache") is not True or params.get("edge_ttl", {}).get("mode") != (
        "bypass_by_default"
    ):
        raise ValueError("Origin-based edge policy drifted; review it first")
    if "browser_ttl" in params and params["browser_ttl"] != {"mode": "respect_origin"}:
        raise ValueError("Existing browser TTL differs from the reviewed baseline")
    rollback = rule_body(rule)
    update = deepcopy(rollback)
    update["action_parameters"]["browser_ttl"] = {"mode": "respect_origin"}
    digest = hashlib.sha256(
        json.dumps(rule, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "source_rule_sha256": digest,
        "rule_id": rule_id,
        "source_version": rule.get("version"),
        "changed": update != rollback,
        "update": update,
        "rollback": rollback,
    }


def class_b_exposure(
    reads: int,
    *,
    other_cost_usd: Decimal = Decimal("0"),
    eur_per_usd: Decimal = Decimal("1"),
    tax_multiplier: Decimal = Decimal("1.2"),
) -> dict:
    """Standard account-wide scenario, not a bill or an enforced spending cap.

    All buckets share the 10M free allowance. Round excess to whole millions.
    Caller must separately include storage, Class A and other metered costs.
    """
    if isinstance(reads, bool) or not isinstance(reads, int) or reads < 0:
        raise ValueError("reads must be a nonnegative integer")
    if other_cost_usd < 0 or eur_per_usd <= 0 or tax_multiplier < 1:
        raise ValueError("invalid scenario assumptions")
    units = (Decimal(max(0, reads - 10_000_000)) / 1_000_000).to_integral_value(
        rounding=ROUND_CEILING
    )
    usd = units * Decimal("0.36")
    return {
        "account_class_b_reads": reads,
        "billable_millions": int(units),
        "class_b_usd": str(usd),
        "scenario_total_eur": str((usd + other_cost_usd) * eur_per_usd * tax_multiplier),
        "universal_cap": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--rule-id", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    output = args.output_dir.resolve()
    if output == repo or repo in output.parents:
        parser.error("write private rule snapshots and drafts outside the public repository")
    plan = prepare(json.loads(args.snapshot.read_text()), args.rule_id)
    output.mkdir(parents=True, exist_ok=True)
    for name in ("update", "rollback"):
        (output / f"cache-rule-{name}.json").write_text(json.dumps(plan[name], indent=2) + "\n")
    (output / "cache-rule-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    (output / "read-methods-rule-draft.json").write_text(
        json.dumps(READ_METHODS_RULE, indent=2) + "\n"
    )
    print("Offline drafts prepared; no provider change. Recheck source digest before approval.")


if __name__ == "__main__":
    main()
