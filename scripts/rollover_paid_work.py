#!/usr/bin/env python3
"""Apply the owner's October 20-November 20 period approval without resetting usage."""

import argparse

from ingest.paid_work import Guard, enforced
from ingest.paid_work_review import rollover_october20


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--expected-reviewed-at", required=True)
    p.add_argument("--reviewed-at", required=True)
    p.add_argument("--confirm-reviewed-provider-billing", action="store_true")
    p.add_argument("--confirm-owner-approved-october20", action="store_true")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if not enforced():
        p.error("period rollover requires existing spending enforcement")
    if not args.confirm_owner_approved_october20:
        p.error("the specific October 20-November 20 owner authorization is required")
    if not args.confirm_reviewed_provider_billing:
        p.error("first review account-wide costs, period, conversion, tax and outstanding work")
    applied = rollover_october20(
        Guard.from_env(), args.expected_reviewed_at, args.reviewed_at, apply=args.apply
    )
    print(
        "Approved period and review updated with one CAS and exact read-back; usage preserved"
        if applied
        else "Approved period dry run passed; no write made"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
