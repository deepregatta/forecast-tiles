"""Regional deterministic models from Open-Meteo's public AWS files.

docs/open-meteo-bulk-implementation-plan.md: three whole `.om` files per
complete `data_run/` cycle, fetched anonymously with requests and decoded
locally with omfiles (the optional `openmeteo` extra, GPL-2.0-only). No
hosted API, no range reader, no raw-data mirror.

  registry  product definitions and every per-layer setting the CLI needs
  catalog   complete-run metadata, cycle selection and source identity
  reader    whole-file GETs and local decoding to [time, lat, lon]
  grids     exact geometry and the versioned expected footprints
  adapter   unit conversion, gust alignment, masking -> ForecastCube
"""
