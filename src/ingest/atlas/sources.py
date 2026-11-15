"""SHOM's 2D tidal-current atlases as NetCDF4, and which grids a pack carries.

Read on data.gouv.fr (SHOM's own listings, organisation "Shom") and SHOM's
download service on 2026-11-15; every archive downloaded that day from
services.data.shom.fr and its files' attributes read (docs/tidal-atlas-format.md
§7). Licence Ouverte 2.0; the credit and the supplement-only condition are
SHOM's own words from each pack's metadata.

A pack carries each zone's whole-zone grid. The port insets (250 m, 100 m,
30 m) are harbour scale, and Le Havre's is timed from low water; they are
left out.
"""

from __future__ import annotations

from dataclasses import dataclass

ATTRIBUTION = (
    "Shom, 2026. Atlas de courants de marée 2D au format netcdf, "
    "https://dx.doi.org/10.17183/ATLASCOURANTS2D_NETCDF"
)
LIMITATION = "A n'utiliser qu'en complément des cartes et ouvrages nautiques officiels."
LICENCE = "Licence Ouverte 2.0"
LICENCE_URL = "https://www.etalab.gouv.fr/licence-ouverte-open-licence"
DOWNLOAD = (
    "https://services.data.shom.fr/INSPIRE/telechargement/prepackageGroup/"
    "ATLASCOURANTS2D_NETCDF_{zone}_PACK_DL/prepackage/ATLASCOURANTS2D_NETCDF_{zone}/file/"
    "ATLASCOURANTS2D_NETCDF_{zone}.7z"
)


@dataclass(frozen=True)
class Grid:
    id: str
    zone: str
    title: str
    archive: str  # SHOM's zone key in the download URL
    archive_sha256: str
    file: str
    file_sha256: str
    resolution_m: int
    port: str
    shom_code: str

    @property
    def url(self) -> str:
        return DOWNLOAD.format(zone=self.archive)


# Finest first: a point takes the first grid that has water there.
GRIDS = (
    Grid(
        "golfe-normand-breton",
        "Golfe normand-breton",
        "Golfe normand-breton, Cherbourg to Trégastel and the Channel Islands",
        "GOLFE_NORMAND_BRETON",
        "82596b8637c51e540176b27a240b0beb696d7bf334de4ec1667660f6b3a64ead",
        "ATLASDYN-SURF_TELEMAC2D_R1123000_GOLFE-NORM-BRET-562-500M-1h.nc",
        "6fac0872f52d9705c2959e4d1ef88787a63acba1b5e921106d8cb98d615fd44f",
        500,
        "SAINT-MALO",
        "0410",
    ),
    Grid(
        "bretagne-nord",
        "Bretagne Nord",
        "Bretagne Nord, Trégastel to Porsguen",
        "BRETAGNE_NORD",
        "cbe02c1ba1fb37f21e4fffc328389206290519bf0ae284a83bcdd2598be53822",
        "ATLASDYN-SURF_TELEMAC2D_R1124000_ABERS-563-500M-30min.nc",
        "bb5fe502596e14102895f57646606e2f0f0a1a28debca03b846506a8e1fedd50",
        500,
        "ROSCOFF",
        "0054",
    ),
    Grid(
        "finistere",
        "Finistère",
        "Finistère, Porsguen to south of Penmarc'h, the Iroise and the Raz de Sein",
        "FINISTERE",
        "aba79c64069acc38867392bcfe15a58f5b3ff094185c18526a8accc259d6d17a",
        "ATLASDYN-SURF_TELEMAC3D-SURF_R1111000_FINISTERE-560-500M-5min.nc",
        "49205069fe8636440ffed0debeff2e7a037da5bf1751ef5633d8137f1da3a36e",
        500,
        "BREST",
        "0003",
    ),
    Grid(
        "baie-de-seine",
        "Baie de Seine",
        "Baie de Seine, Cherbourg to Fécamp",
        "BAIE_DE_SEINE",
        "54d04ad130a1b03895dd83144bd6e798a63d74241575cd50a1d8707e8a4b49dc",
        "ATLASDYN-SURF_TELEMAC3D-SURF_R1122000_BAIE-DE-SEINE-561-500M-5min.nc",
        "641c4ef6eb282672736c4f5f7b1acaad206ce668f6c4ce072cf8088d6e83af99",
        500,
        "CHERBOURG",
        "0013",
    ),
    Grid(
        "pas-de-calais",
        "Pas de Calais",
        "Pas de Calais, Dunkirk to Saint-Valery-en-Caux",
        "PAS_DE_CALAIS",
        "4810d4a32712722fc79de80f8566af4f77de04c028417b3847b9fa9d0b408fb5",
        "ATLASDYN-SURF_TELEMAC3D-SURF_R1121000_PAS-DE-CALAIS-557-500M-5min.nc",
        "764ac167cfeb27c822e4389f3c2f7d68fef80d214579cd999edc2c1a17562520",
        500,
        "CALAIS",
        "0055",
    ),
    Grid(
        "manche",
        "Manche",
        "Manche, Dunkirk to Brest",
        "MANCHE",
        "d1c7d36004268d9826dba9aed7a3f5d720e5784e47fcb5bda0b4b785207c6e9b",
        "ATLASDYN-SURF_TELEMAC3D-SURF_R1120000_MANCHE-564-2KM-5min.nc",
        "7718a8478a71f808aac1eda06683845ba1d2f6b82e342afc0417784b5e88d735",
        2000,
        "CHERBOURG",
        "0013",
    ),
    Grid(
        "gascogne",
        "Gascogne",
        "Golfe de Gascogne, Brittany to the north coast of Spain",
        "GASCOGNE",
        "df2b21c57086808969ff2acb687da9e78fb832357e812b04695fa7bd4ea92ff6",
        "ATLASDYN-SURF_TELEMAC2D_R1110000_GASCOGNE-565-2KM-30min.nc",
        "8f83cc8585403df2d38c936e230128adf0cc4b34b3657dbd4693ad0cacf1f4ec",
        2000,
        "CONCARNEAU",
        "0160",
    ),
)

BY_ID = {grid.id: grid for grid in GRIDS}
