# Forecast data notices

The repository's MIT licence covers its code. It does not relicense upstream
weather data or optional ingestion libraries.

The machine-readable record per source (licence, attribution, whether the data
may be redistributed, and the date its terms were read) is
[`contracts/data-sources.json`](contracts/data-sources.json), validated against
[`contracts/data-sources.schema.json`](contracts/data-sources.schema.json). A
value appears there only where a file in this repository already states it;
every other source reads `unconfirmed`. The notices below stay the prose
authority, UKV's ShareAlike duties above all.

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

## Credit lines for derived tiles

Recorded on 12 November 2026. The tiles and Passage's GRIB exports are derived
from five sources whose terms ask for a credit and, for CC BY 4.0, an indication
of changes. One wording per product is used everywhere this repository states it:
the register's `attribution` and the line Passage's GRIB files page shows are the
same text, in the form credit; licence (link); modification clause. The credit
sentences and DOIs are the providers' own (see the section above); the clause is
ours.

| Product | Credit line |
|---|---|
| ECMWF IFS (`ecmwf-open-ifs`) | This service is based on data and products of the European Centre for Medium-Range Weather Forecasts (ECMWF); ECMWF open data; CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/); modified: converted and quantized data |
| Copernicus Marine GLO12 (`cmems-glo12`) | Generated using E.U. Copernicus Marine Service Information; doi:10.48670/moi-00016; modified: converted and quantized data |
| Copernicus Marine IBI (`cmems-ibi`) | Generated using E.U. Copernicus Marine Service Information; doi:10.48670/moi-00027; modified: converted and quantized data |
| Copernicus Marine North West Shelf (`cmems-nws`), staged | Generated using E.U. Copernicus Marine Service Information; doi:10.48670/moi-00054; modified: converted and quantized data |
| Copernicus Marine Mediterranean (`cmems-med`), staged | Generated using E.U. Copernicus Marine Service Information; doi:10.48670/mds-00359; modified: converted and quantized data |
| DWD ICON-EU (`dwd-icon-eu`) | Deutscher Wetterdienst ICON-EU via Open-Meteo; CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/); modified: converted and quantized data |
| Météo-France AROME (`meteofrance-arome`) | Météo-France AROME via Open-Meteo; CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/); modified: converted and quantized data |

ECMWF's disclaimer is not part of these lines: its wording is not recorded.

What "converted and quantized" rests on, by source file: the ingest multiplies
speeds and currents in m/s by 1.943844 to knots (`MS_TO_KT`) and stores each value
as a scaled 16-bit integer (`quantize`), in `src/ingest/sources/ecmwf_open.py`
(wind and gust), `src/ingest/sources/cmems.py` and `src/ingest/sources/ibi.py`
(surface currents; the surface layer is selected with `isel(depth=0)`) and
`src/ingest/sources/openmeteo/adapter.py` (AROME and ICON-EU; wind given as speed
and direction is converted to u/v components, and values outside the registered
footprint are masked). Only UKV is remapped to another grid (bilinear, same
adapter), which is why its own line says so and the others do not.

The tile manifests already published, and the next ones until a producer change
is made and a run published, still carry the earlier strings: `provenance.attribution`
from `src/ingest/sources/openmeteo/registry.py` (AROME, ICON-EU, UKV), `cmems.py`
and `ibi.py` (GLO12 and IBI, without "Generated using" or the modification clause)
and `ecmwf_open.py` (the ECMWF provenance, "ECMWF open data IFS 0.25deg (CC BY 4.0)").
This section changes the register and the notice, not those producers.

## NW Shelf and Mediterranean currents (staged 14 November 2026)

`cmems-nws` and `cmems-med` are staged for Tactician and not published: no run
is in the bucket ([docs/shelf-currents.md](docs/shelf-currents.md)). Their
terms were read on 14 November 2026 at
https://marine.copernicus.eu/user-corner/service-commitments-and-licence, the
same Copernicus Marine Service licence as GLO12 and IBI: its annex, section
2.2(b), permits modifying the products and distributing derived work "for any
purpose"; section 2.4(a) asks for the credit "Generated using E.U. Copernicus
Marine Service Information" with the product's DOI on the home page or the
page where users get the data; section 2.6 asks for records that trace use,
passed into every downstream licence. The tiles are derived work (converted to
knots and quantized; the surface layer only). Each product's page was read the
same day: https://data.marine.copernicus.eu/product/NWSHELF_ANALYSISFORECAST_PHY_004_013/description
and https://data.marine.copernicus.eu/product/MEDSEA_ANALYSISFORECAST_PHY_006_013/description.
The Mediterranean dataset version 202511 retires on 2027-01-12. Their
manifests carry the credit line in `provenance.attribution` without the
modification clause, as IBI's do.

## Display chart (TCP1)

