#!/usr/bin/env python3
"""Construit les cartes et prévisions ARPEGE 0,1° (grille EURAT01) pour la France et l'Europe.

La chaîne lit directement les paquets GRIB2 ouverts de Météo-France publiés
sur data.gouv.fr. Les fichiers nationaux sont découpés par département pour le
module WordPress, tandis que les cartes restent calculées depuis la grille
ARPEGE native (EURAT01, 0,1°) et non depuis les seules coordonnées des communes.
Contrairement à AROME (aire limitée, ~1,3 km), ARPEGE est le modèle global de
Météo-France : résolution plus grossière (~10 km sur l'Europe) mais échéances
plus longues (jusqu'à +102 h contre +51 h pour AROME).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import requests
from eccodes import (
    codes_get,
    codes_get_double_array,
    codes_get_double_elements,
    codes_grib_new_from_file,
    codes_new_from_message,
    codes_release,
)
from scipy.ndimage import map_coordinates

from arpege_maps import DEFAULT_BOUNDS, ArpegeMapRenderer

try:
    from meteofrance_wcs_client import (
        MeteoFranceWCSAuthError,
        MeteoFranceWCSError,
    )
    from arpege_api_source import (
        ARPEGE_API_PRECISION_DEGREES,
        ARPEGE_API_TERRITORY,
        GRAVITY as API_GRAVITY,
        ApiRunResult,
        fetch_arpege_api_run,
    )

    API_SOURCE_AVAILABLE = True
except ImportError:  # pragma: no cover - modules toujours livrés ensemble
    API_SOURCE_AVAILABLE = False


LOGGER = logging.getLogger("arpege.france")
PIPELINE_VERSION = "1.3.1"
GRAVITY_MS2 = 9.80665
# Niveaux isobares réellement présents dans le paquet IP1 (100 à 1000 hPa,
# vérifié le 08/09/2026 sur un fichier réel) : on ne retient que ceux utiles
# à l'onglet Tempête / haute altitude du comparateur (850-500 hPa) plus
# 1000 hPa, nécessaire au calcul de l'épaisseur 1000-500.
ISOBARIC_LEVELS_HPA = (1000, 850, 800, 700, 600, 500)
# v1.3.1 : IP1 publie aussi le vent (u/v) à ces mêmes niveaux, dans le même
# fichier déjà téléchargé — aucun coût réseau supplémentaire. Sert au vent
# d'altitude du mode Tempête et au cisaillement 0-500 hPa (proxy standard du
# cisaillement profond 0-6 km, cf. buildStormTable côté comparateur).
ISOBARIC_POINT_ONLY_FIELDS = frozenset(
    f"{prefix}_{level}_{suffix}"
    for prefix, suffix in (
        ("temperature", "k"),
        ("geopotential", "gpm"),
        ("wind_u", "ms"),
        ("wind_v", "ms"),
    )
    for level in ISOBARIC_LEVELS_HPA
)
DATASET_API = (
    "https://www.data.gouv.fr/api/1/datasets/"
    "paquets-arpege-resolution-0-1deg/"
)
DATASET_PAGE = "https://www.data.gouv.fr/datasets/paquets-arpege-resolution-0-1deg"
DEFAULT_CURRENT_METADATA_URL = (
    "https://raw.githubusercontent.com/alertesmeteo-hub/"
    "arpege-meteo-france/data/index.json"
)
USER_AGENT = "alertes-meteo.com/arpege-meteofrance-france/1.0"
METEOFRANCE_API_KEY_ENV = "METEOFRANCE_API_KEY"


@dataclass(frozen=True)
class GridSpec:
    """Décrit une grille régulière lat/lon (Ni×Nj points, pas constant).

    Généralisé en v1.2.0 pour permettre à la source API officielle
    Météo-France (ARPEGE-005-EURAT, 0,05°) de fonctionner à côté de la grille
    historique EURAT01 0,1° de data.gouv.fr, sans dupliquer toute la logique
    d'extraction/interpolation.
    """

    ni: int
    nj: int
    lat_first: float
    lon_first: float
    step: float


# Grille EURAT01 (domaine Euro-Atlantique, 0,1°) documentée par Météo-France :
# 20°N-72°N, -32°E-42°E. La cohérence avec les en-têtes GRIB2 réels est
# vérifiée à chaque téléchargement (cf. NationalGrid.validate plus bas) ;
# c'est la grille utilisée par la source historique data.gouv.fr (fallback).
LEGACY_GRID = GridSpec(ni=741, nj=521, lat_first=72.0, lon_first=-32.0, step=0.1)
# Alias conservés pour compatibilité (tests, scripts externes éventuels).
ARPEGE_NI = LEGACY_GRID.ni
ARPEGE_NJ = LEGACY_GRID.nj
ARPEGE_LAT_FIRST = LEGACY_GRID.lat_first
ARPEGE_LON_FIRST = LEGACY_GRID.lon_first
ARPEGE_STEP = LEGACY_GRID.step

MAP_WIDTH = 2200
MAP_HEIGHT = 1640

# Format compact partagé avec le JavaScript. Les diagnostics explicitement
# dérivés sont conservés car ils servent aux tableaux orages et neige.
VALUE_COLUMNS = (
    "temperature_c",
    "humidity_pct",
    "precipitation_mm",
    "cloud_cover_pct",
    "wind_speed_kmh",
    "wind_direction_deg",
    "wind_gust_kmh",
    "pressure_hpa",
    "condition_code",
    "pressure_surface_hpa",
    "dewpoint_c",
    "precipitation_total_mm",
    "cloud_low_pct",
    "cloud_mid_pct",
    "cloud_high_pct",
    "cape_jkg",
    "thunder_risk_code",
    "lcl_m",
    "lightning_score",
    "hail_risk_code",
    "convective_precipitation_mm",
    "storm_type_code",
    "snow_risk_code",
    "snowfall_mm",
    "snow_fresh_cm",
    "snow_depth_cm",
    "snow_water_equivalent_mm",
    "snow_stick_risk_code",
    "snow_phase_code",
    "snowfall_total_mm",
    # Champs ajoutés (v1.1.0) : réellement présents dans les paquets SP1/SP2
    # d'après le descriptif technique Météo-France (TSURF, H_COULIM,
    # COLONNE_VAPO, TMIN/TMAX(2m), FLSEN, FLLAT, FLSOLAIRE_D, FLTHERM_D).
    "surface_temperature_c",
    "boundary_layer_height_m",
    "precipitable_water_mm",
    "temperature_min_2m_c",
    "temperature_max_2m_c",
    "sensible_heat_mjm2",
    "latent_heat_mjm2",
    "solar_radiation_down_wm2",
    "thermal_radiation_down_wm2",
    # Champs d'altitude ajoutés v1.2.0 (source API Météo-France uniquement ;
    # restent null pour tout run produit depuis le fallback data.gouv.fr).
    "temperature_850_c",
    "temperature_500_c",
    "temperature_300_c",
    "wind_speed_850_kmh",
    "wind_speed_500_kmh",
    "wind_speed_300_kmh",
    "wind_speed_100m_kmh",
    "humidity_850_pct",
    "humidity_500_pct",
    "geopotential_850_m",
    "geopotential_500_m",
    "vertical_velocity_500_pas",
    "cin_jkg",
    # Champs isobares ajoutés v1.3.0, extraits directement du paquet IP1
    # (data.gouv.fr, gratuit) : contrairement aux champs ci-dessus, ceux-là
    # sont bien renseignés sur la source de secours (pas seulement via
    # l'API Météo-France payante). Cf. message_field() plus bas.
    "temperature_800_c",
    "temperature_700_c",
    "temperature_600_c",
    "geopotential_800_m",
    "geopotential_700_m",
    "geopotential_600_m",
    "thickness_1000_500_dam",
    "freezing_level_m",
    # v1.3.1 : vent isobare (même paquet IP1, cf. plus haut).
    "wind_speed_800_kmh",
    "wind_speed_700_kmh",
    "wind_speed_600_kmh",
    "shear_0_6_ms",
)

INTEGER_COLUMNS = {
    "humidity_pct",
    "cloud_cover_pct",
    "wind_speed_kmh",
    "wind_direction_deg",
    "wind_gust_kmh",
    "pressure_hpa",
    "condition_code",
    "pressure_surface_hpa",
    "cloud_low_pct",
    "cloud_mid_pct",
    "cloud_high_pct",
    "cape_jkg",
    "thunder_risk_code",
    "lcl_m",
    "lightning_score",
    "hail_risk_code",
    "storm_type_code",
    "snow_risk_code",
    "snow_stick_risk_code",
    "snow_phase_code",
    "boundary_layer_height_m",
    "wind_speed_850_kmh",
    "wind_speed_500_kmh",
    "wind_speed_300_kmh",
    "wind_speed_100m_kmh",
    "humidity_850_pct",
    "humidity_500_pct",
    "geopotential_850_m",
    "geopotential_500_m",
    "cin_jkg",
    "geopotential_800_m",
    "geopotential_700_m",
    "geopotential_600_m",
    "thickness_1000_500_dam",
    "freezing_level_m",
    "wind_speed_800_kmh",
    "wind_speed_700_kmh",
    "wind_speed_600_kmh",
}

MAP_FIELDS = {
    "temperature_c",
    "wind_chill_c",
    "wet_bulb_c",
    "dewpoint_c",
    "humidex",
    "humidity_pct",
    "precipitation_mm",
    "precipitation_total_mm",
    "snow_mm",
    "snow_water_equivalent_mm",
    "snow_depth_cm",
    "wind_speed_kmh",
    "wind_gust_kmh",
    "pressure_hpa",
    "surface_pressure_hpa",
    "cloud_cover_pct",
    "cloud_low_pct",
    "cloud_mid_pct",
    "cloud_high_pct",
    "cape_jkg",
    "hail_risk_code",
    "storm_type_code",
    "altitude_m",
    "surface_temperature_c",
    "mixed_layer_depth_m",
    "precipitable_water_mm",
    "temperature_min_2m_c",
    "temperature_max_2m_c",
    "sensible_heat_mjm2",
    "latent_heat_mjm2",
    "solar_radiation_down_wm2",
    "thermal_radiation_down_wm2",
    "temperature_850_c",
    "temperature_500_c",
    "temperature_300_c",
    "wind_speed_850_kmh",
    "wind_speed_500_kmh",
    "wind_speed_300_kmh",
    "wind_speed_100m_kmh",
    "humidity_850_pct",
    "humidity_500_pct",
    "geopotential_850_m",
    "geopotential_500_m",
    "vertical_velocity_500_pas",
    "cin_jkg",
}

CONDITION_CODES = {
    0: "unknown",
    1: "clear",
    2: "partly_cloudy",
    3: "cloudy",
    4: "overcast",
    5: "rain",
    6: "heavy_rain",
    7: "snow",
    8: "fog",
    9: "windy",
}

# Les paquets ARPEGE réels ne sont pas un fichier par échéance : chaque
# paquet couvre une PLAGE d'échéances (ex. 000H012H = +0 h à +12 h, avec un
# pas d'1 h jusqu'à +48 h puis 3 h au-delà) et regroupe plusieurs paramètres
# GRIB2 dans un seul fichier. Vérifié le 2026-09-04 contre le vrai catalogue
# data.gouv.fr : le nom réel est
# ``arpege__01__SP1__000H012H__2026-09-04T12:00:00Z.grib2`` (et non un
# format ``__NNH__`` par échéance unique comme initialement supposé).
RESOURCE_RE = re.compile(
    r"^arpege__01__(?P<group>SP1|SP2|HP1|HP2|IP1|IP2|IP3|IP4)__"
    r"(?P<lead_start>\d{3})H(?P<lead_end>\d{3})H__(?P<run>.+)\.grib2$",
    re.IGNORECASE,
)
LOCAL_RESOURCE_RE = re.compile(
    r"(?P<group>SP1|SP2|HP1|HP2|IP1|IP2|IP3|IP4)[^0-9]*"
    r"(?P<lead_start>\d{3})H(?P<lead_end>\d{3})H",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Resource:
    group: str
    lead_start: int
    lead_end: int
    run_text: str | None
    title: str
    url: str | None
    size: int | None
    local_path: Path | None = None


class IncompleteRunError(RuntimeError):
    """Le catalogue distant ne contient pas encore un run ARPEGE cohérent."""


@dataclass
class DepartmentData:
    code: str
    global_point_ids: np.ndarray
    points: list[list[Any]]
    communes: list[list[Any]]


@dataclass
class NationalCatalog:
    version: str
    model_indexes: list[int]
    point_latitudes: np.ndarray
    point_longitudes: np.ndarray
    point_departments: list[str]
    departments: dict[str, DepartmentData]
    commune_count: int
    # Coordonnées communales brutes (non calées sur la grille ARPEGE 0,1°),
    # utilisées uniquement pour tracer le contour des départements : à ~10 km
    # de résolution, un tracé basé sur les points de grille produit un rendu
    # "vitrail" très anguleux. La densité communale (~35 000 points) donne un
    # tracé beaucoup plus fidèle, indépendant de la résolution du modèle.
    commune_latitudes: np.ndarray
    commune_longitudes: np.ndarray
    commune_departments: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        default="config/communes-france.json",
        help="Catalogue officiel des communes de France",
    )
    parser.add_argument(
        "--output-dir",
        default="build/arpege-national",
        help="Dossier de publication à produire",
    )
    parser.add_argument(
        "--forecast-hours",
        type=int,
        default=72,
        help=(
            "Dernière échéance, entre 1 et 102 heures (vérifié le 2026-09-05 "
            "contre le \"Descriptif technique des paquets de données modèle "
            "ARPEGE\" de Météo-France, v. 02/01/2024 : la grille EURAT01 "
            "0,1° est publiée en 9 tranches 00-12,...,97-102 et ne va pas "
            "au-delà de +102 h, quel que soit le réseau 00/06/12/18 UTC — "
            "il n'existe pas de tranche 102H-114H)"
        ),
    )
    parser.add_argument(
        "--resource-directory",
        help="Dossier local de GRIB2 SP1/SP2/HP1 pour les tests hors ligne",
    )
    parser.add_argument(
        "--current-metadata-url",
        default=DEFAULT_CURRENT_METADATA_URL,
        help="index.json actuellement publié, pour éviter un run identique",
    )
    parser.add_argument(
        "--catalog-attempts",
        type=int,
        default=4,
        help=(
            "Nombre de lectures du catalogue data.gouv.fr lorsqu'un run est "
            "en cours de remplacement (défaut : 4)"
        ),
    )
    parser.add_argument(
        "--catalog-retry-seconds",
        type=int,
        default=60,
        help="Attente entre deux lectures du catalogue incomplet (défaut : 60 s)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force la reconstruction même si ce run est déjà publié",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "api", "legacy"),
        default="auto",
        help=(
            "Source des données : 'api' force l'API officielle Météo-France "
            "(nécessite METEOFRANCE_API_KEY, échoue si indisponible), "
            "'legacy' force le scraping data.gouv.fr historique, 'auto' "
            "(défaut) utilise l'API si une clé est configurée et retombe "
            "automatiquement sur data.gouv.fr en cas d'échec"
        ),
    )
    return parser.parse_args()


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_run_text(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def safe_get(gid: int, key: str, default: Any = None) -> Any:
    try:
        return codes_get(gid, key)
    except Exception:
        return default


def grib_datetime(gid: int, date_key: str, time_key: str) -> datetime | None:
    date_value = safe_get(gid, date_key)
    time_value = safe_get(gid, time_key)
    if date_value is None or time_value is None:
        return None
    try:
        return datetime.strptime(
            f"{int(date_value):08d}{int(time_value):04d}", "%Y%m%d%H%M"
        ).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def grid_index(
    latitude: float, longitude: float, grid: GridSpec = LEGACY_GRID
) -> tuple[int, float, float]:
    row = int(round((grid.lat_first - latitude) / grid.step))
    column = int(round((longitude - grid.lon_first) / grid.step))
    row = max(0, min(grid.nj - 1, row))
    column = max(0, min(grid.ni - 1, column))
    index = row * grid.ni + column
    model_latitude = grid.lat_first - row * grid.step
    model_longitude = grid.lon_first + column * grid.step
    return index, model_latitude, model_longitude


def load_catalog(path: Path, grid: GridSpec = LEGACY_GRID) -> NationalCatalog:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    raw_communes = payload.get("communes") or []
    if len(raw_communes) < 34_000:
        raise RuntimeError("Le catalogue communal France est incomplet")

    mapped: list[tuple[list[Any], int, float, float]] = []
    point_coordinates: dict[int, tuple[float, float]] = {}
    for commune in raw_communes:
        if not isinstance(commune, list) or len(commune) < 7:
            raise RuntimeError("Entrée communale invalide dans le catalogue")
        latitude = float(commune[5])
        longitude = float(commune[6])
        model_index, model_latitude, model_longitude = grid_index(
            latitude, longitude, grid
        )
        mapped.append((commune, model_index, model_latitude, model_longitude))
        point_coordinates[model_index] = (model_latitude, model_longitude)

    model_indexes = sorted(point_coordinates)
    global_identifier = {
        model_index: position for position, model_index in enumerate(model_indexes)
    }
    point_latitudes = np.asarray(
        [point_coordinates[index][0] for index in model_indexes], dtype=np.float64
    )
    point_longitudes = np.asarray(
        [point_coordinates[index][1] for index in model_indexes], dtype=np.float64
    )

    department_votes: dict[int, Counter[str]] = defaultdict(Counter)
    by_department: dict[str, list[tuple[list[Any], int]]] = defaultdict(list)
    for commune, model_index, _latitude, _longitude in mapped:
        department = str(commune[2]).upper()
        global_id = global_identifier[model_index]
        department_votes[global_id][department] += 1
        by_department[department].append((commune, global_id))

    point_departments = [
        department_votes[position].most_common(1)[0][0]
        if department_votes[position]
        else ""
        for position in range(len(model_indexes))
    ]

    departments: dict[str, DepartmentData] = {}
    for department, entries in sorted(by_department.items()):
        global_ids = sorted({global_id for _commune, global_id in entries})
        local_identifier = {
            global_id: position for position, global_id in enumerate(global_ids)
        }
        compact_communes = [
            [
                str(commune[0]),
                str(commune[1]),
                list(commune[3]),
                int(commune[4]),
                float(commune[5]),
                float(commune[6]),
                local_identifier[global_id],
            ]
            for commune, global_id in entries
        ]
        compact_points = [
            [
                model_indexes[global_id],
                round(float(point_latitudes[global_id]), 5),
                round(float(point_longitudes[global_id]), 5),
            ]
            for global_id in global_ids
        ]
        departments[department] = DepartmentData(
            code=department,
            global_point_ids=np.asarray(global_ids, dtype=np.int64),
            points=compact_points,
            communes=compact_communes,
        )

    if len(departments) != 96:
        raise RuntimeError(
            f"Nombre inattendu de départements métropolitains : {len(departments)}"
        )
    LOGGER.info(
        "Catalogue ARPEGE : %s communes, %s points 1,3 km, %s départements",
        len(raw_communes),
        len(model_indexes),
        len(departments),
    )
    commune_latitudes = np.asarray(
        [float(commune[5]) for commune in raw_communes], dtype=np.float64
    )
    commune_longitudes = np.asarray(
        [float(commune[6]) for commune in raw_communes], dtype=np.float64
    )
    commune_departments = [str(commune[2]).upper() for commune in raw_communes]

    return NationalCatalog(
        version=f"{payload.get('catalog_version', '1')}-arpege001",
        model_indexes=model_indexes,
        point_latitudes=point_latitudes,
        point_longitudes=point_longitudes,
        point_departments=point_departments,
        departments=departments,
        commune_count=len(raw_communes),
        commune_latitudes=commune_latitudes,
        commune_longitudes=commune_longitudes,
        commune_departments=commune_departments,
    )


def api_resources(session: requests.Session) -> list[Resource]:
    response = session.get(
        DATASET_API,
        params={"_": int(time.time())},
        headers={"Cache-Control": "no-cache"},
        timeout=(15, 90),
    )
    response.raise_for_status()
    payload = response.json()
    resources: list[Resource] = []
    for item in payload.get("resources") or []:
        title = str(item.get("title") or "")
        match = RESOURCE_RE.match(title)
        if not match:
            continue
        resources.append(
            Resource(
                group=match.group("group").upper(),
                lead_start=int(match.group("lead_start")),
                lead_end=int(match.group("lead_end")),
                run_text=match.group("run"),
                title=title,
                url=str(item.get("url") or ""),
                size=int(item.get("filesize") or 0) or None,
            )
        )
    if not resources:
        raise RuntimeError("Aucune ressource ARPEGE 0,1° trouvée sur data.gouv.fr")
    return resources


def local_resources(directory: Path) -> list[Resource]:
    resources: list[Resource] = []
    for path in sorted(directory.glob("*.grib2")):
        match = RESOURCE_RE.match(path.name) or LOCAL_RESOURCE_RE.search(path.name)
        if not match:
            continue
        resources.append(
            Resource(
                group=match.group("group").upper(),
                lead_start=int(match.group("lead_start")),
                lead_end=int(match.group("lead_end")),
                run_text=(match.groupdict().get("run") if "run" in match.groupdict() else None),
                title=path.name,
                url=None,
                size=path.stat().st_size,
                local_path=path.resolve(),
            )
        )
    if not resources:
        raise RuntimeError(f"Aucun GRIB2 ARPEGE reconnu dans {directory}")
    return resources


def _covers_range(spans: list[tuple[int, int]], upto: int) -> bool:
    """Vérifie qu'une liste de plages [debut, fin] couvre 0..upto sans trou."""

    covered = -1
    for lead_start, lead_end in sorted(spans):
        if lead_start > covered + 1:
            return covered >= upto
        covered = max(covered, lead_end)
        if covered >= upto:
            return True
    return covered >= upto


