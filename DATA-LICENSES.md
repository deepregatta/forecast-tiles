# Forecast data notices

The repository's MIT licence covers its code. It does not relicense upstream
weather data or optional ingestion libraries.

## Met Office UKV

UKV source data, the cropped UKV weather fixtures in
`tests/fixtures/openmeteo/ukv/`, and transformed UKV forecast tiles are provided
under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
British Crown copyright 2023–2025, the Met Office, as stated by the
[Met Office-managed source listing](https://registry.opendata.aws/met-office-uk-deterministic/).
Met Office UK Deterministic (UKV) 2 km was accessed on 2026-10-02. Bulk files
are distributed by [Open-Meteo](https://github.com/open-meteo/open-data);
that catalogue's general CC BY notice does not replace UKV's upstream terms.

Modifications: native cell CRS correction from the bulk-declared sphere to
the primary NetCDF ellipsoid; speed/from-direction conversion to geographic
vectors; bilinear remapping to a 0.025° geographic grid with masked boundaries;
quantization and compression. Offline fixtures are cropped and recompressed.
Source URLs, ETags, checksums and comparison results accompany the fixtures;
run manifests retain source object identity, attribution and modifications.

Preserve attribution, the licence link and modification notices when sharing
these data, including GRIB exports. Adapted UKV data retain the same licence;
do not add restrictions incompatible with it or suggest provider endorsement.
The upstream service is offered without support or warranties. See the
[licence legal code](https://creativecommons.org/licenses/by-sa/4.0/legalcode.en).
Software that reads the data keeps its own licence.

## AROME and ICON-EU

Attribution and data terms are carried by each product's run manifest and
cropped fixture metadata: Météo-France AROME and Deutscher Wetterdienst ICON-EU,
distributed through Open-Meteo's CC BY 4.0 bulk catalogue. Consult the provider
and catalogue links in the implementation plan when redistributing those data.
