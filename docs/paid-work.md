# Optional production spending control

Production entry points can opt into a persistent admission ledger with
`PAID_WORK_ENFORCE=1`. The workflows take this from the repository variable
of the same name; its absent/default value is `0`. Activation requires a
reviewed `ops/paid-work.json` in the existing R2 bucket. No new credential,
paid plan, forecast format or consumer change is required.

The implementation is vendored from `oscar/cloud/recompute/paid_work.py`
(2026-10-03). Passage's prepared-run uploader uses the same protocol and
object in the shared forecast bucket. Keep the three implementations in sync.

The document has version `1`, a boolean `paused`, timezone-aware
`period_start`, exclusive `period_end`, and `reviewed_at`; each provider gate
has an explicit `allow_paid_work=true` decision from that review. Keep actual
billing amounts in the private operator review file: this bucket's ledger
is publicly readable. The local controller compares known `spend_eur_ttc`
against `pause_at_eur_ttc` and publishes only the decision. Unknown or reached
spend produces a closed decision; no financial amounts or credentials belong
in the public object. Its hashed work identities and runtime counters are
operational metadata, also publicly readable.
Missing/unreadable/malformed state, a review older than 72 hours, a future
review, expired period, operator pause or a reached provider gate stops work.
Reported provider costs are delayed; these reviewed values are not live
billing telemetry. An alert does not write the document automatically.

Limits are `max_seconds`, `max_starts`, `single_active`, and `channels`.
Each channel names `daily_starts`, `min_interval_seconds`, and
`max_run_seconds`. Usage is shared across channels. Each admission reserves
the entire worst-case duration using R2 `If-Match`, before building or
publishing. A lost response stops admission without refund; only a definite
412 conflict is retried (six attempts maximum). SDK retries are disabled. Control-document GET/stream transport failures and
transient HTTP responses receive at most three read attempts with one/two-second
backoff; response bodies close between attempts. Missing/denied objects and
malformed/oversized documents stop immediately. Exhausted reads still close
admission. Admission PUTs are never retried after an uncertain outcome.
Completed, failed, timed-out and crashed runs retain their charged allowance.
One active lease per channel lasts its reservation plus 60 seconds; optional
`single_active=true` also serializes different channels.

The provider-cycle identity blocks dispatcher/fallback/manual duplicate
production, including `--force`. Already-published cycles return without a
reservation. Provider discovery/waiting precedes admission and can still
make reads; the enforced wall-clock deadline includes that wait. The
reserved allowance conservatively covers the workflow duration: GLO12
currents 18,000 seconds (its existing 300-minute workflow includes up to
240 minutes of waiting), regional jobs 9,000 seconds (150 minutes), other
layers and land 14,400 seconds. A denied admission leaves pointers and
published objects alone. `--dry-run` uses local storage and bypasses paid
production admission.

An intentional pause prints `paused` and returns exit 0, so workflows do
not amplify it through failure retries. Regional attempt reports record
`outcome=paused`, `failure_category=spending_control`; a green workflow on
this path does **not** prove new data was published. No automatic recovery
or monthly counter reset occurs. Consumer reads continue normally.

## Operator pause and recovery

Use the existing bucket-scoped credentials; never print them. Read the
current document and ETag, change policy fields, preserve **all** `usage`,
then write with that ETag (`If-Match`) and read back. On a conflict, reread;
on an unknown outcome, inspect before trying again. Setting `paused=true`
blocks new admissions, but does not cancel an in-flight publication.

Before resuming: verify account-wide R2 costs, billing period, conversion,
tax treatment and outstanding work; update the provider gates and
`reviewed_at`, then set `paused=false` with CAS. Allowance exhaustion
requires a separately approved increase or an operator-reviewed new period.
Archive the old ledger before starting a new period. Never erase counters
to recover a retry. For a reviewed repair, remove only its hashed identity
from the channel's `seen`, retain charged counters, and wait for lease and
frequency limits. Dispatch one bounded job, then inspect its report/pointer.

The manual [`review-paid-work`](../.github/workflows/review-paid-work.yml)
workflow records a completed billing review using the existing R2 credentials.
It defaults to dry run and requires explicit confirmation of the account-wide
review, the exact previous `reviewed_at`, and a new timezone-aware timestamp
within fifteen minutes. It changes only `reviewed_at`; provider decisions must
already permit work. Operator pause, expired periods, limits, counters, charged
identities and active leases remain intact. One conditional write is followed
by exact authenticated read-back. A conflict or uncertain result stops without
another write; inspect the current document before any further operator action.
This tool does not obtain billing evidence or turn a closed decision into an
open one. Keep the actual billing receipt private.

For a reviewed failed GFS or ECMWF full-horizon cycle, the manual
[`recover-forecast`](../.github/workflows/recover-forecast.yml) workflow performs
that exact one-identity CAS and authenticated read-back before ordinary guarded
ingestion. It refuses published/superseded cycles, complete uncommitted runs,
active leases and exhausted limits. It never refunds charges or retries an
uncertain control PUT. This is an operator action, not an automatic retry cron.
Partial root tiles are reused only when all scientific header fields and the
entire encoded payload match; original compressed bytes and diagnostic times
remain intact. Changed data/layout refuses recovery, and published manifests
and regional immutability rules retain their existing protections.
Root data-object creates have at most three attempts: an authenticated read
must prove absence before the same create-only body can be retried. Confirmed
bytes are reused; different bytes or unknown read-back stop the upload. This
does not retry admission writes or grant another producer start.

Rollback: set the repository variable to `0` (or remove it), or revert the
guard integration commit. This reopens optional production and removes its
protection. Keep the ledger for recovery; existing data needs no rollback.
Keeping enforcement on with `paused=true` is the safer operational stop.

## Exposure outside the producer ledger

This controls participating producers, not account-wide R2 operations or
bytes. Public reads, existing storage, other writers, control-document
reads/PUTs, discovery reads and jobs already running continue to incur
operations. R2 bills rounded units; its first paid million Class A
operations is $4.50. Free egress does not mean free reads. A small monetary
allocation cannot be promised as a hard global ceiling by this mechanism.
GitHub zero paid-usage budgets must remain enabled; this ledger's runtime
allowance is a workload limit, not a paid Actions allowance.

[R2 pricing](https://developers.cloudflare.com/r2/pricing/) and
[conditional S3 writes](https://developers.cloudflare.com/r2/api/s3/extensions/).

## Physical storage admission

[Storage v1](storage-admission.md) is an independent, separately gated capacity
contract owned in forecast-tiles. Root, regional and land publishers reserve
their entire temporary upload peak with conditional coordination before data
writes when `CAPACITY_ENFORCE=1`. Its default remains `0` pending Passage/OSCAR
writer integration and reviewed account allocations. It never changes this
ledger's usage. Expired, finished and uncertain storage reservations remain
charged until an explicit paused/fenced reconciliation; runtime lease expiry
is not storage-release proof. Existing data and ordinary reads remain available.
