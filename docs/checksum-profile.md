# Checksum phase measurement (2026-10-03)

The original Python FNV-1a loop accounted for about 23% of the bounded GFS
encode/compress/manifest phase in an initial replay (1.00 s of 4.39 s, 11.70 MB
compressed). This justified replacing that loop with native FNV-1a through
[`fnv-c`](https://pypi.org/project/fnv-c/0.3.1/), pinned to `0.3.1` in
`pyproject.toml` and by artifact SHA-256 in `uv.lock`. Its MIT-licensed C source
uses the same offset basis, unsigned bytes, xor-before-multiply and unsigned
64-bit overflow. The wrapper still returns 16 lowercase hex digits and hashes
the complete compressed object, including embedded zero bytes.

The change is confined to `ingest.publish.fnv64`, also used by the existing
land publisher. It adds no checksum cache, retained byte copy, runtime
compilation or alternate wire algorithm. Manifest construction hashes each
tile once. The post-publication check independently hashes up to three freshly
fetched objects; reusing local hashes there would bypass the integrity check.
Tile encoding, deterministic gzip settings, schemas, immutable publication and
pointer/retention behavior are unchanged.

## Reproduce

Install the committed dependencies with `uv sync --locked --all-extras`. The
lock includes small native wheels for the supported CPython 3.12/3.13 Linux
runner platforms. On platforms without a compatible wheel, upstream's source
build requires a C compiler, Python headers and CFFI build dependencies. The
normal wheel installation does not compile at runtime or fall back silently.
An isolated installation of this repository's built wheel also passed the
known-vector smoke check and declares the exact native runtime dependency.

Run sequentially on Linux, selecting an available CPU if affinity is desired:

```sh
.venv/bin/python scripts/checksum_profile.py --fixture weather --repeats 5 --cpu 2 --memory
.venv/bin/python scripts/checksum_profile.py --fixture ensemble --repeats 5 --cpu 2 --memory
.venv/bin/python scripts/checksum_profile.py --fixture regional --repeats 5 --cpu 2 --memory
bash scripts/verify.sh
```

`--cpu` is optional; CPU 2 was available for this measurement. Each backend
gets one warmup and five alternating measured passes in the same process.
The script retains the original Python implementation as an oracle and
asserts a single SHA-256 over tile IDs, compressed tiles and canonical manifest
bytes across every pass. Timestamps, seed, fields, environment and gzip level
are identical. Fixture creation and SHA-256 proof are outside phase timing.
Memory uses a separate fresh process for each backend; traced passes are
excluded from timing comparisons. No provider download or R2 call occurs.

## Fixture bounds and interpretation

Root fixtures start with prequantized arrays; the regional fixture starts
with float32 knot fields quantized during encoding, matching its adapter and
including the intentionally missing gust at +0 h. They use native resolution
and full forecast axes on bounded spatial crops:

| Fixture | Geometry | Variables and forecast axis | Input arrays |
|---|---|---|---|
| GFS weather | 40 x 320 at 0.25 degrees; eight 10-degree tiles | Eight variables; 161 hourly / 81 three-hourly steps | 22.73 MB |
| GEFS ensemble | 20 x 160 at 0.5 degrees; eight 10-degree tiles | Wind/gust mean + anomalies; 31 members, 89 steps | 18.80 MB |
| AROME regional | 100 x 400 at 0.025 degrees; two partial 5-degree tiles | Wind u/v/gust, 52 hourly steps | 24.96 MB |

Fields combine a small spatial gradient with seeded noise. They exercise
real tile dimensions, axes, quantized dtypes and regional manifest metadata,
but do not represent a measured distribution of provider entropy or a valid
production forecast. `ValidationReport` is empty because this experiment
begins after validation. Source resolution/download/decode, validation,
uploads, post-publication read-back and retention are outside its timed phase.
No end-to-end ingestion or production speedup is inferred.

Tracemalloc records allocations after the cube exists; input array bytes are
reported separately. Retained allocations cover the compressed tile list and
manifest. Peak RSS includes the interpreter, imports, cube and native buffers;
it is a process high-water mark rather than a per-phase allocation count.
The native loop uses constant auxiliary space. This change does not remove
the retained cube or compressed tile list.

## Measured result

The final comparison used CPython 3.13.13, NumPy 2.5.1, `fnv-c` 0.3.1 and
CPU affinity 2 on an AMD Ryzen 7 7735HS Linux host with the powersave governor.
Wall times vary on this shared host. Medians below include the entire local
`build_tiles` + `build_manifest` phase, rather than just the checksum loop.
Raw samples, ranges, byte identities and separate memory passes are retained
in [checksum-profile-results.json](checksum-profile-results.json).

| Fixture | Compressed MB | Encode s (Python/native) | Gzip s (Python/native) | Hash s (Python/native) | Complete phase s (Python/native) | Median phase reduction |
|---|---|---|---|---|---|---|
| GFS weather | 11.70 | 0.013 / 0.014 | 3.569 / 3.480 | 1.073 / 0.012 | 4.666 / 3.519 | 24.6% |
| GEFS ensemble | 13.89 | 0.009 / 0.008 | 0.549 / 0.512 | 1.071 / 0.012 | 1.617 / 0.546 | 66.2% |
| AROME regional | 7.33 | 0.095 / 0.075 | 1.288 / 0.909 | 1.134 / 0.008 | 2.737 / 1.008 | 63.2% |

Weather whole-phase ranges were 3.987–5.423 s before and 3.229–4.867 s after;
ensemble ranges were 1.557–1.846 s before and 0.540–0.629 s after. Regional
ranges were 1.317–3.840 s before and 0.870–1.782 s after. Encoding
and compression are unchanged and their normal timing variation contributes
to observed totals. The isolated hash saving accounts for about 22.7%,
65.5% and 41.2% of the original median whole phases, respectively. In
particular, the regional 63.2% observed median difference includes considerable
variation in unchanged compression; it cannot all be attributed to this
optimization. These are local fixture measurements, not forecasts of a
multi-GB production run's duration.

| Fixture | Traced retained MB after manifest (Python/native) | Traced peak MB (Python/native) | Process peak RSS MB (Python/native) |
|---|---|---|---|
| GFS weather | 11.721 / 11.722 | 23.020 / 23.020 | 100.84 / 101.65 |
| GEFS ensemble | 13.900 / 13.901 | 24.212 / 24.212 | 99.92 / 100.11 |
| AROME regional | 7.342 / 7.342 | 46.345 / 46.345 | 127.92 / 128.11 |

MB is decimal. Input arrays above are additional to traced retained bytes.
Both comparison backends use the same installed dependencies, so these RSS
figures do not measure a change in cold import overhead. No retained-memory
reduction is claimed; measured traced differences are around a kilobyte.

## Compatibility evidence

`tests/test_checksums.py` compares empty/NUL/high-bit/repeated/all-byte inputs,
seeded random lengths through 1 MiB, committed compressed and uncompressed
weather/ensemble goldens, and complete global/ensemble/regional manifest
bytes against the original loop. Repeated builds remain byte-identical.
The offline suite also exercises the CLI scratch publisher, land publication,
post-publication integrity checks, schemas and concurrent immutable publication.
`tests/test_r2_conditional.py` remains deliberately excluded by `scripts/verify.sh`;
real provider writes and live ingestion are separate operator evidence.
The final `bash scripts/verify.sh` run passed 375 Python tests, Ruff lint,
format checks for 88 Python files, dispatcher typecheck and 60 Vitest tests.
