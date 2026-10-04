# Public forecast reads and cache policy

F02 preparation, 2026-10-04. Local tooling is implemented; provider changes are
pending approval. The custom domain remains readable and the managed development
endpoint remains enabled. Legacy compatibility has not been globally cleared.
No data, forecast cadence, publisher credential, storage allocation or plan
changes belong to this preparation.

## Existing producer and consumer contract

`forecast.deepregatta.com` is the production public origin. Authenticated
publishers continue using the S3 endpoint, not this hostname. P02 verified
Passage's current production/preview settings, real browser conditional reads,
GRIB download, prepared inputs and a legacy saved briefing. Its maintained
[consumer inventory](https://github.com/deepregatta/passage/blob/main/docs/forecast-consumers.md)
also records the remaining installed/offline/external-client gaps.

Tactician's shore transport, shell link and harbor default use the custom domain.
Shore fetches JSON with ordinary GET and resumes compressed forecast objects
using `Range: bytes=<start>-<end>`. It accepts correct 206 ranges or a complete
200 object when ranges are ignored. It needs readable `Content-Range` and exact
compressed bytes; a challenge page would break this native client. Its land
consumer reads the separate TLI1 pointer, manifest and gzip tile paths.
Source defaults do not prove the configuration of every installed binary.

| Object family | Origin policy | Consumer behavior |
| --- | --- | --- |
| `latest.json`, `latest-regional.json`, `prepared/latest.json`, `land-index/latest.json` | `public, max-age=300, must-revalidate` | Fresh discovery; Passage explicitly revalidates JSON, Tactician uses ordinary GET |
| `forecast-runs/<run>/manifest.json`, PFT1 `.bin.gz` | `public, max-age=31536000, immutable` | Run identity and compressed-byte manifest digests remain exact |
| `prepared/runs/<run>/...<revision>.json/png` | Same immutable policy | Each action pins exact artifact revisions; historical saved paths stay verbatim |
| `land-index/<index>/manifest.json`, TLI1 `.bin.gz` | Same immutable policy | Conservative routing-index identity remains exact |

The existing producer already implements these policies. F02 does not rewrite
objects or introduce new cache headers. Status/control objects must not receive
an invented immutable lifetime.

## Dated public HTTP observations

The current hostname rule enables caching and uses
`edge_ttl: {mode: "bypass_by_default"}`. Its complete read-back had no Browser
TTL override or status-code TTL entries. The zone browser minimum was four
hours. All four pointer GETs consequently advertised `max-age=14400`; HEAD
advertised 300. P02 independently observed edge revalidation at five minutes
and real Chromium conditional revalidation, so this discrepancy is not proof
of four-hour application staleness.

Root/regional/prepared/land JSON, gzip and PNG samples remained readable.
Immutable objects advertised one year. Cold GETs missed and repeats hit;
two adjacent forecast/TLI1 ranges returned correct 206 bytes from cache.
ETag/Last-Modified conditionals returned 304 and client reload headers still
hit warm cache in the sample. HEAD was DYNAMIC. Distinct query values produced
MISSes, with the repeated value then hitting. Missing-object GETs returned
404 BYPASS twice. These observations concern sampled objects and one location;
they do not count billed R2 operations or guarantee cache retention everywhere.

## Prepared cache change

Add only `browser_ttl: {mode: "respect_origin"}` to the current forecast-host
rule. Preserve its hostname expression, activation, cache eligibility, origin
edge policy, order and other settings. Do not change the global zone browser
TTL, introduce an edge override, or cache errors under the immutable lifetime.
Expected pointer GET browser TTL is 300; immutable responses keep 31536000.
Old browser entries can retain their existing four-hour lifetime: a CDN purge
does not invalidate client caches. Extra ordinary browser requests are possible;
the edge lifetime remains origin-based. This is policy alignment, not a client
freshness repair or a billing cap.

The offline builder reads a complete private rule snapshot, checks the reviewed
ID, hostname/action/activation and edge policy, retains other action parameters,
and emits a single-rule update plus exact rollback. It refuses unknown top-level
fields rather than dropping an unreviewed setting. It makes no network call.

```sh
.venv/bin/python scripts/prepare_public_read_policy.py /tmp/f02/cache-before.json \
  --rule-id <reviewed-rule-id> --output-dir /tmp/f02/drafts
```

Compare the recorded source-rule digest against a fresh provider read before
applying an approved change. Use the Rulesets API **single-rule PATCH**, or
the dashboard's existing-rule editor. Do not replace the entire ruleset from
this one-rule payload. Generated provider IDs/snapshots/drafts stay private.

## Free protections and residual exposure

Keep HTTPS, TLS 1.2, Strict origin TLS, existing CDN caching and Free DDoS
protection. Check available Free custom-rule and rate-limit slots privately;
preserve existing shared-zone protections rather than displacing another
service's rule. Per-IP limits also do not
bound distributed account-wide reads. Do not introduce interactive challenges
for browser or native forecast downloads.

The builder emits an optional Free custom-rule draft, restricted to the
forecast host, blocking methods other than GET/HEAD/OPTIONS. It leaves normal
reads, CORS preflights, ranges, validators and query-bearing reads usable.
This rejects unsupported request methods but **does not limit GET floods**.
It requires separate approval and a fresh slot/rule-order check. Rollback
disables/removes only that new rule; existing shared-zone rules remain untouched.

Query normalization is not part of the prepared update. The provider's current
documentation distinguishes query-key capabilities by plan; Free entitlement
and external query semantics were not proved. Do not upgrade or suppress
client cache-busting as a shortcut. Ordinary cold misses, cache eviction,
pointer revalidation, query variations, HEAD, missing objects and uncacheable
requests can still reach R2. Available metrics need not distinguish public
hostnames from authenticated operations; no paid logging, Workers, WAF or
Cache Reserve is added. The managed hostname has its own
provider rate limiting, without a verified monetary ceiling, and bypasses the
custom domain's cache/security controls.

Standard R2 shares 10 million Class B operations per month across the account;
excess rounds up to whole millions at $0.36 each. For illustrative assumptions
of EUR 1/USD and a 1.2 tax multiplier, Class B alone is EUR 0.432 at 10,000,001
reads, EUR 4.32 at 20 million, and EUR 38.88 at 100 million. Add all buckets'
storage/Class A/other costs and actual tax/card conversion before comparing to
the R2 EUR 4 allocation. Delayed usage is not a final bill. The helper's
`class_b_exposure` exposes these assumptions; it enforces no limit.
Pause new optional paid production at established limits while retaining data
and ordinary reads. The observed producer byte limit is not a universal
physical-byte or monetary ceiling. Current total policy remains EUR 20/month,
including tax/conversion and excluding domains: Google/Firebase EUR 12, R2
EUR 4, reserve EUR 4. Keep free plans.

## Endpoint retirement gate, rollback and read-back

Before a disablement proposal, verify Passage canonical and required historical
deployment assets/configuration, Tactician deployed/installed builds and real
devices, caller environment overrides, external scripts and archived absolute
URLs. Migrate configurable callers to the custom domain while retaining each
object path/run/index/revision. Ship updated installed clients and verify
saved-briefing reload, offshore/reconnect behavior and resumable downloads
before retiring their previous hostname. Do not rewrite historical provenance.
Unreachable installed clients and externally archived absolute URLs remain
blocked consumers until an explicit compatibility decision. P02 alone does
not approve or clear endpoint retirement.

After compatibility and specific approval, the exact access action is to set
only `passage-forecast`'s managed development-domain `enabled` to false. Preserve
the custom-domain attachment/activation, CORS, S3 access, stored objects and
cadence. Rollback sets only that managed endpoint back to true, under applicable
approval, and restores its public exposure. It is not a data rollback.

For approved cache/access steps, read back the changed fields and both endpoint
settings. Verify normal/conditional GETs for all four pointers; immutable JSON,
PFT1/TLI1 gzip and PNG; CORS/CSP; real Passage GRIB and saved briefings; native
resumable ranges. Compare bytes to manifest digests and prepared revision hashes.
After disablement, confirm the alternate endpoint denies actual public reads
as well as reporting disabled. Keep missing device/publication-transition/
legacy/billing proof unresolved. No synthetic production write is needed.

The bounded probe runs sequentially against explicitly supplied sample paths,
with capped response sizes, whitelisted headers and no redirects/credentials:

```sh
.venv/bin/python scripts/probe_public_reads.py --base-url https://forecast.deepregatta.com \
  --samples /tmp/f02/samples.json --output /tmp/f02/read-back.json --queries \
  --require-origin-browser-ttl
```

Each sample declares `path`, `kind` (`json`, `png`, `pft`, `tli`) and `mutable`.
Optional `bytes`/`fnv64` verify a manifest's compressed tile identity; `sha256`
verifies a prepared content revision. Supply pointers, root/regional manifests
and tiles, prepared artifacts and a land manifest/tile. Omit the strict browser
TTL flag for baseline diagnosis; discrepancy reporting then remains separate
from usability failures. Offline CI exercises policy drift/rollback, billing
rounding and loopback conditional/range/error/TTL/integrity behavior. Neither
tool is run against production by CI.

Official references checked on 2026-10-04:
[R2 public buckets](https://developers.cloudflare.com/r2/buckets/public-buckets/),
[cache-rule settings](https://developers.cloudflare.com/cache/how-to/cache-rules/settings/),
[R2 pricing](https://developers.cloudflare.com/r2/pricing/),
[Free rate-limit capabilities](https://developers.cloudflare.com/waf/rate-limiting-rules/),
[single-rule updates](https://developers.cloudflare.com/ruleset-engine/rulesets-api/update-rule/),
[managed-domain access update](https://developers.cloudflare.com/api/resources/r2/subresources/buckets/subresources/domains/subresources/managed/methods/update/).
