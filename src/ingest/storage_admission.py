"""Canonical storage admission v1, vendorable using only the Python stdlib.

Whole-upload debits are permanent until an explicit paused reconciliation.
This deliberately trades availability for safety after uncertain writes/crashes.
No billing values, bucket identities or private object names belong in the ledger.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from dataclasses import dataclass

VERSION = 1
KEY = "ops/capacity-v1.json"
CONTROL_BYTES = 262_144
MUTABLE_UPLOAD_BYTES = 1_048_576
ATTEMPTS = 6


class CapacityDenied(RuntimeError):
    pass


def enforced() -> bool:
    value = os.environ.get("CAPACITY_ENFORCE", "0")
    if value not in {"0", "1"}:
        raise CapacityDenied("invalid CAPACITY_ENFORCE")
    return value == "1"


def number(value, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError("invalid byte count")
    return value


def encode(doc):
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > CONTROL_BYTES:
        raise CapacityDenied("capacity document full; reconcile before more admissions")
    return raw


def validate(doc):
    try:
        if set(doc) != {
            "version",
            "paused",
            "rollout_complete",
            "epoch",
            "limit_bytes",
            "headroom_bytes",
            "owners",
            "baseline_bytes",
            "reservations",
            "seen_work",
        }:
            raise ValueError("unknown policy fields")
        if doc["version"] != VERSION or type(doc["version"]) is not int:
            raise ValueError("version")
        if type(doc["rollout_complete"]) is not bool:
            raise ValueError("rollout")
        if type(doc["paused"]) is not bool:
            raise ValueError("pause")
        for key in ("limit_bytes", "headroom_bytes"):
            number(doc[key], positive=True)
        number(doc["epoch"])
        if not doc["owners"] or set(doc["baseline_bytes"]) != set(doc["owners"]):
            raise ValueError("ownership")
        prefixes = set()
        for name, owner in doc["owners"].items():
            if set(owner) != {"mode", "prefixes", "ceiling_bytes"} or not isinstance(
                owner["prefixes"], list
            ):
                raise ValueError("unknown owner fields")
            if not isinstance(name, str) or not name or not owner["prefixes"]:
                raise ValueError("owner")
            number(doc["baseline_bytes"][name])
            if owner["mode"] not in {"managed", "unmanaged"}:
                raise ValueError("mode")
            number(owner["ceiling_bytes"], positive=True)
            if doc["baseline_bytes"][name] > owner["ceiling_bytes"]:
                raise ValueError("baseline exceeds allocation")
            for prefix in owner["prefixes"]:
                if not isinstance(prefix, str) or not prefix or prefix in prefixes:
                    raise ValueError("ambiguous prefix")
                prefixes.add(prefix)
        seen = doc["seen_work"]
        if not isinstance(seen, list) or len(set(seen)) != len(seen):
            raise ValueError("seen work")
        if any(
            not isinstance(s, str) or len(s) != 64 or any(c not in "0123456789abcdef" for c in s)
            for s in seen
        ):
            raise ValueError("work identity")
        if not isinstance(doc["reservations"], dict):
            raise ValueError("reservations")
        for token, r in doc["reservations"].items():
            if set(r) != {"writer", "work", "bytes", "expires_at", "state"}:
                raise ValueError("unknown reservation fields")
            if not isinstance(token, str) or not token or r["writer"] not in doc["owners"]:
                raise ValueError("reservation")
            if doc["owners"][r["writer"]]["mode"] != "managed":
                raise ValueError("unmanaged reservation")
            number(r["bytes"], positive=True)
            if r["work"] not in seen or r["state"] not in {"active", "finished"}:
                raise ValueError("reservation state")
            if type(r["expires_at"]) not in {int, float} or not math.isfinite(r["expires_at"]):
                raise ValueError("expiry")
        encode(doc)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise CapacityDenied("invalid capacity state; reconcile before admission") from exc


def owner_for(key, owners):
    matches = [
        (len(p), name) for name, o in owners.items() for p in o["prefixes"] if key.startswith(p)
    ]
    if not matches:
        raise CapacityDenied("unclassified object growth; reconcile ownership")
    return max(matches)[1]


def inventory(store, owners):
    """Strict physical inventory: all completed objects and multipart parts.

    Longest explicit prefix owns an object; unknown keys are never zero bytes.
    Multipart parts require a store implementation, even if it reports zero.
    """
    totals = {name: 0 for name in owners}
    try:
        seen = set()
        for obj in store.list_objects(""):
            key = obj["key"]
            size = number(obj["bytes"])
            if not isinstance(key, str) or key in seen:
                raise ValueError("invalid or duplicate object")
            seen.add(key)
            if key == KEY:
                if size > CONTROL_BYTES:
                    raise ValueError("oversized capacity object")
                continue  # fixed CONTROL_BYTES allocation, independent of document growth
            totals[owner_for(key, owners)] += size
        # Multipart uploads are unconverted work and need a fixed dedicated allocation.
        parts = number(store.multipart_bytes())
        if parts:
            if "multipart" not in owners or owners["multipart"]["mode"] != "unmanaged":
                raise CapacityDenied("multipart occupancy has no conservative allocation")
            totals["multipart"] += parts
    except CapacityDenied:
        raise
    except Exception as exc:
        raise CapacityDenied("physical inventory unavailable; refusing admission") from exc
    return totals


def envelope(doc, measured):
    """Per-owner ceilings coordinate bucket allocation; unconverted owners reserve theirs in full."""
    debits = {name: 0 for name in doc["owners"]}
    for r in doc["reservations"].values():
        debits[r["writer"]] += r["bytes"]  # expired and finished are still charged
    charged = {}
    for name, o in doc["owners"].items():
        if o["mode"] == "unmanaged":
            charged[name] = o["ceiling_bytes"]
            if measured[name] > charged[name]:
                raise CapacityDenied("unconverted writer exceeded its allocation")
        else:
            charged[name] = doc["baseline_bytes"][name] + debits[name]
            if measured[name] > charged[name]:
                raise CapacityDenied("unexpected managed writer growth; reconcile before admission")
            if charged[name] > o["ceiling_bytes"]:
                raise CapacityDenied("writer capacity allocation reached")
    peak = sum(charged.values()) + doc["headroom_bytes"] + CONTROL_BYTES
    if peak > doc["limit_bytes"]:
        raise CapacityDenied("bucket capacity allocation reached")
    return peak


@dataclass(frozen=True)
class Reservation:
    token: str
    writer: str
    bytes: int
    epoch: int


class Admission:
    def __init__(self, store, *, conflicts=(), clock=time.time):
        self.store, self.conflicts, self.clock = store, conflicts, clock

    def read(self):
        try:
            raw, etag = self.store.get_with_etag(KEY)
            if raw is None or not etag or len(raw) > CONTROL_BYTES:
                raise ValueError("missing/oversized ledger")
            doc = json.loads(raw)
        except Exception as exc:
            raise CapacityDenied("capacity state unavailable; refusing admission") from exc
        validate(doc)
        return doc, etag

    def write(self, doc, etag):
        try:
            self.store.put(
                KEY,
                encode(doc),
                content_type="application/json",
                cache_control="no-store",
                if_match=etag,
            )
        except self.conflicts:
            return False
        except Exception as exc:
            raise CapacityDenied(
                "capacity write outcome unknown; debit retained, reconcile before retry"
            ) from exc
        return True

    def acquire(self, writer, work_id, upload_bytes, *, seconds=14_400):
        try:
            number(upload_bytes, positive=True)
            number(seconds, positive=True)
        except ValueError as exc:
            raise CapacityDenied("invalid reservation bound") from exc
        work = hashlib.sha256((writer + ":" + work_id).encode()).hexdigest()
        token = uuid.uuid4().hex
        for _ in range(ATTEMPTS):
            doc, etag = self.read()
            if doc["paused"] or not doc["rollout_complete"]:
                raise CapacityDenied("capacity admissions paused")
            if writer not in doc["owners"] or doc["owners"][writer]["mode"] != "managed":
                raise CapacityDenied("writer rollout not admitted")
            if work in doc["seen_work"]:
                raise CapacityDenied(
                    "work already reserved; reconcile uncertain or duplicate publication"
                )
            measured = inventory(self.store, doc["owners"])
            envelope(doc, measured)
            doc["reservations"][token] = {
                "writer": writer,
                "work": work,
                "bytes": upload_bytes,
                "expires_at": self.clock() + seconds,
                "state": "active",
            }
            doc["seen_work"].append(work)
            envelope(doc, measured)
            if self.write(doc, etag):
                # The fresh read proves this token exists, and sees concurrent operator pauses.
                result = Reservation(token, writer, upload_bytes, doc["epoch"])
                self.check(result)
                return result
        raise CapacityDenied("capacity coordination conflicts exhausted")

    def check(self, reservation):
        doc, _ = self.read()
        r = doc["reservations"].get(reservation.token)
        if (
            doc["paused"]
            or not doc["rollout_complete"]
            or doc["epoch"] != reservation.epoch
            or not r
            or r["state"] != "active"
            or r["writer"] != reservation.writer
            or r["bytes"] != reservation.bytes
            or self.clock() >= r["expires_at"]
        ):
            raise CapacityDenied("capacity reservation stopped or expired; bytes remain charged")
        return doc

    def finish(self, reservation):
        # Finishing is diagnostic only; never credits bytes or changes paid-work usage.
        for _ in range(ATTEMPTS):
            doc, etag = self.read()
            r = doc["reservations"].get(reservation.token)
            if not r or doc["epoch"] != reservation.epoch:
                raise CapacityDenied("reservation changed before finish")
            r["state"] = "finished"
            if self.write(doc, etag):
                return
        raise CapacityDenied("finish conflict; reservation remains charged")

    def reconcile(self, *, stopped_tokens=(), evidence):
        """Operator-only after pause, drain AND fence of every unfinished writer.

        evidence identifies a private external stop/fence receipt, never a timeout.
        No automatic call path; never deletes data. Retain duplicate-work history.
        """
        doc, etag = self.read()
        if not doc["paused"] or not isinstance(evidence, str) or not evidence.strip():
            raise CapacityDenied("reconciliation requires pause and verified stop evidence")
        active = {t for t, r in doc["reservations"].items() if r["state"] != "finished"}
        if active != set(stopped_tokens):
            raise CapacityDenied("every unfinished writer needs external stop/fence proof")
        measured = inventory(self.store, doc["owners"])
        # Unknown keys or allocation excess remain closed; deletes never inferred.
        candidate = {
            **doc,
            "baseline_bytes": measured,
            "reservations": {},
            "epoch": doc["epoch"] + 1,
        }
        validate(candidate)
        envelope(candidate, measured)
        if not self.write(candidate, etag):
            raise CapacityDenied("reconciliation conflict; reread without releasing debits")
        reread, _ = self.read()
        if reread["epoch"] != candidate["epoch"] or not reread["paused"]:
            raise CapacityDenied("reconciliation outcome changed; keep admissions paused")
        return reread


class ReservedStore:
    """Publication-scoped adapter bounds every upload attempt before calling the store."""

    def __init__(self, admission, reservation, *, prefix, mutable_keys):
        self.admission, self.reservation = admission, reservation
        self.store = admission.store
        self.prefix, self.mutable_keys = prefix, set(mutable_keys)
        self.attempted_bytes = 0

    def __getattr__(self, name):
        return getattr(self.store, name)

    def put(self, key, data, **kwargs):
        if not key.startswith(self.prefix) and key not in self.mutable_keys:
            raise CapacityDenied("upload outside reserved publication scope")
        doc = self.admission.check(self.reservation)
        owner = owner_for(key, doc["owners"])
        if key not in self.mutable_keys and owner != self.reservation.writer:
            raise CapacityDenied("upload ownership differs from reservation")
        if key in self.mutable_keys:
            # Recheck foreign/unconverted growth and read availability before promotion.
            measured = inventory(self.store, doc["owners"])
            envelope(doc, measured)
            if owner != self.reservation.writer:
                allocation = doc["owners"][owner]
                if (
                    allocation["mode"] != "unmanaged"
                    or measured[owner] + len(data) > allocation["ceiling_bytes"]
                ):
                    raise CapacityDenied("mutable upload exceeds its conservative allocation")
        if self.attempted_bytes + len(data) > self.reservation.bytes:
            raise CapacityDenied("upload exceeds reserved peak")
        self.attempted_bytes += len(data)  # uncertainty/conflicts never refund an attempt
        return self.store.put(key, data, **kwargs)

    def delete(self, key):
        doc = self.admission.check(self.reservation)
        if owner_for(key, doc["owners"]) != self.reservation.writer:
            raise CapacityDenied("retention outside writer ownership")
        # Existing reference-aware retention owns the keys; no storage credit here.
        return self.store.delete(key)


def account_cost_model(
    bucket_bytes, *, class_a=None, class_b=None, usd_to_eur=None, tax_multiplier=None
):
    """Reconcile Standard-storage allocations/snapshots, never dashboard-counter sums.

    Call separately for the measured snapshot and proposed daily-peak envelope.
    Estimates a constant whole-period plateau, NOT a measured GB-month bill.
    Account-wide free allowances apply once. Unknown inputs remain unknown.
    """
    known = 0
    unknown = []
    for bucket, size in bucket_bytes.items():
        if size is None:
            unknown.append(bucket)
        else:
            known += number(size)
    if not bucket_bytes:
        unknown.append("inventory")
    storage_usd = None if unknown else math.ceil(max(0, known / 1_000_000_000 - 10)) * 0.015
    operations_usd = None
    if class_a is not None and class_b is not None:
        number(class_a)
        number(class_b)
        operations_usd = (
            math.ceil(max(0, class_a - 1_000_000) / 1_000_000) * 4.50
            + math.ceil(max(0, class_b - 10_000_000) / 1_000_000) * 0.36
        )
    total_usd = (
        None if storage_usd is None or operations_usd is None else storage_usd + operations_usd
    )
    total_eur = None
    if usd_to_eur is not None and tax_multiplier is not None:
        if any(
            type(x) not in {int, float} or not math.isfinite(x) or x <= 0
            for x in (usd_to_eur, tax_multiplier)
        ):
            raise ValueError("explicit conversion and tax assumptions required")
        total_eur = None if total_usd is None else total_usd * usd_to_eur * tax_multiplier
    return {
        "known_snapshot_bytes": known,
        "unknown_buckets": unknown,
        "basis": "Standard storage; constant daily-peak plateau, not measured GB-month billing",
        "storage_usd": storage_usd,
        "operations_usd": operations_usd,
        "total_usd": total_usd,
        "total_eur_ttc": total_eur,
    }
