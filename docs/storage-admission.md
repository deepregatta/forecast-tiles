# Shared storage admission v1

F01 owns the reusable protocol in `src/ingest/storage_admission.py`, its
[JSON Schema](../contracts/storage-admission-v1.schema.json),
[scratch example](../contracts/storage-admission-v1.example.json) and
[acceptance tests](../tests/test_storage_admission.py). The implementation uses
only Python's standard library. No consumer imports another checkout at runtime.

Status on 2026-10-04: root, regional and land publication boundaries implement
v1 locally. **Production enforcement is not activated.** Workflows expose
`CAPACITY_ENFORCE`, default `0`; setting it to `1` requires the writer inventory,
allocation and rollout gates below. This change does not deploy the dispatcher,
open full regional cadence, change IAM, provision a ledger, delete data, or
change the existing `ops/paid-work.json` usage. Dated account inventory and
financial assumptions remain in private operator evidence, outside this repo.

[Coordinated rollout](storage-rollout.md) covers D01 release ordering, read-only
paused policy preparation, preservation of existing debits and unresolved GCS,
native-only and legacy-writer gates. Preparation does not authorize activation.

## Two independent controls

`MAX_BUCKET_BYTES` remains the retained forecast tile guard. Its fallback now
matches the rechecked 14,000,000,000-byte setting; it is neither the universal
physical bucket cap nor a monetary cap. Missing/inconsistent retained forecast
manifests still fail closed. The regional calculation still checks its physical
inventory, registered per-run caps and configured simultaneous-upload envelope.

Storage v1 adds a separately reviewed physical allocation. `PAID_WORK_ENFORCE`
still controls starts, runtime, provider-cycle duplication and reviewed spending.
Its counters are never reset or modified by storage v1. Admission denial raises
`StorageGuardError` before data uploads and leaves existing objects/pointers
readable; a failed workflow is not a publication receipt. A pause during upload
may leave an incomplete immutable prefix; it is counted until reconciled.
`--dry-run` bypasses production policy and uses scratch storage. Tests can inject
an `Admission` to exercise the identical protocol on a scratch store.

## Allocation and reservation

One document, `ops/capacity-v1.json`, owns policy and reservations for a bucket.
Required fields are `version=1`, `paused`, `rollout_complete`, `epoch`, positive
`limit_bytes` and `headroom_bytes`, `owners`, `baseline_bytes`, `reservations`,
and `seen_work`. Unknown fields, malformed counters and unsupported versions
close admission. The 100 MB example is deliberately synthetic, paused and
incomplete; it is not a production policy. Initialize with a full physical
inventory, not its zero baselines.

Each owner declares distinct prefixes and a byte ceiling. Longest prefix wins,
allowing regional prefixes inside the root `forecast-runs/` namespace. Managed
owners charge their reconciled baseline plus **every** reservation. Unconverted
owners reserve their entire ceiling, including upload overlap, until their
writers migrate or have a demonstrated external bound. Their unavailable
inventory never counts as zero. Unknown keys, unexpected managed growth and
unconverted occupancy above its allocation stop admission. All complete runs,
prepared revisions, land indexes, incomplete/orphaned objects, status/control
objects and multipart parts are counted. Multipart inventory unavailable also
closes admission; nonzero parts require a dedicated unmanaged allocation.

The admission equation is:

```
managed baselines + all managed upload debits
  + all unconverted owner ceilings
  + headroom + 262144 control-document bytes <= bucket limit
```

Every managed owner must also remain within its own ceiling. Equality is
admitted. The control object's maximum size is always reserved, preventing its
own growth from escaping accounting. The entire compressed upload and manifest
are reserved before the first data PUT. Forecast and land also reserve 1 MiB
for bounded pointer/status attempts. Each attempted PUT charges the adapter's
local allowance before sending; uncertain attempts/conflicts receive no refund.
A larger pointer or additional attempts beyond that allowance stop publication.
Retention earns no immediate credit. Existing data plus a new upload are
charged simultaneously; anticipated deletions cannot fund an upload.

