"""Renew a completed provider review without changing policy or charged usage."""

from copy import deepcopy

from ingest.paid_work import Guard, Paused, timestamp


def renew_review(guard: Guard, expected_reviewed_at: str, reviewed_at: str, *, apply=False):
    """One CAS only; a conflict or uncertain result requires operator inspection.

    The caller must first review actual account-wide provider billing. This
    helper records that review, never supplies a decision or changes a limit.
    """
    try:
        observed = timestamp(reviewed_at)
        timestamp(expected_reviewed_at)
    except (TypeError, ValueError, AttributeError) as exc:
        raise Paused("review timestamps require explicit timezones") from exc
    if not 0 <= guard.clock() - observed <= 15 * 60:
        raise Paused("completed billing review must be within the last 15 minutes")
    doc, etag = guard.read()
    if doc.get("reviewed_at") != expected_reviewed_at:
        raise Paused("review changed; inspect current control before applying")
    if observed <= timestamp(expected_reviewed_at):
        raise Paused("review must advance")
    # Monetary policy belongs in the private operator review. Only already-open
    # explicit provider decisions can be renewed by this deliberately narrow tool.
    gates = doc.get("gates")
    if (
        not isinstance(gates, list)
        or not gates
        or any(
            not isinstance(gate, dict) or gate.get("allow_paid_work") is not True for gate in gates
        )
    ):
        raise Paused("existing explicit provider decisions must already permit work")
    candidate = deepcopy(doc)
    candidate["reviewed_at"] = reviewed_at
    guard.validate(candidate)  # Still refuses pause, expired period or closed policy.
    if not apply:
        return False
    if not guard.write(candidate, etag):
        raise Paused("review CAS conflict; no write repeated")
    readback, _ = guard.read()
    if readback != candidate:
        raise Paused("review read-back differs; inspect before any further write")
    guard.validate(readback)
    return True