The chart archives under `charts/` and the golden fixture in
`tests/fixtures/chart-pack-cherbourg/` are derived from OpenStreetMap data
(ODbL 1.0, © OpenStreetMap contributors) through the Protomaps daily basemap
build (© Protomaps) and Open Waters' Seamap tiles (© Open Waters: Seamap; tiles
CC BY 4.0, being corrected to ODbL), from SHOM Balisage (Licence Ouverte 2.0,
"Balisage © Shom") and from EMODnet Digital Bathymetry DTM 2024 (CC BY 4.0,
EMODnet Bathymetry Consortium, "DO NOT USE FOR NAVIGATION"). As derivative
databases they stay openly available under the ODbL. Each archive's own
`sources`, `attribution` and `not_for_navigation` metadata names its inputs;
[docs/chart-pack-format.md](docs/chart-pack-format.md#8-sources-and-licences)
has the table. They are a picture for a crew, not a navigation chart.

## Terms read on 12 November 2026

The owner confirmed the fifteen sources below as redistributable on 12 November
2026. For fourteen of them a page of the provider or of the source's registry was
read that day; for Météo-France AROME no current provider page naming the
licence was found, so the owner's decision is the only basis and `verified_on`
stays empty. For GSHHG and Open Waters Seamap the owner decided with open points
that the register's `notes` name. All are recorded in
[`contracts/data-sources.json`](contracts/data-sources.json). The conditions
(credits, disclaimers, modification notices) are obligations on what is
published, not only register fields. The full reading, with its caveats, is
Passage's
[licence verification proposals](https://github.com/deepregatta/passage/blob/main/docs/maintenance/2026-11-12-licence-verification-proposals.md).

| Source (register id) | Licence | Page read |
|---|---|---|
| NOAA GFS (`noaa-gfs`) | US Government work, public domain | https://registry.opendata.aws/noaa-gfs-bdp-pds |
| NOAA GFS-Wave (`noaa-gfs-wave`), by inference: no page names the wave files | US Government work, public domain | https://registry.opendata.aws/noaa-gfs-bdp-pds |
| NOAA GEFS (`noaa-gefs`) | US Government work, public domain | https://registry.opendata.aws/noaa-gefs/ |
| ECMWF open data IFS (`ecmwf-open-ifs`) | CC BY 4.0 plus ECMWF Terms of Use for open data | https://www.ecmwf.int/en/forecasts/datasets/open-data |
| Copernicus Marine GLO12 (`cmems-glo12`) | Copernicus Marine Service licence | https://marine.copernicus.eu/user-corner/service-commitments-and-licence |
| Copernicus Marine IBI (`cmems-ibi`) | Copernicus Marine Service licence | https://marine.copernicus.eu/user-corner/service-commitments-and-licence |
| DWD ICON-EU (`dwd-icon-eu`), reaching us through Open-Meteo's bulk files, whose terms are not yet confirmed | CC BY 4.0 | https://www.dwd.de/EN/service/legal_notice/legal_notice.html |
| EMODnet Digital Bathymetry (`emodnet-bathymetry`) | CC BY 4.0 | https://emodnet.ec.europa.eu/en/terms-use-emodnet-online-services-data-and-data-products |
| SHOM Balisage (`shom-balisage`) | Licence Ouverte 2.0 | https://www.data.gouv.fr/datasets/balisage-maritime-aides-a-la-navigation/informations |
| Met Office UKV (`metoffice-ukv`), via Open-Meteo's bulk files; the owner confirmed CC BY-SA 4.0 as the licence that applies, so derived tiles and GRIB exports carry the credit, the licence link, a modification notice and the BY-SA label (see the UKV section above) | CC BY-SA 4.0 | https://registry.opendata.aws/met-office-uk-deterministic/ |
| OpenStreetMap (`openstreetmap`) | ODbL 1.0 | https://www.openstreetmap.org/copyright |
| Protomaps (`protomaps-basemap`) | Tiles ODbL, code BSD-3-Clause, styles CC0 | https://github.com/protomaps/basemaps |
| Météo-France AROME (`meteofrance-arome`), via Open-Meteo's bulk files; owner's decision, no provider page read | CC BY 4.0 (Open-Meteo bulk catalogue); Météo-France open data | none |
| GSHHG (`gshhg`); LGPL since 2.2.2, the NOAA archive copy is labelled CC0 1.0, and what LGPL reaches for a derived land index is not settled | LGPL-3.0-or-later, with permission to use, copy, modify and distribute given attribution | https://www.ncei.noaa.gov/products/shoreline-coastline-resources |
| Open Waters Seamap (`open-waters-seamap`), identified with medium confidence; its bathymetry source's terms were not read | CC BY 4.0, being corrected to ODbL | https://openwaters.io/charts/seamap/ |

The Copernicus Marine licence asks licensees to keep records that trace product
use, and that duty passes into every downstream licence.