Reservations use `If-Match` against the exact ledger ETag. Six definite conflicts
may reread and retry. A timeout, 5xx or lost reservation response stops the
caller without retrying or uploading, even when the reservation may have landed.
The fresh read after a successful PUT must prove the active token. The R2 client
disables SDK retries so an uncertain conditional write cannot silently repeat.
Each upload checks the token, pause and epoch. Before a pointer/status write it
also rechecks physical inventory, catching unexpected foreign growth during
upload. Managed callers must use the scoped adapter for every write.

A work identity is SHA-256 of writer plus publication identity. It remains in
`seen_work` across reconciliation, so manual, fallback and force retries cannot
reacquire a possibly occupied prefix. Reservations expire as *permission to
write*, never as byte charges. Finished reservations also retain their complete
charge. A full control document closes admission; it does not silently truncate
history or reset counters.

## Publication and uncertainty

Root and regional callers both use `publish_run`; land uses `publish_index`.
Every scheduled/manual/fallback/force path reaches these boundaries. Regionals
retain `latest-regional.json` separation and their existing canary/runtime gates.
Immutable tile/manifest PUTs are conditional creates. Root/land identical bytes
may be reused; regional duplicate prefixes keep their strict refusal. Different bytes in a completed or interrupted prefix are refused.
Root force can reuse identical content with its original manifest timestamps;
it cannot replace an immutable run. Changed content requires a new run/index.

Forecast pointer CAS already merges only its own layer and settles unknown
outcomes by rereading. Land now follows the same discipline for its own domain,
refuses stale versions, and does not prune after unresolved pointer outcomes.
Land retention protects every domain's current/previous references, incomplete
indexes and newer concurrent uploads. A publication exception leaves its full
capacity debit in place. A confirmed pointer followed by an uncertain finish
is reported as an incomplete operational outcome; it does not refund capacity
or undo readable data.

## Reconciliation, rollout and rollback

There is intentionally no automatic release or reconciliation scheduler.
Permanent worst-case debits can pause production well before the physical
bucket fills. This is an availability tradeoff requiring an operator
reconciliation plan before activation.

1. Inventory every bucket and writer. Sum all bucket allocations and reserve an
   account margin; bucket credentials need no runtime cross-bucket access.
   Include race publishers/recompute, ML mirror/snapshot/backup, private upload
   APIs, preview bindings, prepared uploaders, polars/manual uploads, forecast
   schedules/manual/force/fallback, land, repair scripts, control ledgers and
   incomplete multipart uploads. Unknown ownership stays closed.
2. Ship v1 code to forecast, P01 and O01 with enforcement off. Cover every writer
   using reservation adapters or verified conservative external allocations;
   free plans remain unchanged. Run the consumer acceptance cases below.
   An unbounded unconverted writer cannot satisfy `rollout_complete`.
3. After separate production approval, pause/drain all participating producers,
   including manual and private-upload paths. Inventory through the uncached
   authenticated API; provision reviewed ledgers with `If-None-Match: *`.
   Set matching nonzero owner baselines and explicit ceilings, initially paused
   and `rollout_complete=false`. Preserve/archive any existing ledger, ETag and
   paid-work usage; never overwrite or initialize missing usage as zero.
4. Enable each participating enforcement flag while admissions remain paused.
   Read back flags and deployed versions/adapters. Record the rollout receipt,
   then CAS `rollout_complete=true` and `paused=false` only after every writer
   has its budget. Allow ordinary scheduled jobs; do not provoke paid tests.
   Read back reservation-before-upload, immutable data and confirmed pointers.
5. To reconcile, CAS pause, stop/drain **all** writers and fence every unfinished
   token. Expiry alone is insufficient: a previously dispatched PUT may still
   land. Record external process/credential stop and in-flight-drain proof in
   private evidence. Archive the exact ledger/ETag. Call `Admission.reconcile`
   with the complete unfinished-token set and evidence reference. It performs a
   fresh physical inventory and conditional epoch increment, replaces baselines
   with actual occupied bytes, clears only reconciled debits and preserves
   `seen_work`. No object or paid-work usage is deleted. Read back while paused
   before reviewing/resuming. Conflicts or uncertain writes require reread;
   they never authorize a guessed release. Never repurpose a reconciled token.

