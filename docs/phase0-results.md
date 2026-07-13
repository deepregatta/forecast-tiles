# Phase 0 — size-validation results (go/no-go gate)

Measured 2026-07-13 with `scripts/size_prototype.py` against live cycles
(GFS/GEFS/GFS-Wave cycle 2026-07-12T18Z, ECMWF open data latest). Sampled
forecast steps were downloaded via `.idx` byte-range subsetting, quantized and
tiled with the real `tilekit` codec, gzipped at level 9, and extrapolated to
the full per-run tile set.

## First pass — plan axes: NO-GO by 1%

| Layer | Full-run gz | Wall time (sample) | Basis |
|---|---|---|---|
| weather (GFS 0.25°) | 0.87 GB | 40 s | measured, full globe |
| ensemble (GEFS 0.5°, 31 members) | 1.08 GB | 36 s | measured wind, gust assumed ×2 |
| waves (GFS-Wave 0.25°) | 0.55 GB | 43 s | measured, full globe |
| weather-ecmwf (ECMWF open 0.25°) | 0.19 GB | 26 s | measured, full globe |
| currents (GLO12 1/12°) | 1.35 GB | — | analytic estimate¹ |
| **Total** | **4.04 GB** | | ×2 retention = **8.09 GB** > 8 GB guard |

¹ No CMEMS credentials in the measurement environment: raw int16 size × 0.71
ocean fraction × the gzip ratio measured on the GFS wind group. Must be
verified with real GLO12 data in Phase 3.

## Fallback levers applied (in the plan's pre-agreed order): GO

1. **Ensemble 6-hourly beyond 144 h** — axis becomes 3 h → 144 h (49 steps) +
   6 h → 384 h (40 steps): 1.08 → **0.97 GB**.
2. **Currents 6-hourly** — 41 steps to 240 h: 1.35 → **0.68 GB**.

| | Full-run gz |
|---|---|
| Revised total | **3.26 GB** |
| ×2 retention | **6.53 GB** (8 GB guard, ~1.5 GB headroom) |
| **Verdict** | **GO** (limit 4.0 GB/generation) |

## Wall-time signal

The whole 5-layer sampled measurement ran in ~2.5 minutes on a laptop
(16 threads); per-layer full runs extrapolate well under the 45-minute
runner budget. GEFS (31 members × 89 steps × 2-3 vars) remains the layer to
watch on GitHub-hosted runners.

## Notes carried into later phases

- **Tidal aliasing in currents**: GLO12 hourly output includes tidal currents;
  a 6-hourly axis samples the ~12.4 h tidal cycle badly. Phase 3 should decide
  between (a) 6-hourly instantaneous fields with a "not tidal streams" UI
  caveat (current plan) or (b) CMEMS daily-mean de-tided fields (smaller,
  semantically cleaner, 24 h cadence). Either way the UI must not present
  these currents as tidal stream predictions.
- **APCP is absent at f000** (no accumulation at analysis time) — the pipeline
  fills the first step with missing values, as the prototype does.
- ECMWF open data measured wind u/v only; gust (`10fg6` family) needs a
  param-availability check in Phase 3 — budget impact is small (layer is
  0.19 GB with 2 vars).
- Quantization round-trip: decoded values matched source GRIB within
  ≤ scale/2 everywhere (asserted in codec tests; spot-checked in prototype).