def choose_resources(
    resources: Iterable[Resource], forecast_hours: int
) -> tuple[dict[tuple[str, int, int], Resource], datetime | None]:
    resources = list(resources)
    grouped: dict[str, dict[tuple[str, int, int], Resource]] = defaultdict(dict)
    for resource in resources:
        grouped[resource.run_text or "local"][
            resource.group, resource.lead_start, resource.lead_end
        ] = resource

    # SP1/SP2 doivent, à eux deux par famille, couvrir 0..forecast_hours sans
    # trou. L'altitude (message "h") est incluse directement dans les paquets
    # SP2 à +00 h : aucun paquet HP1 séparé n'est nécessaire (vérifié
    # 2026-09-04 : HP1 000H012H pèse ~740 Mo pour un champ déjà disponible en
    # ~700 Ko dans SP2).
    candidates: list[tuple[datetime, str, dict[tuple[str, int, int], Resource]]] = []
    for run_text, selection in grouped.items():
        sp1_spans = [(ls, le) for (g, ls, le) in selection if g == "SP1"]
        sp2_spans = [(ls, le) for (g, ls, le) in selection if g == "SP2"]
        if not sp1_spans or not sp2_spans:
            continue
        if not (
            _covers_range(sp1_spans, forecast_hours)
            and _covers_range(sp2_spans, forecast_hours)
        ):
            continue
        parsed = parse_run_text(None if run_text == "local" else run_text)
        candidates.append((parsed or datetime.min.replace(tzinfo=timezone.utc), run_text, selection))
    if not candidates:
        inventories: list[str] = []
        for run_text in sorted(grouped):
            counts = Counter(group for group, _ls, _le in grouped[run_text])
            inventories.append(
                f"{run_text}: "
                + ", ".join(
                    f"{group}={counts.get(group, 0)}"
                    for group in ("SP1", "SP2", "HP1", "IP1")
                )
            )
        raise IncompleteRunError(
            "Catalogue ARPEGE en cours de synchronisation : aucun run unique ne "
            f"contient SP1/SP2 couvrant +00 h à +{forecast_hours:02d} h "
            f"(par run : {'; '.join(inventories) or 'aucune ressource'})"
        )
    _date, run_text, selection = max(candidates, key=lambda item: item[0])
    chosen = {
        key: resource
        for key, resource in selection.items()
        if key[0] in ("SP1", "SP2") and key[1] <= forecast_hours
    }
    # IP1 (niveaux isobares, v1.3.0) est ajouté en best-effort : contrairement
    # à SP1/SP2, son absence ou son incomplétude temporaire ne doit pas faire
    # échouer tout le run — les champs d'altitude resteront simplement à null
    # pour cette publication (cf. message_field/transform_step), comme avant
    # v1.3.0.
    ip1_spans = [(ls, le) for (g, ls, le) in selection if g == "IP1"]
    if _covers_range(ip1_spans, forecast_hours):
        chosen.update(
            {
                key: resource
                for key, resource in selection.items()
                if key[0] == "IP1" and key[1] <= forecast_hours
            }
        )
    else:
        LOGGER.warning(
            "Paquets IP1 (niveaux de pression) incomplets ou absents pour le "
            "run %s : température/géopotentiel 850-500 hPa resteront "
            "indisponibles pour cette publication.",
            run_text,
        )
    return chosen, parse_run_text(None if run_text == "local" else run_text)


