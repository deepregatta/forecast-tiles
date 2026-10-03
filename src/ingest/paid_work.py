# Vendored from oscar/cloud/recompute/paid_work.py, 2026-10-03.
"""Persistent admission control for optional producers; no data-path changes.

The operator provisions one reviewed JSON document in R2. Policy and usage live
in the same object, so an If-Match reservation cannot race a pause or another
producer. Reserve the entire worst-case duration before starting; never refund
it after failure, timeout or process loss. Missing/stale/uncertain state closes
admission. PAID_WORK_ENFORCE=1 activates this guard after production approval.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone


class Paused(RuntimeError):
    """Work stays queued; the published data remains readable."""


class RuntimeExpired(BaseException):
    """A deadline must escape provider/SDK `except Exception` retry loops."""


def enforced() -> bool:
    value = os.environ.get("PAID_WORK_ENFORCE", "0")
    if value not in {"0", "1"}:
        raise Paused("invalid PAID_WORK_ENFORCE (expected 0 or 1)")
    return value == "1"


def timestamp(value: str) -> float:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return result.timestamp()


def positive(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("limit must be numeric")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("limit must be positive and finite")
    return value


class Guard:
    def __init__(self, client, bucket: str, key="ops/paid-work.json", *, clock=time.time):
        self.client, self.bucket, self.key, self.clock = client, bucket, key, clock

    @classmethod
    def from_env(cls):
        try:
            import boto3
            from botocore.config import Config

            # No SDK retries of admission PUTs: an unknown outcome must stop work.
            client = boto3.client(
                "s3",
                endpoint_url=os.environ.get("R2_ENDPOINT") or os.environ["R2_ENDPOINT_URL"],
                aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                region_name="auto",
                config=Config(
                    connect_timeout=10,
                    read_timeout=20,
                    retries={"total_max_attempts": 1},
                ),
            )
            return cls(client, os.environ["R2_BUCKET"])
        except Exception as exc:
            raise Paused("control client unavailable") from exc

    def read(self):
        try:
            result = self.client.get_object(Bucket=self.bucket, Key=self.key)
            body = result["Body"].read(1_000_001)
            if len(body) > 1_000_000:
                raise ValueError("oversized control document")
            return json.loads(body), result["ETag"]
        except Exception as exc:
            raise Paused("control state unavailable") from exc

    def validate(self, doc):
        now = self.clock()
        try:
            if not isinstance(doc, dict) or type(doc["version"]) is not int:
                raise ValueError("invalid version")
            if doc["version"] != 1 or doc["paused"] is not False:
                raise Paused("operator pause")
            if not timestamp(doc["period_start"]) <= now < timestamp(doc["period_end"]):
                raise Paused("period requires review")
            observed = timestamp(doc["reviewed_at"])
            if not observed <= now < observed + 72 * 3600:
                raise Paused("billing review is stale")
            if not isinstance(doc["gates"], list) or not doc["gates"]:
                raise ValueError("no provider gate")
            for gate in doc["gates"]:
                # A public forecast bucket carries only a reviewed decision,
                # never the owner's actual monetary billing amounts.
                if "allow_paid_work" in gate:
                    if gate["allow_paid_work"] is not True:
                        raise Paused("provider review does not allow paid work")
                    continue
                spend = gate["spend_eur_ttc"]
                if isinstance(spend, bool) or not isinstance(spend, (int, float)):
                    raise ValueError("unknown spend")
                if not math.isfinite(spend) or spend < 0:
                    raise ValueError("unknown spend")
                if spend >= positive(gate["pause_at_eur_ttc"]):
                    raise Paused("provider spending limit")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise Paused("invalid control state") from exc

    def check(self):
        doc, _ = self.read()
        self.validate(doc)

    def write(self, doc, etag):
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self.key,
                Body=json.dumps(doc).encode(),
                ContentType="application/json",
                CacheControl="no-store",
                IfMatch=etag,
            )
        except Exception as exc:
            status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 412:
                return False
            raise Paused("reservation outcome unknown; review before retry") from exc
        return True

    def acquire(self, channel: str, work_id: str, seconds: int, *, grace=60):
        positive(seconds)
        token = uuid.uuid4().hex
        identity = hashlib.sha256(work_id.encode()).hexdigest()
        for _ in range(6):
            doc, etag = self.read()
            self.validate(doc)
            now = self.clock()
            day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            try:
                limits = doc["channels"][channel]
                if type(doc.get("single_active", False)) is not bool:
                    raise ValueError("single_active must be boolean")
                usage = doc.setdefault("usage", {"seconds": 0, "starts": 0, "channels": {}})
                state = usage["channels"].setdefault(channel, {"seen": {}, "days": {}, "last": 0})
                for value in (
                    usage["seconds"],
                    usage["starts"],
                    state["last"],
                    *state["days"].values(),
                ):
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        or value < 0
                    ):
                        raise ValueError("invalid usage counter")
                if state["last"] > now:
                    raise ValueError("usage timestamp is in the future")
                for other in usage["channels"].values():
                    active = other.get("active")
                    if active is not None:
                        positive(active["until"])
                        if not isinstance(active["token"], str) or not active["token"]:
                            raise ValueError("invalid active lease")
                # Validate every limit even when its particular branch is not reached.
                monthly_seconds = positive(doc["max_seconds"])
                monthly_starts = positive(doc["max_starts"])
                daily_starts = positive(limits["daily_starts"])
                interval = positive(limits["min_interval_seconds"])
                if seconds > positive(limits["max_run_seconds"]):
                    raise Paused("run exceeds its reserved duration")
                if any(
                    s.get("active", {}).get("until", 0) > now for s in usage["channels"].values()
                ):
                    # Limit concurrent jobs across every participating producer.
                    if doc.get("single_active", False):
                        raise Paused("another producer is running")
                if state.get("active", {}).get("until", 0) > now:
                    raise Paused("this producer is running")
                if identity in state["seen"]:
                    raise Paused("duplicate work requires explicit recovery")
                if state["last"] and now - state["last"] < interval:
                    raise Paused("recompute frequency limit")
                if state["days"].get(day, 0) >= daily_starts:
                    raise Paused("daily starts limit")
                if (
                    usage["starts"] >= monthly_starts
                    or usage["seconds"] + seconds > monthly_seconds
                ):
                    raise Paused("cumulative runtime/start limit")
                usage["starts"] += 1
                usage["seconds"] += seconds
                state["last"] = now
                state["days"][day] = state["days"].get(day, 0) + 1
                state["seen"][identity] = token
                state["active"] = {"token": token, "until": now + seconds + grace}
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise Paused("invalid usage or channel policy") from exc
            if self.write(doc, etag):
                return token
        raise Paused("reservation contention")

    def finish(self, channel: str, token: str):
        # Release only this lease, preserving a concurrent operator pause and all
        # charged duration. If this fails, expiry frees the lease; no refund.
        for _ in range(6):
            doc, etag = self.read()
            state = doc.get("usage", {}).get("channels", {}).get(channel, {})
            if state.get("active", {}).get("token") != token:
                return
            del state["active"]
            if self.write(doc, etag):
                return
        raise Paused("lease release contention")


@contextmanager
def deadline(seconds: int):
    """Linux producer deadline; a timeout never starts another automatic job."""

    def expired(*_):
        raise RuntimeExpired("reserved runtime expired")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
