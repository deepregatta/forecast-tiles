# Shared contract ownership and pins

Runtime code stays within its repository. Shared schema/fixture reuse is vendored;
verification reads only committed local files and never fetches mutable `main` or
needs a sibling checkout.

| Contract | Canonical owner | Producer | Consumers |
|---|---|---|---|
| Four `forecast-*.schema.json` schemas: tile, manifest, latest, latest-regional | `passage/contracts/` | forecast-tiles | Passage; Tactician models the fields it uses |
| PFT1 weather/ensemble codec golden objects and expectations | `forecast-tiles/tests/fixtures/golden-*` | forecast-tiles | Passage; Tactician retains the weather golden |
| Three `land-index-*.schema.json` schemas and the Raz de Sein golden bundle | `forecast-tiles/contracts/` and `tests/fixtures/land-index-raz/` | forecast-tiles | Tactician `core/land` |

The lock records each canonical repository/path, `PFT1/schema-1` or
`TLI1/schema-1` version, canonical Git blob revision (`git-blob:`), and SHA-256.
Blob revisions identify the already shared artifact bytes, without embedding
private implementation commits or review evidence. Both revision and digest
must agree for exact copies. Scoped Git attributes retain LF for pinned JSON
and binary bytes for tiles, including `autocrlf=true` checkouts. The scripts also reject missing/duplicate inventory
entries. The format version is for the pin record, not a new wire schema.

These are pins to a declared canonical revision, not a claim to track the latest
upstream revision. An intentional canonical change requires explicit review:
copy the versioned artifact, update its revision/digests and any documented
representation in affected repositories, then run both producer and consumer
tests. Do not refresh pins just to silence a failure. Byte changes (including
formatting) require review even if old outputs still validate.

The negative suite copies the checker, lock and real artifacts into an isolated
temporary checkout, then weakens each schema, changes JSON fixture versions or
corrupts binary fixtures. It also rejects missing artifacts, removed/duplicate
pins, unknown pin formats, and a digest-only refresh with a stale revision. No
producer implementation or private product evidence is copied into the public
repository.

`contracts/shared-contracts.lock.json` pins four vendored forecast schemas,
four PFT1 goldens, three canonical land schemas and the three-file land golden.
All are exact bytes; no intentional artifact divergence exists.

Run `.venv/bin/python scripts/check_shared_contracts.py` and
`.venv/bin/python -m unittest discover -s scripts -p test_shared_contracts.py`.
Both are first in the Python group of `bash scripts/verify.sh`, which CI uses.
The existing codec, dry-run, regional layout, land-fixture and land-publish
tests validate emitted artifacts against these schemas and geographic answers.
This pin work does not change any published schema or fixture bytes.