def wait_for_complete_remote_run(
    session: requests.Session,
    forecast_hours: int,
    attempts: int,
    retry_seconds: int,
) -> tuple[dict[tuple[str, int], Resource], datetime | None] | None:
    """Attend la fin du remplacement SP1/SP2/HP1 effectué par data.gouv.fr.

    Météo-France remplace parfois les quatre familles l'une après l'autre. Dans
    cette courte fenêtre, chaque famille compte bien 52 fichiers, mais leurs
    horodatages de run diffèrent et elles ne doivent surtout pas être mélangées.
    """

    last_error: IncompleteRunError | None = None
    for attempt in range(1, attempts + 1):
        discovered = api_resources(session)
        try:
            return choose_resources(discovered, forecast_hours)
        except IncompleteRunError as exc:
            last_error = exc
            if attempt < attempts:
                LOGGER.warning(
                    "%s. Nouvelle vérification dans %s s (%s/%s).",
                    exc,
                    retry_seconds,
                    attempt,
                    attempts,
                )
                if retry_seconds:
                    time.sleep(retry_seconds)

    LOGGER.warning(
        "%s. Aucune donnée ne sera écrasée ; le prochain passage du workflow "
        "réessaiera automatiquement. Aucune clé API Météo-France n'est requise.",
        last_error,
    )
    return None


def already_published(url: str, run_time: datetime | None) -> bool:
    if not url or run_time is None:
        return False
    try:
        response = requests.get(
            url,
            timeout=(10, 30),
            headers={"User-Agent": USER_AGENT},
        )
        if response.status_code != 200:
            return False
        payload = response.json()
        model = payload.get("model") or {}
        return (
            payload.get("status") == "ok"
            and model.get("run_time") == iso_utc(run_time)
            and model.get("pipeline_version") == PIPELINE_VERSION
        )
    except (requests.RequestException, ValueError, TypeError):
        return False


def download_resource(
    session: requests.Session, resource: Resource, destination: Path
) -> None:
    if resource.local_path is not None:
        shutil.copy2(resource.local_path, destination)
        return
    if not resource.url:
        raise RuntimeError(f"Adresse de téléchargement absente : {resource.title}")
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with session.get(
                resource.url,
                stream=True,
                timeout=(20, 180),
                headers={"User-Agent": USER_AGENT},
            ) as response:
                response.raise_for_status()
                with destination.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=2 * 1024 * 1024):
                        if chunk:
                            handle.write(chunk)
            if resource.size and destination.stat().st_size != resource.size:
                raise RuntimeError(
                    f"Taille inattendue pour {resource.title} : "
                    f"{destination.stat().st_size} au lieu de {resource.size}"
                )
            return
        except (requests.RequestException, OSError, RuntimeError) as error:
            last_error = error
            destination.unlink(missing_ok=True)
            if attempt < 3:
                LOGGER.warning(
                    "Téléchargement à retenter (%s/3) : %s", attempt, resource.title
                )
                time.sleep(2**attempt)
    raise RuntimeError(f"Téléchargement impossible : {resource.title}") from last_error


def mask_missing(values: np.ndarray, missing_value: Any) -> np.ndarray:
    result = np.asarray(values, dtype=np.float64)
    invalid = ~np.isfinite(result) | (np.abs(result) > 1.0e20)
    try:
        missing = float(missing_value)
    except (TypeError, ValueError):
        missing = math.nan
    if math.isfinite(missing):
        invalid |= np.isclose(result, missing, rtol=0.0, atol=1.0e-9)
    result[invalid] = np.nan
    return result


def message_field(gid: int) -> str | None:
    """Associe un message GRIB2 ARPEGE à un champ interne.

    ``reflectivity_dbz`` (RFLCTVT_MAX, discipline 0/catégorie 16/numéro 193)
    et le graupel (``tgrp``) ont été retirés le 2026-09-05 : le descriptif
    technique Météo-France des paquets ARPEGE (SP1/SP2, EURAT01 0,1°) ne les
    liste pas — ils étaient toujours absents des GRIB2 réels et restaient
    silencieusement à NaN. Cf. README pour le détail des paquets.

    v1.3.0 : reconnaît aussi les messages isobares du paquet IP1 (température
    ``t`` et géopotentiel ``z``, ``typeOfLevel == "isobaricInhPa"``) pour les
    niveaux listés dans ``ISOBARIC_LEVELS_HPA``. Vérifié le 08/09/2026 sur un
    fichier IP1 réel avec eccodes : ``t`` et ``z`` y existent bien de 100 à
    1000 hPa (contrairement à AROME 0,01°, qui ne publie aucun paquet
    isobare — seulement HP1, des niveaux d'altitude, pas de pression).
    """

    short_name = str(safe_get(gid, "shortName", ""))
    if short_name in ("t", "z", "u", "v") and str(safe_get(gid, "typeOfLevel", "")) == "isobaricInhPa":
        level = int(safe_get(gid, "level", -1))
        if level in ISOBARIC_LEVELS_HPA:
            if short_name == "t":
                return f"temperature_{level}_k"
            if short_name == "z":
                return f"geopotential_{level}_gpm"
            return f"wind_{short_name}_{level}_ms"
        return None
    direct = {
        "2t": "temperature_k",
        "2r": "humidity_pct",
        "10u": "wind_u_ms",
        "10v": "wind_v_ms",
        "max_10efg": "gust_u_ms",
        "max_10nfg": "gust_v_ms",
        "CAPE_INS": "cape_jkg",
        "sp": "surface_pressure_pa",
        "lcc": "cloud_low_pct",
        "mcc": "cloud_mid_pct",
        "hcc": "cloud_high_pct",
        "tirf": "precipitation_total_mm",
        "tsnowp": "snow_total_mm",
        "h": "altitude_m",
        # Ajoutés v1.1.0 : confirmés réellement présents dans SP1/SP2 par le
        # "Descriptif technique des paquets de données modèle ARPEGE"
        # (Météo-France, v. 02/01/2024) et par la table de shortNames locale
        # Météo-France (centre 84/85, cf. projet MeteoFetch).
        "TSURF": "surface_temperature_k",
        "H_COULIM": "boundary_layer_height_m",
        "COLONNE_VAPO": "precipitable_water_mm",
        "TMIN": "temperature_min_2m_k",
        "TMAX": "temperature_max_2m_k",
        "FLSEN": "sensible_heat_total",
        "FLLAT": "latent_heat_total",
        "FLSOLAIRE_D": "solar_radiation_down_wm2",
        "FLTHERM_D": "thermal_radiation_down_wm2",
    }
    if short_name in direct:
        return direct[short_name]
    return None


class NationalGrid:
    def __init__(self, catalog: NationalCatalog, grid: GridSpec = LEGACY_GRID) -> None:
        self.catalog = catalog
        self.grid = grid
        self.validated = False

    def validate(self, gid: int) -> None:
        if self.validated:
            return
        ni = int(safe_get(gid, "Ni", 0))
        nj = int(safe_get(gid, "Nj", 0))
        lat_first = float(safe_get(gid, "latitudeOfFirstGridPointInDegrees", 0))
        lon_first = float(safe_get(gid, "longitudeOfFirstGridPointInDegrees", 0))
        lon_first = (lon_first + 180.0) % 360.0 - 180.0
        if (
            ni != self.grid.ni
            or nj != self.grid.nj
            or not math.isclose(lat_first, self.grid.lat_first, abs_tol=1.0e-6)
            or not math.isclose(lon_first, self.grid.lon_first, abs_tol=1.0e-6)
        ):
            raise RuntimeError(
                "La grille reçue ne correspond pas à la grille attendue "
                f"({self.grid.ni} × {self.grid.nj}, premier point "
                f"{self.grid.lat_first}/{self.grid.lon_first}) : reçu "
                f"{ni} × {nj}, premier point {lat_first}/{lon_first}"
            )
        if max(self.catalog.model_indexes) >= ni * nj:
            raise RuntimeError("Un indice communal dépasse la grille ARPEGE")
        self.validated = True

    def extract(self, gid: int) -> np.ndarray:
        self.validate(gid)
        values = codes_get_double_elements(gid, "values", self.catalog.model_indexes)
        return mask_missing(values, safe_get(gid, "missingValue"))