Rollback: keep the new ledger paused and stop new optional production, while
keeping data reads available. Restore a known code revision only under that
pause; old writers do not implement the new cap. Do not disable v1 on one writer
while others assume its allocation. Preserve both ledgers, reservations, data
and pointers. No credential revocation, IAM change, cleanup, migration or public
access change is required by F01. Any later such change needs its own exact
approved plan and read-back.

Read-only preview:

```sh
uv run python scripts/storage_capacity.py --policy /path/to/reviewed-draft.json --dir /tmp/scratch-store
# omit --dir for authenticated R2 inventory; this command never writes R2
```

## Vendoring and consumer acceptance

P01 and O01 must copy `src/ingest/storage_admission.py` into their own tooling
package, plus the schema, example and acceptance vectors. Record this canonical
repository, exact source commit, date and SHA-256 of each copied file in their
shared-contract lock. `contracts/storage-admission-v1.lock.json` pins this
repository's source digests. Verify them in local/CI gates; deliberate upgrades
refresh producer and consumer tests together. The store adapter supplies
`get_with_etag`, conditional `put`, strict paginated `list_objects`,
`multipart_bytes`, and its definite-conflict exception tuple. It must disable
SDK write retries and never convert a generic timeout into a definite conflict.
For OSCAR's Workers/private upload APIs, implement equivalent v1 CAS and debit
semantics in JavaScript against the same bucket ledger; a Python-only publisher
migration is insufficient. No paid plan upgrade is implied.

P01 acceptance: reserve the actual versioned wire bytes, dependency-rewritten
manifests and pointer peak before any upload; count all legacy and immutable
revisions/direct files; preserve saved-briefing references; use the adapter for
prepared, polars and manual publication. Race simultaneous prepared/forecast
writers at equality and one byte over. Interrupted uploads, uncertain ledger
and pointer writes must retain charges and the old readable pointer until
confirmed. No pruning to create hypothetical upload headroom.

O01 acceptance: inventory all deployed bucket bindings and every publisher,
including immutable race generations/pointer archives, private uploads,
recompute, backups/current mirrors/snapshots, preview and manual transport.
Stage exact encoded/compressed bytes before reservation or bound streamed
uploads conservatively. Reserve the full temporary generation/mirror peak;
retain revisions and pending parts in inventory. Prove allocation denial at
exact equality/one byte over, two distinct simultaneous writers, foreign growth,
crash/expiry, unknown inventory and uncertain reservation/pointer responses.
Private receipt/user/object identifiers must stay out of public ledgers.
Reconciliation requires confirmed fencing, leaves essential data intact and
cannot reset the existing paid-work ledger. Do not deploy enforcement until
all participating implementations and rollback versions are accounted for.

## Cost model boundary

`account_cost_model` reconciles one physical total across buckets and applies
Standard's account free allowances once. It requires explicit operation counts,
conversion and tax assumptions; unknown inventory or inputs keep the respective
cost unknown. Use measured snapshots and proposed peak envelopes as separate
scenarios. A constant daily-peak plateau is an estimate, not observed GB-month
billing. Private financial review owns the EUR 20 total (Google/Firebase 12,
R2 4, reserve 4; domains excluded). A physical-byte allocation cannot guarantee
that target: operation rounding, public reads, daily peaks and delayed taxes/
conversion still matter. Preserve the approved spending ledger and free plans.

Official sources checked 2026-10-04: [R2 pricing](https://developers.cloudflare.com/r2/pricing/)
for daily-peak GB-months, whole-unit rounding and shared allowances;
[R2 consistency](https://developers.cloudflare.com/r2/reference/consistency/)
for authenticated object/list reads; [conditional S3 extensions](https://developers.cloudflare.com/r2/api/s3/extensions/)
for destination conditions. Cached public-domain reads cannot establish control
state. No live synthetic write was used to validate F01.
