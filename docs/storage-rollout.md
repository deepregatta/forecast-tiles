# Coordinated storage rollout

D01 preparation, 2026-10-04. The v1 canonical bundle and the Passage/OSCAR
vendor pins are compatible. This is a rollout runbook, not a production
activation receipt. Account inventory, effective settings and writer coverage
must be refreshed before applying any reviewed proposal. Private allocations,
inventory, approvals and provider receipts belong outside these repositories.

## Prepare exact conditional inputs

[`prepare_storage_policy.py`](../scripts/prepare_storage_policy.py) reads the
existing ledger and complete physical/multipart inventory. It prepares only
`paused=true`, `rollout_complete=false` policies. It never writes to a provider,
resumes writers, reconciles tokens or modifies the paid-work ledger.

```sh
.venv/bin/python scripts/prepare_storage_policy.py \
  --proposal /private/empty-allocation-template.json \
  --output /private/paused-conditional-policy.json
# Add --dir /tmp/scratch-store to simulate without provider access.
```

The allocation template has zero accounting and an explicit reviewed ownership
map, ceilings, bucket limit and headroom. The output replaces those zero
baselines with measured occupancy only when the authenticated read confirms
that no ledger exists. Its create condition is `If-None-Match: *`. This tool
does not initialize or reset paid-work counters.

For an existing ledger, the output keeps its epoch, baselines, every active,
finished or expired reservation, and all duplicate history. Its update condition
is the exact read ETag. Owner names, modes and prefixes cannot change through
this path. Unexpected managed growth or a reduced ceiling below existing
charges denies preparation; use the separately fenced reconciliation protocol.
The sum of owner ceilings, headroom and the fixed control allowance must fit the
bucket limit. Account allocation totals still require a separate private review.

The private output wrapper contains `condition` and `policy`. Only the encoded
`policy` is a provider object. Never publish the wrapper, account allocations,
financial review, private inventory, stop evidence or approval records. Files
are created exclusively with mode 0600, outside this checkout. Preparation while
writers are active is a proposal snapshot; it must be repeated after approved
pause/drain. A conditional write cannot compensate for an unfenced old writer.

## Release order and required coverage

1. Deliver compatible adapters with enforcement off. Confirm current main,
   deployed versions and all entry points: scheduled/manual/fallback/force root
   and regional workflows, one-shot land, Passage prepared publication, OSCAR
   race publication/backfill/recompute, ML snapshot/current, both private-upload
   bindings, control writers and any direct transport. Preserve the regional
   canary gates and disabled full cadence.
2. Review account allocations and nonsecret public policy content. Unknown or
   unbounded old writers prevent completion. Before conversion, their whole
   demonstrated peak must remain an unmanaged allocation; a configured ceiling
   alone does not constrain a legacy writer. If old/new writers share a prefix,
   retain the whole namespace as unmanaged until old writers are stopped and
   drained. Do not split identical-prefix ownership or infer fencing from age.
3. Obtain approval for exact pause/drain, policy and configuration changes. Stop
   new optional work, drain existing producers and archive controls/ETags without
   resetting usage. Include manual, backup, local service and historical callers.
   Keep pointers, saved references and ordinary reads available. No deletion,
   public endpoint closure, IAM change or credential revocation is in D01.
4. Repeat authenticated inventory and policy preparation. Create missing ledgers
   conditionally, or CAS-update existing accounting while paused. Convert an
   unmanaged namespace only after externally proven stop/drain and a reviewed
   fresh-inventory/epoch transition. Preserve reservations and duplicate history;
   a conversion is not permission to discard uncertain debits.
5. Enable matching flags while controls remain paused and incomplete. Read back
   actual deployed adapters and effective settings, including inherited variables
   and already-running workflow snapshots. No old in-flight job may survive the
   conversion boundary. Setting a variable does not alter an existing process.
6. CAS `rollout_complete=true` and resume only after every writer is bounded or
   fenced and rollback versions are accounted for. Unknown responses require
   authenticated exact read-back before proceeding; never retry an uncertain PUT
   or reseed a missing-looking ledger. Observe ordinary scheduled publication.

OSCAR has two additional gates. Native private uploads need separately proven
zero multipart occupancy and fencing of all S3/manual/multipart paths before
`CAPACITY_NATIVE_ONLY=1`. Its GCS FUSE producers cannot be bounded by R2: the
new worker pauses when capacity is enabled, and the deployed older image needs
an explicit reviewed pause/deployment path. Missing those proofs prevents a
fleet-wide resume. Record substantive publisher gaps as handoffs; do not expand
this stage into access changes or new GCS admission implementation.

## Read-back, rollback and evidence

For each effective control, verify exact policy bytes, ETag, owner allocations,
baseline/debit/history preservation and flags against the approved proposal.
Public cached GETs do not establish control state. Verify current/previous
pointers, manifests and saved artifact paths remain readable. Scheduled positive
evidence must show reservation-before-upload and confirmed immutable/pointer
publication. A successful paused/skipped job is not that evidence. Do not trigger
paid recomputes or large uploads as tests.

Rollback is a coordinated pause with all producers drained. Retain controls,
usage, reservations, duplicate history and data. Reverting code or turning one
flag off while other writers rely on the allocation opens a bypass. Do not
restore an archived pre-rollout ledger over newer reservations. Reconciliation
requires external fencing for every unfinished token, fresh inventory, a CAS
epoch increment and paused read-back; expiry alone releases nothing. Readable
data is preserved throughout pause and resume.

Report separately: local simulated enforcement; hosted CI; deployed adapter and
provider configuration; activation approval/application; natural publication;
client/device evidence. Missing evidence remains pending. Physical allocations
do not establish monthly monetary compliance: public read costs, operations,
storage history and conversion remain separate review inputs.

Official references checked 2026-10-04: [R2 conditional destination writes](https://developers.cloudflare.com/r2/api/s3/extensions/),
[authenticated consistency and cached-domain limits](https://developers.cloudflare.com/r2/reference/consistency/),
[account-wide pricing](https://developers.cloudflare.com/r2/pricing/).