def mercator(latitude: np.ndarray) -> np.ndarray:
    radians = np.radians(np.clip(latitude, -85.0, 85.0))
    return np.log(np.tan(np.pi / 4.0 + radians / 2.0))


def inverse_mercator(value: np.ndarray) -> np.ndarray:
    return np.degrees(2.0 * np.arctan(np.exp(value)) - np.pi / 2.0)


class MapSampler:
    """Rééchantillonne la grille régulière ARPEGE sur la carte Web Mercator."""

    def __init__(self, width: int, height: int, grid: GridSpec = LEGACY_GRID) -> None:
        self.width = int(width)
        self.height = int(height)
        self.grid = grid
        bounds = DEFAULT_BOUNDS
        target_latitudes = inverse_mercator(
            np.linspace(
                mercator(np.asarray(float(bounds["north"]))),
                mercator(np.asarray(float(bounds["south"]))),
                self.height,
            )
        )
        target_longitudes = np.linspace(
            float(bounds["west"]), float(bounds["east"]), self.width
        )
        rows = (grid.lat_first - target_latitudes) / grid.step
        columns = (target_longitudes - grid.lon_first) / grid.step
        self.row_grid = np.broadcast_to(rows[:, None], (self.height, self.width))
        self.column_grid = np.broadcast_to(
            columns[None, :], (self.height, self.width)
        )
        self.coverage = (
            (self.row_grid >= 0)
            & (self.row_grid <= grid.nj - 1)
            & (self.column_grid >= 0)
            & (self.column_grid <= grid.ni - 1)
        )

    def extract(self, gid: int, validator: NationalGrid) -> np.ndarray:
        validator.validate(gid)
        values = mask_missing(
            codes_get_double_array(gid, "values"),
            safe_get(gid, "missingValue"),
        ).reshape(self.grid.nj, self.grid.ni)
        sampled = map_coordinates(
            values,
            [self.row_grid, self.column_grid],
            order=1,
            mode="constant",
            cval=np.nan,
            prefilter=False,
        ).astype(np.float32, copy=False)
        sampled[~self.coverage] = np.nan
        return sampled


class MapValueSpill:
    """Décharge sur disque les champs cartographiques bruts par échéance.

    ``parse_grib_files``/``decode_api_run_result`` décodaient auparavant TOUS
    les champs cartographiques (déjà rééchantillonnés à la résolution de
    rendu, ~14 Mo par champ en float32) de TOUTES les échéances en mémoire
    avant même de commencer le rendu. L'ajout de 9 nouveaux champs SP1/SP2 et
    le passage de +72 h (73 échéances) à +102 h (103 échéances) dans la
    v1.1.0 a fait plus que doubler ce pic mémoire (~15 → ~22 champs × 73 → 103
    échéances), le faisant dépasser la RAM du runner GitHub Actions
    (~7 Go) : c'est la cause du "The operation was canceled" (OOM kill)
    observé juste après le début du rendu, pas le tracé des frontières
    communales (mesuré à ~230 Mo / 3 s, négligeable).

    Cette classe conserve le dict ``steps`` en mémoire (structure et champs
    ``values`` ponctuels, petits) mais écrit chaque tableau ``map_values`` sur
    disque dès son extraction et ne le recharge qu'au moment du rendu de son
    échéance, dans la boucle principale — le pic mémoire redevient de l'ordre
    d'une seule échéance plutôt que de la totalité de la prévision.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._counter = 0

    def store(self, lead_hour: int, field: str, array: np.ndarray) -> Path:
        self._counter += 1
        destination = self.directory / f"{lead_hour:03d}_{field}_{self._counter}.npy"
        np.save(destination, np.asarray(array, dtype=np.float32), allow_pickle=False)
        return destination

    @staticmethod
    def load(reference: "np.ndarray | Path") -> np.ndarray:
        if isinstance(reference, Path):
            array = np.load(reference, allow_pickle=False)
            try:
                reference.unlink()
            except OSError:
                pass
            return array
        return reference

    @staticmethod
    def materialize(map_values: dict[str, Any]) -> dict[str, np.ndarray]:
        """Recharge en mémoire, puis supprime, les fichiers d'une échéance."""

        return {
            field: MapValueSpill.load(value) for field, value in map_values.items()
        }


def parse_grib_files(
    paths: Iterable[Path],
    grid: NationalGrid,
    map_sampler: MapSampler,
    spill: "MapValueSpill | None" = None,
) -> dict[int, dict[str, Any]]:
    """Décode un ensemble de paquets GRIB2 ARPEGE et regroupe les messages
    par échéance réelle (``endStep``).

    Contrairement à AROME, un paquet ARPEGE (ex. ``SP1__000H012H``) couvre
    plusieurs échéances et paramètres dans un même fichier ; il faut donc
    répartir chaque message GRIB dans le bon panier d'échéance plutôt que de
    supposer un fichier = une échéance.

    Si ``spill`` est fourni, les tableaux ``map_values`` (les plus gros, déjà
    à la résolution de rendu) sont écrits sur disque immédiatement plutôt que
    conservés en mémoire pour les 100+ échéances — cf. ``MapValueSpill``.
    """

    steps: dict[int, dict[str, Any]] = {}

    for path in paths:
        with path.open("rb") as handle:
            while True:
                gid = codes_grib_new_from_file(handle)
                if gid is None:
                    break
                try:
                    field = message_field(gid)
                    if field is None:
                        continue
                    end_step = safe_get(gid, "endStep")
                    if end_step is None:
                        continue
                    lead_hour = int(end_step)
                    bucket = steps.setdefault(
                        lead_hour,
                        {
                            "lead_hour": lead_hour,
                            "run_time": None,
                            "valid_time": None,
                            "values": {},
                            "map_values": {},
                        },
                    )
                    bucket["run_time"] = bucket["run_time"] or grib_datetime(
                        gid, "dataDate", "dataTime"
                    )
                    bucket["valid_time"] = bucket["valid_time"] or grib_datetime(
                        gid, "validityDate", "validityTime"
                    )
                    values = grid.extract(gid)
                    if field.startswith("geopotential_") and field.endswith("_gpm"):
                        # IP1 fournit le géopotentiel réel (m²/s²), pas la
                        # hauteur géopotentielle : on convertit ici pour que
                        # transform_step() reçoive directement des mètres,
                        # comme le fait déjà arpege_api_source.py côté API.
                        values = values / GRAVITY_MS2
                    bucket["values"][field] = values
                    if field not in ISOBARIC_POINT_ONLY_FIELDS:
                        # Les niveaux isobares (v1.3.0) ne servent qu'aux
                        # tableaux par commune (onglet Tempête) : inutile de
                        # payer le rééchantillonnage pleine résolution pour
                        # de nouvelles couches carte non demandées, alors que
                        # l'ajout de champs SP1/SP2 en v1.1.0 avait déjà fait
                        # dépasser la RAM du runner GitHub Actions (cf.
                        # MapValueSpill ci-dessus).
                        map_array = map_sampler.extract(gid, grid)
                        bucket["map_values"][field] = (
                            spill.store(lead_hour, field, map_array)
                            if spill is not None
                            else map_array
                        )
                finally:
                    codes_release(gid)

    for lead_hour, bucket in steps.items():
        if bucket["valid_time"] is None and bucket["run_time"] is not None:
            bucket["valid_time"] = bucket["run_time"] + timedelta(hours=lead_hour)
    return steps


def decode_api_run_result(
    api_result: "ApiRunResult",
    grid: NationalGrid,
    map_sampler: MapSampler,
    spill: "MapValueSpill | None" = None,
) -> dict[int, dict[str, Any]]:
    """Décode les messages GRIB2 téléchargés via l'API officielle Météo-France.

    Contrairement à ``parse_grib_files`` (qui associe un champ à un message
    via son ``shortName`` GRIB, cf. ``message_field``), chaque message ici est
    déjà associé à son nom de champ interne par construction (une requête
    ``GetCoverage`` = un champ demandé explicitement), donc aucune
    correspondance heuristique n'est nécessaire — seule la conversion
    géopotentiel → hauteur (÷ g) reste appliquée si besoin.

    Non testé en conditions réelles (cf. arpege_api_source.py) : suppose que
    ``codes_new_from_message`` (eccodes) décode correctement un GRIB2 unique
    reçu en mémoire, comme le fait le client communautaire meteole avec
    cfgrib sur le même flux ``application/wmo-grib``.
    """

    steps: dict[int, dict[str, Any]] = {}
    for lead_hour, fields in api_result.payload.items():
        bucket = steps.setdefault(
            lead_hour,
            {
                "lead_hour": lead_hour,
                "run_time": api_result.run_time,
                "valid_time": None,
                "values": {},
                "map_values": {},
            },
        )
        for field_name, raw_message in fields.items():
            gid = codes_new_from_message(raw_message)
            if gid is None:
                LOGGER.warning(
                    "Message GRIB2 vide pour %s à +%03d h (réponse API "
                    "ignorée)",
                    field_name,
                    lead_hour,
                )
                continue
            try:
                bucket["run_time"] = bucket["run_time"] or grib_datetime(
                    gid, "dataDate", "dataTime"
                )
                bucket["valid_time"] = bucket["valid_time"] or grib_datetime(
                    gid, "validityDate", "validityTime"
                )
                point_values = grid.extract(gid)
                map_values = map_sampler.extract(gid, grid)
                resolved = api_result.resolved_fields.get(field_name)
                if resolved is not None and resolved.request.geopotential_to_height:
                    # Ne divise que si l'indicateur résolu est bien le
                    # géopotentiel brut (m²/s²) et non déjà une hauteur
                    # géopotentielle (m) — cf. FieldRequest.
                    if "GEOPOTENTIAL_HEIGHT" not in resolved.indicator.upper():
                        point_values = point_values / API_GRAVITY
                        map_values = map_values / API_GRAVITY
                bucket["values"][field_name] = point_values
                bucket["map_values"][field_name] = (
                    spill.store(lead_hour, field_name, map_values)
                    if spill is not None
                    else map_values
                )
            finally:
                codes_release(gid)

    for lead_hour, bucket in steps.items():
        if bucket["valid_time"] is None and bucket["run_time"] is not None:
            bucket["valid_time"] = bucket["run_time"] + timedelta(hours=lead_hour)
    return steps


