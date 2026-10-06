#!/usr/bin/env python3
"""Record a fresh account-wide billing review; preserve all existing policy and usage."""

import argparse

from ingest.paid_work import Guard, enforced
from ingest.paid_work_review import renew_review


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--expected-reviewed-at", required=True)
    p.add_argument("--reviewed-at", required=True)
    p.add_argument("--confirm-reviewed-provider-billing", action="store_true")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if not enforced():
        p.error("review renewal requires existing spending enforcement")
    if not args.confirm_reviewed_provider_billing:
        p.error("first review account-wide costs, period, conversion, tax and outstanding work")
    applied = renew_review(
        Guard.from_env(), args.expected_reviewed_at, args.reviewed_at, apply=args.apply
    )
    print(
        "Review renewed with conditional write and exact read-back; policy and usage preserved"
        if applied
        else "Review dry run passed; no write made"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