def array_like(
    raw: dict[str, np.ndarray], name: str, shape: tuple[int, ...]
) -> np.ndarray:
    values = raw.get(name)
    if values is None:
        return np.full(shape, np.nan, dtype=np.float64)
    result = np.asarray(values, dtype=np.float64)
    if result.shape != shape:
        raise RuntimeError(f"Forme inattendue pour le champ {name} : {result.shape}")
    return result


def accumulation(
    raw: dict[str, np.ndarray],
    name: str,
    shape: tuple[int, ...],
    previous: np.ndarray | None,
    lead_hour: int,
) -> tuple[np.ndarray, np.ndarray]:
    total = array_like(raw, name, shape)
    if not np.any(np.isfinite(total)) and lead_hour == 0:
        total = np.zeros(shape, dtype=np.float64)
    total = np.where(np.isfinite(total), np.maximum(total, 0.0), np.nan)
    if previous is None:
        hourly = total.copy()
    else:
        hourly = np.maximum(total - previous, 0.0)
        hourly[~np.isfinite(total)] = np.nan
    return hourly, total.copy()


def rounded(values: np.ndarray, decimals: int) -> np.ndarray:
    return np.round(values, decimals)


def transform_step(
    raw: dict[str, np.ndarray],
    altitude: np.ndarray,
    previous: dict[str, np.ndarray],
    lead_hour: int,
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    shape = altitude.shape
    temperature = array_like(raw, "temperature_k", shape) - 273.15
    humidity = np.clip(array_like(raw, "humidity_pct", shape), 0, 100)
    u_wind = array_like(raw, "wind_u_ms", shape)
    v_wind = array_like(raw, "wind_v_ms", shape)
    gust_u = array_like(raw, "gust_u_ms", shape)
    gust_v = array_like(raw, "gust_v_ms", shape)
    surface_pressure = array_like(raw, "surface_pressure_pa", shape) / 100.0
    cape = np.maximum(array_like(raw, "cape_jkg", shape), 0.0)
    cloud_low = np.clip(array_like(raw, "cloud_low_pct", shape), 0, 100)
    cloud_mid = np.clip(array_like(raw, "cloud_mid_pct", shape), 0, 100)
    cloud_high = np.clip(array_like(raw, "cloud_high_pct", shape), 0, 100)
    surface_temperature = array_like(raw, "surface_temperature_k", shape) - 273.15
    boundary_layer_height = np.maximum(
        array_like(raw, "boundary_layer_height_m", shape), 0.0
    )
    precipitable_water = np.maximum(
        array_like(raw, "precipitable_water_mm", shape), 0.0
    )
    temperature_min_2m = array_like(raw, "temperature_min_2m_k", shape) - 273.15
    temperature_max_2m = array_like(raw, "temperature_max_2m_k", shape) - 273.15
    solar_radiation_down = np.maximum(
        array_like(raw, "solar_radiation_down_wm2", shape), 0.0
    )
    thermal_radiation_down = np.maximum(
        array_like(raw, "thermal_radiation_down_wm2", shape), 0.0
    )

    # Champs d'altitude ajoutés en v1.2.0. temperature_850/500 et
    # geopotential_850/500 sont alimentés soit par l'API officielle
    # Météo-France (ARPEGE-005-EURAT-WCS, cf. arpege_api_source.py), soit —
    # depuis v1.3.0 — directement par le paquet IP1 gratuit de data.gouv.fr
    # (cf. message_field()), qui publie en fait tous les niveaux 100-1000 hPa
    # sur la grille EURAT01. temperature_300/wind_speed_300/humidity_850-500/
    # wind_speed_100m restent, eux, exclusifs à l'API (IP1 ne les couvre pas
    # tel qu'exploité ici) : ils restent à NaN hors run API.
    temperature_850 = array_like(raw, "temperature_850_k", shape) - 273.15
    temperature_500 = array_like(raw, "temperature_500_k", shape) - 273.15
    temperature_300 = array_like(raw, "temperature_300_k", shape) - 273.15
    wind_speed_850 = np.hypot(
        array_like(raw, "wind_u_850_ms", shape), array_like(raw, "wind_v_850_ms", shape)
    ) * 3.6
    wind_speed_500 = np.hypot(
        array_like(raw, "wind_u_500_ms", shape), array_like(raw, "wind_v_500_ms", shape)
    ) * 3.6
    wind_speed_300 = np.hypot(
        array_like(raw, "wind_u_300_ms", shape), array_like(raw, "wind_v_300_ms", shape)
    ) * 3.6
    wind_speed_100m = np.hypot(
        array_like(raw, "wind_u_100m_ms", shape), array_like(raw, "wind_v_100m_ms", shape)
    ) * 3.6
    humidity_850 = np.clip(array_like(raw, "humidity_850_pct", shape), 0, 100)
    humidity_500 = np.clip(array_like(raw, "humidity_500_pct", shape), 0, 100)
    # Le WCS Météo-France expose "GEOPOTENTIAL" (m²/s², à diviser par g) ou
    # directement "GEOPOTENTIAL_HEIGHT" (m) selon l'indicateur réellement
    # résolu par arpege_api_source.py ; celui-ci normalise systématiquement
    # vers des mètres avant d'écrire "geopotential_*_gpm", donc aucune
    # division supplémentaire n'est nécessaire ici.
    geopotential_850 = array_like(raw, "geopotential_850_gpm", shape)
    geopotential_500 = array_like(raw, "geopotential_500_gpm", shape)
    vertical_velocity_500 = array_like(raw, "vertical_velocity_500_pas", shape)
    cin = np.maximum(array_like(raw, "cin_jkg", shape), 0.0)

    # Niveaux 800/700/600 hPa ajoutés v1.3.0 (IP1 uniquement, cf. plus haut).
    temperature_800 = array_like(raw, "temperature_800_k", shape) - 273.15
    temperature_700 = array_like(raw, "temperature_700_k", shape) - 273.15
    temperature_600 = array_like(raw, "temperature_600_k", shape) - 273.15
    geopotential_800 = array_like(raw, "geopotential_800_gpm", shape)
    geopotential_700 = array_like(raw, "geopotential_700_gpm", shape)
    geopotential_600 = array_like(raw, "geopotential_600_gpm", shape)
    geopotential_1000 = array_like(raw, "geopotential_1000_gpm", shape)
    wind_speed_800 = np.hypot(
        array_like(raw, "wind_u_800_ms", shape), array_like(raw, "wind_v_800_ms", shape)
    ) * 3.6
    wind_speed_700 = np.hypot(
        array_like(raw, "wind_u_700_ms", shape), array_like(raw, "wind_v_700_ms", shape)
    ) * 3.6
    wind_speed_600 = np.hypot(
        array_like(raw, "wind_u_600_ms", shape), array_like(raw, "wind_v_600_ms", shape)
    ) * 3.6
    # Cisaillement profond (proxy 0-500 hPa, m/s) : différence vectorielle
    # entre le vent 10 m et le vent 500 hPa. Standard en prévision convective
    # comme approximation du cisaillement 0-6 km lorsque le vent réel à 6 km
    # n'est pas disponible (500 hPa ≈ 5,5-6 km selon l'épaisseur de la
    # colonne). Cf. shear_0_6_ms côté HARMONIE pour l'équivalent direct.
    shear_0_6 = np.hypot(
        array_like(raw, "wind_u_500_ms", shape) - u_wind,
        array_like(raw, "wind_v_500_ms", shape) - v_wind,
    )

    # Épaisseur 1000-500 hPa (dam) : proportionnelle à la température moyenne
    # de la couche, utilisée pour distinguer pluie/neige en altitude.
    thickness_1000_500 = (geopotential_500 - geopotential_1000) / 10.0

    # Iso 0°C (m) : interpolation linéaire de l'altitude où la température
    # traverse 0°C entre deux niveaux adjacents du profil vertical dont on
    # dispose (1000/850/800/700/600/500 hPa). Si le profil ne traverse pas
    # 0°C dans cette plage (isotherme 0° trop basse ou trop haute), la valeur
    # reste NaN — pas d'extrapolation fantôme.
    profile_levels_hpa = (1000, 850, 800, 700, 600, 500)
    profile_temperature = {
        1000: array_like(raw, "temperature_1000_k", shape) - 273.15,
        850: temperature_850,
        800: temperature_800,
        700: temperature_700,
        600: temperature_600,
        500: temperature_500,
    }
    profile_geopotential = {
        1000: geopotential_1000,
        850: geopotential_850,
        800: geopotential_800,
        700: geopotential_700,
        600: geopotential_600,
        500: geopotential_500,
    }
    freezing_level = np.full(shape, np.nan)
    found = np.zeros(shape, dtype=bool)
    for lower_hpa, upper_hpa in zip(profile_levels_hpa, profile_levels_hpa[1:]):
        t_lower = profile_temperature[lower_hpa]
        t_upper = profile_temperature[upper_hpa]
        z_lower = profile_geopotential[lower_hpa]
        z_upper = profile_geopotential[upper_hpa]
        crosses = (
            ~found
            & np.isfinite(t_lower)
            & np.isfinite(t_upper)
            & np.isfinite(z_lower)
            & np.isfinite(z_upper)
            & (t_lower >= 0.0)
            & (t_upper < 0.0)
        )
        span = t_lower - t_upper
        safe_span = np.where(span > 1.0e-6, span, 1.0)
        fraction = np.clip(t_lower / safe_span, 0.0, 1.0)
        interpolated = z_lower + fraction * (z_upper - z_lower)
        freezing_level = np.where(crosses, interpolated, freezing_level)
        found = found | crosses

    precipitation, rain_total = accumulation(
        raw,
        "precipitation_total_mm",
        shape,
        previous.get("rain_total"),
        lead_hour,
    )
    snow, snow_total = accumulation(
        raw, "snow_total_mm", shape, previous.get("snow_total"), lead_hour
    )
    # FLSEN/FLLAT sont des flux cumulés (J/m²) depuis le début du run, comme
    # les précipitations : on réutilise le même schéma d'accumulation puis on
    # convertit en MJ/m² pour l'affichage cartographique.
    _sensible_step, sensible_total = accumulation(
        raw, "sensible_heat_total", shape, previous.get("sensible_total"), lead_hour
    )
    _latent_step, latent_total = accumulation(
        raw, "latent_heat_total", shape, previous.get("latent_total"), lead_hour
    )

    wind_speed = np.hypot(u_wind, v_wind) * 3.6
    wind_direction = np.degrees(np.arctan2(-u_wind, -v_wind)) % 360.0
    gust_speed = np.hypot(gust_u, gust_v) * 3.6

    relative = np.clip(humidity / 100.0, 0.01, 1.0)
    gamma = np.log(relative) + 17.625 * temperature / (243.04 + temperature)
    dewpoint = 243.04 * gamma / (17.625 - gamma)
    dewpoint[~np.isfinite(temperature) | ~np.isfinite(humidity)] = np.nan
    lcl = np.clip(125.0 * (temperature - dewpoint), 0, 5000)

    dewpoint_kelvin = np.clip(dewpoint + 273.15, 173.15, 333.15)
    vapour_pressure = 6.11 * np.exp(
        5417.7530 * (1.0 / 273.16 - 1.0 / dewpoint_kelvin)
    )
    humidex = temperature + 0.5555 * (vapour_pressure - 10.0)

    # Approximation de Stull (2011) pour la température du thermomètre mouillé,
    # valable pour une humidité relative de 5 à 99 % (erreur type < 1 °C).
    relative_pct = relative * 100.0
    wet_bulb = (
        temperature * np.arctan(0.151977 * np.sqrt(relative_pct + 8.313659))
        + np.arctan(temperature + relative_pct)
        - np.arctan(relative_pct - 1.676331)
        + 0.00391838 * np.power(relative_pct, 1.5) * np.arctan(0.023101 * relative_pct)
        - 4.686035
    )
    wet_bulb[~np.isfinite(temperature) | ~np.isfinite(humidity)] = np.nan

    wind_chill = temperature.copy()
    chill_valid = (
        np.isfinite(temperature)
        & np.isfinite(wind_speed)
        & (temperature <= 10)
        & (wind_speed >= 4.8)
    )
    wind_factor = np.power(np.maximum(wind_speed, 0), 0.16)
    wind_chill[chill_valid] = (
        13.12
        + 0.6215 * temperature[chill_valid]
        - 11.37 * wind_factor[chill_valid]
        + 0.3965 * temperature[chill_valid] * wind_factor[chill_valid]
    )

    cloud = 100.0 * (
        1.0
        - (1.0 - cloud_low / 100.0)
        * (1.0 - cloud_mid / 100.0)
        * (1.0 - cloud_high / 100.0)
    )
    cloud[
        ~np.isfinite(cloud_low)
        | ~np.isfinite(cloud_mid)
        | ~np.isfinite(cloud_high)
    ] = np.nan

    temperature_kelvin = np.maximum(temperature + 273.15, 180.0)
    pressure = surface_pressure * np.exp(
        9.80665 * np.maximum(altitude, -500.0)
        / (287.05 * (temperature_kelvin + 0.00325 * np.maximum(altitude, 0.0)))
    )
    pressure[~np.isfinite(surface_pressure) | ~np.isfinite(temperature)] = np.nan
    pressure = np.clip(pressure, 850, 1085)

    condition = np.zeros(shape, dtype=np.int16)
    condition[np.isfinite(cloud) & (cloud <= 20)] = 1
    condition[np.isfinite(cloud) & (cloud > 20) & (cloud <= 55)] = 2
    condition[np.isfinite(cloud) & (cloud > 55) & (cloud <= 85)] = 3
    condition[np.isfinite(cloud) & (cloud > 85)] = 4
    condition[np.isfinite(gust_speed) & (gust_speed >= 70)] = 9
    condition[np.isfinite(precipitation) & (precipitation >= 0.1)] = 5
    condition[np.isfinite(precipitation) & (precipitation >= 5)] = 6
    condition[np.isfinite(snow) & (snow >= 0.1)] = 7

    # La réflectivité radar (RFLCTVT_MAX) n'existe pas dans les paquets ARPEGE
    # SP1/SP2 réellement publiés (retirée le 2026-09-05, cf. message_field) :
    # ces indices orage/grêle reposent désormais uniquement sur le CAPE
    # instantané (CAPE_INS, réel) et les rafales, avec des seuils relevés en
    # conséquence. Ils restent des estimations, un peu moins discriminantes
    # qu'avec la réflectivité, mais n'affichent plus de donnée fantôme.
    thunder = np.zeros(shape, dtype=np.int16)
    thunder[cape >= 300] = 1
    thunder[cape >= 800] = 2
    thunder[cape >= 1500] = 3
    thunder[(cape >= 2200) | ((cape >= 1200) & (gust_speed >= 90))] = 4
    thunder[~np.isfinite(cape)] = 0

    lightning = np.clip(np.nan_to_num(cape, nan=0.0) / 25.0, 0, 100)
    hail = np.zeros(shape, dtype=np.int16)
    hail[cape >= 800] = 1
    hail[cape >= 1500] = 2
    hail[(cape >= 2500) & (gust_speed >= 80)] = 3
    convective_fraction = np.clip(np.nan_to_num(cape, nan=0.0) / 1500.0, 0, 1)
    convective_precipitation = precipitation * convective_fraction
    storm_type = np.zeros(shape, dtype=np.int16)
    storm_type[thunder == 1] = 1
    storm_type[thunder == 2] = 2
    storm_type[(thunder >= 3) & (gust_speed >= 60)] = 3
    storm_type[(thunder >= 4) & (cape >= 2000)] = 4

    snow_ratio = np.select(
        [temperature <= -10, temperature <= -5, temperature <= 0, temperature <= 1.5],
        [15.0, 12.0, 10.0, 6.0],
        default=2.0,
    )
    snow_fresh = np.maximum(snow, 0.0) * snow_ratio / 10.0
    previous_fresh = previous.get("fresh_snow")
    if previous_fresh is None:
        snow_depth = snow_fresh.copy()
    else:
        snow_depth = np.nan_to_num(previous_fresh, nan=0.0) + np.nan_to_num(
            snow_fresh, nan=0.0
        )
        snow_depth[~np.isfinite(snow_fresh) & ~np.isfinite(previous_fresh)] = np.nan

    snow_phase = np.zeros(shape, dtype=np.int16)
    snow_phase[np.isfinite(precipitation) & (precipitation >= 0.1)] = 1
    snow_phase[(snow >= 0.03) & (temperature > 0.5)] = 2
    snow_phase[(snow >= 0.03) & (temperature <= 0.5)] = 3
    snow_stick = np.zeros(shape, dtype=np.int16)
    snow_stick[(snow_fresh >= 0.05) & (temperature <= 2.0)] = 1
    snow_stick[(snow_fresh >= 0.2) & (temperature <= 1.0)] = 2
    snow_stick[(snow_fresh >= 0.5) & (temperature <= 0.0)] = 3
    snow_risk = np.zeros(shape, dtype=np.int16)
    snow_risk[(snow >= 0.03) | ((precipitation >= 0.2) & (temperature <= 1.5))] = 1
    snow_risk[(snow_fresh >= 0.3) | ((precipitation >= 1) & (temperature <= 0.5))] = 2
    snow_risk[(snow_fresh >= 1.0) | ((precipitation >= 3) & (temperature <= 0))] = 3
    snow_risk[(snow_fresh >= 3.0) | ((precipitation >= 8) & (temperature <= -1))] = 4

    result = {
        "temperature_c": rounded(temperature, 1),
        "wind_chill_c": rounded(wind_chill, 1),
        "wet_bulb_c": rounded(wet_bulb, 1),
        "dewpoint_c": rounded(dewpoint, 1),
        "humidex": rounded(humidex, 1),
        "humidity_pct": rounded(humidity, 0),
        "precipitation_mm": rounded(precipitation, 1),
        "precipitation_total_mm": rounded(rain_total, 1),
        "cloud_cover_pct": rounded(cloud, 0),
        "cloud_low_pct": rounded(cloud_low, 0),
        "cloud_mid_pct": rounded(cloud_mid, 0),
        "cloud_high_pct": rounded(cloud_high, 0),
        "wind_speed_kmh": rounded(wind_speed, 0),
        "wind_direction_deg": rounded(wind_direction, 0),
        "wind_gust_kmh": rounded(gust_speed, 0),
        "pressure_hpa": rounded(pressure, 0),
        "pressure_surface_hpa": rounded(surface_pressure, 0),
        "surface_pressure_hpa": rounded(surface_pressure, 0),
        "condition_code": condition,
        "cape_jkg": rounded(cape, 0),
        "thunder_risk_code": thunder,
        "lcl_m": rounded(lcl, 0),
        "lightning_score": rounded(lightning, 0),
        "hail_risk_code": hail,
        "convective_precipitation_mm": rounded(convective_precipitation, 1),
        "storm_type_code": storm_type,
        "snow_risk_code": snow_risk,
        "snowfall_mm": rounded(snow, 2),
        "snow_mm": rounded(snow, 2),
        "snow_fresh_cm": rounded(snow_fresh, 1),
        "snow_depth_cm": rounded(snow_depth, 1),
        "snow_water_equivalent_mm": rounded(snow_total, 1),
        "snow_stick_risk_code": snow_stick,
        "snow_phase_code": snow_phase,
        "snowfall_total_mm": rounded(snow_total, 1),
        "altitude_m": rounded(altitude, 0),
        # Champs réels ajoutés v1.1.0 (SP1/SP2, cf. message_field) :
        "surface_temperature_c": rounded(surface_temperature, 1),
        "boundary_layer_height_m": rounded(boundary_layer_height, 0),
        "mixed_layer_depth_m": rounded(boundary_layer_height, 0),
        "precipitable_water_mm": rounded(precipitable_water, 1),
        "temperature_min_2m_c": rounded(temperature_min_2m, 1),
        "temperature_max_2m_c": rounded(temperature_max_2m, 1),
        "sensible_heat_mjm2": rounded(sensible_total / 1.0e6, 2),
        "latent_heat_mjm2": rounded(latent_total / 1.0e6, 2),
        "solar_radiation_down_wm2": rounded(solar_radiation_down, 0),
        "thermal_radiation_down_wm2": rounded(thermal_radiation_down, 0),
        # Champs d'altitude v1.2.0 (API Météo-France uniquement, cf. plus haut)
        "temperature_850_c": rounded(temperature_850, 1),
        "temperature_500_c": rounded(temperature_500, 1),
        "temperature_300_c": rounded(temperature_300, 1),
        "wind_speed_850_kmh": rounded(wind_speed_850, 0),
        "wind_speed_500_kmh": rounded(wind_speed_500, 0),
        "wind_speed_300_kmh": rounded(wind_speed_300, 0),
        "wind_speed_100m_kmh": rounded(wind_speed_100m, 0),
        "humidity_850_pct": rounded(humidity_850, 0),
        "humidity_500_pct": rounded(humidity_500, 0),
        "geopotential_850_m": rounded(geopotential_850, 0),
        "geopotential_500_m": rounded(geopotential_500, 0),
        "vertical_velocity_500_pas": rounded(vertical_velocity_500, 3),
        "cin_jkg": rounded(cin, 0),
        # Champs isobares v1.3.0 (IP1, cf. plus haut) : mode Tempête / haute
        # altitude du comparateur de modèles.
        "temperature_800_c": rounded(temperature_800, 1),
        "temperature_700_c": rounded(temperature_700, 1),
        "temperature_600_c": rounded(temperature_600, 1),
        "geopotential_800_m": rounded(geopotential_800, 0),
        "geopotential_700_m": rounded(geopotential_700, 0),
        "geopotential_600_m": rounded(geopotential_600, 0),
        "thickness_1000_500_dam": rounded(thickness_1000_500, 0),
        "freezing_level_m": rounded(freezing_level, 0),
        "wind_speed_800_kmh": rounded(wind_speed_800, 0),
        "wind_speed_700_kmh": rounded(wind_speed_700, 0),
        "wind_speed_600_kmh": rounded(wind_speed_600, 0),
        "shear_0_6_ms": rounded(shear_0_6, 1),
    }
    state = {
        "rain_total": rain_total,
        "snow_total": snow_total,
        "fresh_snow": snow_depth,
        "sensible_total": sensible_total,
        "latent_total": latent_total,
    }
    return result, state


def json_number(value: Any, integer: bool = False) -> int | float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(round(number)) if integer else number


def compact_rows(
    transformed: dict[str, np.ndarray], point_ids: np.ndarray
) -> list[list[int | float | None]]:
    selected = {
        column: np.asarray(transformed[column])[point_ids] for column in VALUE_COLUMNS
    }
    return [
        [
            json_number(selected[column][position], column in INTEGER_COLUMNS)
            for column in VALUE_COLUMNS
        ]
        for position in range(len(point_ids))
    ]


def write_map_places(catalog: NationalCatalog, destination: Path) -> int:
    places = [
        [
            str(commune[1]),
            int(commune[3]),
            round(float(commune[4]), 5),
            round(float(commune[5]), 5),
            str(commune[0]),
            department.code,
        ]
        for department in catalog.departments.values()
        for commune in department.communes
        if int(commune[3]) > 0
    ]
    places.sort(key=lambda place: (-place[1], place[0]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "schema_version": 2,
                "columns": [
                    "name",
                    "population",
                    "latitude",
                    "longitude",
                    "code_insee",
                    "department",
                ],
                "count": len(places),
                "places": places,
            },
            handle,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        handle.write("\n")
    return len(places)


def write_departments(
    result_directory: Path,
    forecast_directory: Path,
    catalog: NationalCatalog,
    generated_at: str,
) -> tuple[dict[str, Any], int]:
    destination_directory = result_directory / "departements"
    destination_directory.mkdir(parents=True, exist_ok=True)
    department_index: dict[str, Any] = {}
    total_size = 0
    for code, department in catalog.departments.items():
        destination = destination_directory / f"{code}.json"
        with destination.open("w", encoding="utf-8") as output:
            output.write("{")
            output.write('"schema_version":3,"status":"ok","generated_at":')
            json.dump(generated_at, output)
            output.write(',"department":')
            json.dump(code, output)
            output.write(',"columns":')
            json.dump(
                {
                    "points": ["model_index", "latitude", "longitude", "altitude_m"],
                    "communes": [
                        "code_insee",
                        "name",
                        "postal_codes",
                        "population",
                        "latitude",
                        "longitude",
                        "point_id",
                    ],
                    "values": list(VALUE_COLUMNS),
                },
                output,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            output.write(',"points":')
            json.dump(department.points, output, ensure_ascii=False, separators=(",", ":"))
            output.write(',"communes":')
            json.dump(
                department.communes, output, ensure_ascii=False, separators=(",", ":")
            )
            output.write(',"forecast":[')
            first = True
            with (forecast_directory / f"{code}.ndjson").open(
                "r", encoding="utf-8"
            ) as lines:
                for line in lines:
                    if not line.strip():
                        continue
                    if not first:
                        output.write(",")
                    output.write(line.strip())
                    first = False
            output.write("]}\n")
        size = destination.stat().st_size
        total_size += size
        department_index[code] = {
            "file": f"departements/{code}.json",
            "communes": len(department.communes),
            "points": len(department.points),
            "size_bytes": size,
        }
    return department_index, total_size


def build_product(
    resources: dict[tuple[str, int, int], Resource] | None,
    catalog: NationalCatalog,
    forecast_hours: int,
    session: requests.Session,
    working_directory: Path,
    run_hint: datetime | None,
    *,
    grid_spec: GridSpec = LEGACY_GRID,
    api_result: "ApiRunResult | None" = None,
    source_label: str = "data.gouv.fr",
) -> Path:
    result_directory = working_directory / "result"
    forecast_directory = working_directory / "forecast-lines"
    downloads = working_directory / "downloads"
    result_directory.mkdir(parents=True)
    forecast_directory.mkdir(parents=True)
    downloads.mkdir(parents=True)

    line_handles = {
        code: (forecast_directory / f"{code}.ndjson").open("w", encoding="utf-8")
        for code in catalog.departments
    }
    grid = NationalGrid(catalog, grid_spec)
    map_sampler = MapSampler(MAP_WIDTH, MAP_HEIGHT, grid_spec)
    map_renderer = ArpegeMapRenderer(
        np.empty(0),
        np.empty(0),
        result_directory / "maps",
        width=MAP_WIDTH,
        height=MAP_HEIGHT,
        france_latitudes=catalog.commune_latitudes,
        france_longitudes=catalog.commune_longitudes,
        france_departments=catalog.commune_departments,
        boundary_directory=(
            Path(__file__).resolve().parents[1] / "config" / "natural-earth"
        ),
        pregridded=True,
    )

    point_altitude: np.ndarray | None = None
    map_altitude: np.ndarray | None = None
    point_state: dict[str, np.ndarray] = {}
    map_state: dict[str, np.ndarray] = {}
    model_run = run_hint
    source_bytes = 0
    # Cf. MapValueSpill : évite de garder les ~20 champs cartographiques des
    # 103 échéances en mémoire simultanément (cause de l'OOM du runner
    # GitHub Actions introduit en v1.1.0 par l'ajout de champs + le passage
    # à +102 h).
    map_value_spill = MapValueSpill(working_directory / "map-values")

    # Un même paquet (ex. SP1 000H012H) couvre plusieurs échéances : on le
    # télécharge une seule fois, puis on répartit ses messages GRIB par
    # échéance réelle plutôt que de retélécharger un fichier par heure.
    # (chemin data.gouv.fr uniquement ; le chemin API ne télécharge pas de
    # fichiers sur disque, cf. decode_api_run_result plus bas)
    unique_resources: dict[tuple[str, int, int], Resource] = (
        dict(resources.items()) if resources else {}
    )
    downloaded_paths: list[Path] = []
    try:
        if api_result is not None:
            LOGGER.info(
                "Décodage GRIB2 ARPEGE (source API Météo-France, %s requêtes, "
                "%.1f Mo)",
                api_result.request_count,
                api_result.byte_count / 1e6,
            )
            steps_by_lead = decode_api_run_result(
                api_result, grid, map_sampler, spill=map_value_spill
            )
            source_bytes += api_result.byte_count
        else:
            for (group, lead_start, lead_end), resource in sorted(
                unique_resources.items(), key=lambda item: (item[0][0], item[0][1])
            ):
                destination = (
                    downloads / f"{group}-{lead_start:03d}H{lead_end:03d}H.grib2"
                )
                LOGGER.info(
                    "Téléchargement %s +%03d h à +%03d h (%.1f Mo)",
                    group,
                    lead_start,
                    lead_end,
                    (resource.size or 0) / 1e6,
                )
                download_resource(session, resource, destination)
                source_bytes += destination.stat().st_size
                downloaded_paths.append(destination)

            LOGGER.info("Décodage GRIB2 ARPEGE (%s paquets)", len(downloaded_paths))
            steps_by_lead = parse_grib_files(
                downloaded_paths, grid, map_sampler, spill=map_value_spill
            )

        available_leads = sorted(
            lead for lead in steps_by_lead if lead <= forecast_hours
        )
        if not available_leads:
            raise RuntimeError(
                "Aucune échéance décodée dans les paquets ARPEGE téléchargés"
            )
        if 0 not in available_leads:
            raise RuntimeError("Échéance +00 h absente : altitude ARPEGE indisponible")

        for index, lead in enumerate(available_leads):
            step = steps_by_lead[lead]
            # Recharge (et supprime du disque) les champs cartographiques de
            # cette seule échéance — cf. MapValueSpill : ils ont été écrits
            # sur disque au décodage pour ne jamais tenir en mémoire les ~20
            # champs des 103 échéances simultanément.
            step["map_values"] = MapValueSpill.materialize(step["map_values"])
            if "temperature_k" not in step["values"]:
                raise RuntimeError(f"Température à 2 m absente de l'échéance +{lead:02d} h")
            if step["valid_time"] is None:
                raise RuntimeError(f"Date de validité absente à +{lead:02d} h")
            model_run = model_run or step["run_time"]
            if lead == 0:
                point_altitude = step["values"].get("altitude_m")
                map_altitude = step["map_values"].get("altitude_m")
                if point_altitude is None or map_altitude is None:
                    raise RuntimeError(
                        "Altitude ARPEGE (champ 'h') absente des paquets SP2 +00 h"
                    )
                for department in catalog.departments.values():
                    for position, global_id in enumerate(department.global_point_ids):
                        department.points[position].append(
                            json_number(point_altitude[int(global_id)], integer=True)
                        )
            assert point_altitude is not None and map_altitude is not None
            LOGGER.info(
                "Cartes ARPEGE %s/%s : +%02d h",
                index + 1,
                len(available_leads),
                lead,
            )
            transformed, point_state = transform_step(
                step["values"], point_altitude, point_state, lead
            )
            map_transformed, map_state = transform_step(
                step["map_values"], map_altitude, map_state, lead
            )
            map_fields = {
                key: values
                for key, values in map_transformed.items()
                if key in MAP_FIELDS
            }
            map_renderer.render_step(
                lead_hour=lead,
                valid_time=step["valid_time"],
                fields=map_fields,
            )
            # Libère les tableaux cartographiques de cette échéance : sans
            # ceci, `steps_by_lead` (qui reste référencé jusqu'à la fin de la
            # fonction) reconstituerait progressivement en mémoire tout ce
            # que MapValueSpill avait déchargé sur disque, ce qui annulerait
            # le gain mémoire recherché.
            step["map_values"] = None
            map_fields = None
            map_transformed = None
            iso_time = iso_utc(step["valid_time"])
            for code, department in catalog.departments.items():
                line = [
                    iso_time,
                    compact_rows(transformed, department.global_point_ids),
                ]
                json.dump(
                    line,
                    line_handles[code],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                line_handles[code].write("\n")
    finally:
        for path in downloaded_paths:
            path.unlink(missing_ok=True)
        for handle in line_handles.values():
            handle.close()
        shutil.rmtree(map_value_spill.directory, ignore_errors=True)

    generated_at = iso_utc(datetime.now(timezone.utc))
    assert generated_at is not None
    run_time = iso_utc(model_run)
    places_path = result_directory / "maps" / "communes.json"
    places_count = write_map_places(catalog, places_path)
    map_manifest = map_renderer.write_manifest(
        generated_at=generated_at,
        run_time=run_time,
        places_path="maps/communes.json",
    )
    department_index, total_size = write_departments(
        result_directory,
        forecast_directory,
        catalog,
        generated_at,
    )

    is_api_source = api_result is not None
    model = {
        "name": (
            "ARPEGE Euro-Atlantique 0,05° (API Météo-France)"
            if is_api_source
            else "ARPEGE Europe 0,1°"
        ),
        "provider": "Météo-France",
        "dataset": (
            "API officielle Météo-France — Modèle ARPÈGE API v1.0 "
            "(MF-NWP-GLOBAL-ARPEGE-005-EURAT-WCS)"
            if is_api_source
            else "Paquets ARPEGE résolution 0,1°"
        ),
        "domain": "EURAT" if is_api_source else "EURAT01",
        "resolution_degrees": grid_spec.step,
        "resolution_km": round(grid_spec.step * 111.0, 1),
        "forecast_hours_requested": forecast_hours,
        "run_time": run_time,
        "pipeline_version": PIPELINE_VERSION,
        "catalog_version": catalog.version,
        "storm_diagnostics": True,
        "snow_diagnostics": True,
        "source": source_label,
        "source_url": (
            "https://portail-api.meteofrance.fr/web/fr/api/arpege"
            if is_api_source
            else DATASET_PAGE
        ),
        "source_size_bytes": source_bytes,
        "license": (
            "Licence des données Météo-France (portail API, abonnement "
            "payant)" if is_api_source else "Licence Ouverte 2.0"
        ),
    }
    if is_api_source:
        model["api_resolved_fields"] = sorted(api_result.resolved_fields)
        model["api_missing_fields"] = sorted(api_result.missing_fields)
    index = {
        "schema_version": 3,
        "status": "ok",
        "generated_at": generated_at,
        "model": model,
        "coverage": {
            "label": "France métropolitaine et Corse",
            "communes": catalog.commune_count,
            "departments": len(catalog.departments),
        },
        "condition_codes": CONDITION_CODES,
        "diagnostics": {
            "direct": [
                "MUCAPE",
                "pluie cumulée",
                "neige cumulée",
                "pression de surface",
                "nuages bas/moyens/élevés",
                "température de surface",
                "hauteur de couche limite",
                "eau précipitable",
                "Tmin/Tmax 2 m",
                "flux de chaleur sensible/latente cumulés",
                "rayonnement solaire/thermique descendant",
            ],
            "derived": [
                "pression ramenée au niveau de la mer",
                "point de rosée",
                "LCL",
                "risque orage (CAPE)",
                "risque grêle (CAPE)",
                "phase et tenue de la neige",
            ],
            "unavailable": (
                [
                    "réflectivité radar (absente des paquets SP1/SP2)",
                    "graupel (absent des paquets SP1/SP2)",
                    "visibilité 2 m (absente des paquets SP1/SP2 ; seule la "
                    "visibilité isobare IP2 existe, non intégrée)",
                    "densité de foudre (non produite par le PNT ARPEGE)",
                    "ISO 0/-10/-20°C, vorticité/divergence (nécessitent un "
                    "profil vertical complet, non encore calculés même via "
                    "l'API)",
                ]
                if is_api_source
                else [
                    "réflectivité radar (absente des paquets SP1/SP2)",
                    "graupel (absent des paquets SP1/SP2)",
                    "visibilité 2 m (absente des paquets SP1/SP2 ; seule la "
                    "visibilité isobare IP2 existe, non intégrée)",
                    "densité de foudre (non produite par le PNT ARPEGE)",
                    "CIN, vent/température/humidité en altitude, "
                    "géopotentiel, ISO 0/-10/-20°C, vitesse verticale, "
                    "vorticité/divergence, vent à 100 m (nécessitent l'API "
                    "officielle Météo-France payante — disponibles "
                    "seulement quand METEOFRANCE_API_KEY est configurée, "
                    "cf. README)",
                ]
            ),
        },
        "search": {
            "provider": "API Découpage administratif — République française",
            "endpoint": "https://geo.api.gouv.fr/communes",
        },
        "maps": {
            "status": "ok",
            "module_version": map_manifest["module_version"],
            "manifest": "maps/index.json",
            "layers": len(map_manifest["layers"]),
            "steps": len(map_manifest["steps"]),
            "places": places_count,
        },
        "departments": department_index,
        "total_department_bytes": total_size,
    }
    with (result_directory / "index.json").open("w", encoding="utf-8") as handle:
        json.dump(index, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
    LOGGER.info(
        "Produit ARPEGE prêt : %.1f Mo de tableaux, %s couches, %s échéances",
        total_size / 1e6,
        len(map_manifest["layers"]),
        len(map_manifest["steps"]),
    )
    return result_directory


def safe_output_directory(path: Path) -> Path:
    resolved = path.resolve()
    forbidden = {Path("/").resolve(), Path.cwd().resolve(), Path.home().resolve()}
    if resolved in forbidden or len(resolved.parts) < 3:
        raise RuntimeError(f"Dossier de sortie dangereux : {resolved}")
    return resolved


def api_grid_spec(
    domain: tuple[float, float, float, float], precision_degrees: float
) -> GridSpec:
    """Construit la grille implicite d'un run ARPEGE API à partir du domaine
    demandé (min_lat, max_lat, min_lon, max_lon) et de la résolution
    (0,05° pour EURAT). Le premier message GRIB2 réellement décodé confirme
    ou infirme cette hypothèse via ``NationalGrid.validate`` (échec explicite
    plutôt qu'un décalage silencieux)."""

    min_lat, max_lat, min_lon, max_lon = domain
    nj = int(round((max_lat - min_lat) / precision_degrees)) + 1
    ni = int(round((max_lon - min_lon) / precision_degrees)) + 1
    return GridSpec(
        ni=ni, nj=nj, lat_first=max_lat, lon_first=min_lon, step=precision_degrees
    )


def build_via_api(
    catalog_path: Path,
    forecast_hours: int,
    api_key: str,
) -> tuple[NationalCatalog, "ApiRunResult", GridSpec] | None:
    """Tente un run complet via l'API officielle Météo-France.

    Retourne ``None`` si l'API n'est pas utilisable (aucune clé, échec de
    résolution des indicateurs requis, erreur réseau/auth persistante) afin
    de laisser l'appelant retomber sur data.gouv.fr.
    """

    if not API_SOURCE_AVAILABLE:
        LOGGER.warning(
            "Modules API Météo-France absents (meteofrance_wcs_client.py / "
            "arpege_api_source.py) : source API indisponible"
        )
        return None
    try:
        api_result = fetch_arpege_api_run(api_key, forecast_hours)
    except Exception as error:  # noqa: BLE001 - toute erreur doit déclencher le fallback
        LOGGER.warning(
            "Échec de la récupération ARPEGE via l'API Météo-France (%s : "
            "%s) ; retour sur data.gouv.fr",
            type(error).__name__,
            error,
        )
        return None
    grid = api_grid_spec(api_result.domain, api_result.precision_degrees)
    catalog = load_catalog(catalog_path, grid)
    return catalog, api_result, grid


def publish_result(source: Path, destination: Path) -> None:
    target = safe_output_directory(destination)
    temporary = target.with_name(target.name + ".new")
    if temporary.exists():
        shutil.rmtree(temporary)
    shutil.copytree(source, temporary)
    if target.exists():
        shutil.rmtree(target)
    temporary.replace(target)


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    if not 1 <= args.forecast_hours <= 102:
        raise ValueError("forecast-hours doit être compris entre 1 et 102")
    if not 1 <= args.catalog_attempts <= 20:
        raise ValueError("catalog-attempts doit être compris entre 1 et 20")
    if not 0 <= args.catalog_retry_seconds <= 600:
        raise ValueError("catalog-retry-seconds doit être compris entre 0 et 600")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    api_key = os.environ.get(METEOFRANCE_API_KEY_ENV, "").strip()
    use_api = args.source == "api" or (args.source == "auto" and bool(api_key))
    if args.source == "api" and not api_key:
        raise RuntimeError(
            f"--source api requiert la variable d'environnement "
            f"{METEOFRANCE_API_KEY_ENV}"
        )
    if args.resource_directory:
        # Le mode "GRIB locaux" (tests hors ligne) reste exclusivement
        # data.gouv.fr : il n'a pas d'équivalent API à télécharger.
        use_api = False

    api_catalog: NationalCatalog | None = None
    api_result: "ApiRunResult | None" = None
    api_grid: GridSpec = LEGACY_GRID
    if use_api:
        LOGGER.info(
            "Tentative de récupération ARPEGE via l'API officielle "
            "Météo-France (%s)",
            "EURAT 0,05°" if API_SOURCE_AVAILABLE else "modules indisponibles",
        )
        outcome = build_via_api(Path(args.catalog), args.forecast_hours, api_key)
        if outcome is None:
            if args.source == "api":
                raise RuntimeError(
                    "La source API Météo-France a échoué et --source api "
                    "interdit le repli sur data.gouv.fr"
                )
            LOGGER.info("Repli sur la source data.gouv.fr historique")
        else:
            api_catalog, api_result, api_grid = outcome

    if api_result is not None and api_catalog is not None:
        run_hint = api_result.run_time
        LOGGER.info(
            "Run ARPEGE (API Météo-France) sélectionné : %s (%s champs "
            "résolus, %s introuvables)",
            iso_utc(run_hint) or "inconnu",
            len(api_result.resolved_fields),
            len(api_result.missing_fields),
        )
        if not args.force and already_published(args.current_metadata_url, run_hint):
            LOGGER.info(
                "Ce run ARPEGE est déjà publié ; aucune reconstruction nécessaire"
            )
            return 0
        with tempfile.TemporaryDirectory(prefix="arpege-france-build-") as temporary:
            result = build_product(
                None,
                api_catalog,
                args.forecast_hours,
                session,
                Path(temporary),
                run_hint,
                grid_spec=api_grid,
                api_result=api_result,
                source_label="api.meteofrance.fr",
            )
            publish_result(result, Path(args.output_dir))
        LOGGER.info("Fichiers nationaux prêts dans %s (source API)", args.output_dir)
        return 0

    # --- Source historique : scraping data.gouv.fr (fallback ou --source legacy) ---
    catalog = load_catalog(Path(args.catalog))
    if args.resource_directory:
        discovered = local_resources(Path(args.resource_directory))
        resources, run_hint = choose_resources(discovered, args.forecast_hours)
    else:
        selection = wait_for_complete_remote_run(
            session,
            args.forecast_hours,
            args.catalog_attempts,
            args.catalog_retry_seconds,
        )
        if selection is None:
            return 0
        resources, run_hint = selection
    LOGGER.info("Run ARPEGE sélectionné : %s", iso_utc(run_hint) or "GRIB local")
    if not args.force and not args.resource_directory and already_published(
        args.current_metadata_url, run_hint
    ):
        LOGGER.info("Ce run ARPEGE est déjà publié ; aucune reconstruction nécessaire")
        return 0

    with tempfile.TemporaryDirectory(prefix="arpege-france-build-") as temporary:
        result = build_product(
            resources,
            catalog,
            args.forecast_hours,
            session,
            Path(temporary),
            run_hint,
            source_label="data.gouv.fr",
        )
        publish_result(result, Path(args.output_dir))
    LOGGER.info("Fichiers nationaux prêts dans %s (source data.gouv.fr)", args.output_dir)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        LOGGER.exception("Échec de la mise à jour ARPEGE France")
        raise SystemExit(1)
