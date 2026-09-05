#!/usr/bin/env python3
"""Source de données ARPEGE basée sur l'API officielle Météo-France (WCS).

Remplace, quand une clé ``METEOFRANCE_API_KEY`` est disponible, le scraping du
paquet public data.gouv.fr par des requêtes ``GetCoverage`` ciblées sur le
service ``MF-NWP-GLOBAL-ARPEGE-005-EURAT-WCS`` (0,05°, domaine Euro-
Atlantique), abonnement "Modèle ARPÈGE API v1.0" du portail
portail-api.meteofrance.fr.

Ce module ne fait QUE la partie réseau/résolution : télécharger les bons
messages GRIB2 (un par indicateur/niveau/échéance) et les regrouper par
échéance. Le décodage GRIB2 (eccodes) et toute la logique de transformation
restent dans ``update_arpege_france.py`` pour ne pas dupliquer
``NationalGrid``/``MapSampler`` et pour éviter un import circulaire.

IMPORTANT — non testé en conditions réelles : cet environnement de
développement n'a pas accès à une vraie clé ``METEOFRANCE_API_KEY``. Les
éléments suivants sont vérifiés (lecture du code source du client
communautaire MAIF/meteole, cf. ``meteofrance_wcs_client.py``) :

- l'URL de base, le schéma d'authentification par en-tête ``apikey``, la
  structure des chemins WCS et la syntaxe des paramètres GetCoverage/
  DescribeCoverage/GetCapabilities.

Ce qui reste à confirmer par l'utilisateur avec sa vraie clé (cf. rapport de
livraison) :

- les noms EXACTS des indicateurs exposés par ``GetCapabilities`` pour
  ARPEGE-005-EURAT (supposés suivre le schéma ``VARIABLE__TYPE_DE_NIVEAU``
  observé sur AROME/ARPEGE via meteole, ex. ``TEMPERATURE__ISOBARIC_SURFACE``,
  ``GEOMETRIC_HEIGHT__GROUND_OR_WATER_SURFACE`` — mais jamais vérifié pour le
  domaine EURAT/résolution 0,05° précisément) ;
- que ``GetCoverage`` avec ``format=application/wmo-grib`` renvoie bien un
  GRIB2 unique lisible tel quel par eccodes (très probable d'après le client
  meteole qui décode ce flux avec cfgrib, mais jamais exécuté ici) ;
- le volume réel de requêtes toléré par le portail (chaque variable/niveau/
  échéance nécessite un ``GetCoverage`` séparé — voir ``lead_hour_schedule``
  et ``FIELD_REQUESTS`` ci-dessous, potentiellement plusieurs centaines de
  requêtes par run) : ``MAX_WORKERS``/``REQUEST_PAUSE_SECONDS`` sont des
  valeurs prudentes à ajuster après un premier test réel.

En cas d'échec (clé invalide, indicateur requis introuvable, erreur réseau
persistante), ce module lève une exception (``MeteoFranceWCSError`` ou
``ArpegeApiSourceError``) que ``update_arpege_france.py`` intercepte pour
retomber automatiquement sur le scraping data.gouv.fr existant.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from meteofrance_wcs_client import (
    CoverageSummary,
    MeteoFranceWCSClient,
    MeteoFranceWCSError,
)

LOGGER = logging.getLogger("arpege.api_source")

ARPEGE_API_MODEL_PATH = "arpege/1.0"
ARPEGE_API_TERRITORY = "EURAT"
ARPEGE_API_PRECISION_DEGREES = 0.05
ARPEGE_API_ENTRY_POINT = "wcs/MF-NWP-GLOBAL-ARPEGE-005-EURAT-WCS"

# Domaine cible : au moins la France + marge (le domaine réel EURAT complet
# est plus grand ; on ne demande que ce qui est nécessaire aux cartes/points
# communaux pour limiter le poids de chaque réponse GetCoverage).
REQUEST_LATITUDE_RANGE = (36.0, 58.0)
REQUEST_LONGITUDE_RANGE = (-13.0, 19.0)

MAX_WORKERS = 4
REQUEST_PAUSE_SECONDS = 0.0


class ArpegeApiSourceError(RuntimeError):
    """La source API Météo-France n'a pas pu produire un run exploitable."""


@dataclass(frozen=True)
class FieldRequest:
    """Décrit comment retrouver un champ interne dans les capacités WCS."""

    field: str
    indicator_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...] = ()
    pressure_hpa: int | None = None
    height_m: int | None = None
    required: bool = False
    # Si vrai, l'indicateur résolu est en géopotentiel (m²/s²) et doit être
    # divisé par g pour obtenir une hauteur géopotentielle en mètres.
    geopotential_to_height: bool = False


# NB : les mots-clés ci-dessous sont volontairement larges (```any``` = au
# moins un des motifs doit apparaître dans le nom d'indicateur) car les noms
# exacts n'ont pas pu être vérifiés sans clé API réelle (cf. docstring du
# module). ``resolve_indicators`` journalise systématiquement l'indicateur
# effectivement choisi pour chaque champ, afin qu'un désaccord soit visible
# dès le premier run réel.
FIELD_REQUESTS: tuple[FieldRequest, ...] = (
    # --- Champs de surface/2m — équivalents des paquets SP1/SP2 actuels ---
    FieldRequest("temperature_k", ("TEMPERATURE",), exclude_keywords=("MIN", "MAX", "ISOBARIC", "SURFACE_TEMPERATURE"), height_m=2, required=True),
    FieldRequest("humidity_pct", ("RELATIVE_HUMIDITY", "HUMIDITY"), exclude_keywords=("ISOBARIC",), height_m=2),
    FieldRequest("wind_u_ms", ("U_COMPONENT_OF_WIND",), exclude_keywords=("GUST", "ISOBARIC"), height_m=10, required=True),
    FieldRequest("wind_v_ms", ("V_COMPONENT_OF_WIND",), exclude_keywords=("GUST", "ISOBARIC"), height_m=10, required=True),
    FieldRequest("gust_u_ms", ("U_COMPONENT_OF_WIND_GUST", "WIND_GUST"), exclude_keywords=("ISOBARIC",), height_m=10),
    FieldRequest("gust_v_ms", ("V_COMPONENT_OF_WIND_GUST",), exclude_keywords=("ISOBARIC",), height_m=10),
    FieldRequest("surface_pressure_pa", ("PRESSURE",), exclude_keywords=("MEAN_SEA", "ISOBARIC", "VAPOUR", "TENDENCY")),
    FieldRequest("cape_jkg", ("CONVECTIVE_AVAILABLE_POTENTIAL_ENERGY", "CAPE")),
    FieldRequest("cloud_low_pct", ("LOW_CLOUD_COVER",)),
    FieldRequest("cloud_mid_pct", ("MEDIUM_CLOUD_COVER",)),
    FieldRequest("cloud_high_pct", ("HIGH_CLOUD_COVER",)),
    FieldRequest("precipitation_total_mm", ("TOTAL_PRECIPITATION", "TOTAL_WATER_PRECIPITATION")),
    FieldRequest("snow_total_mm", ("TOTAL_SNOW_PRECIPITATION", "SNOW_PRECIPITATION", "SNOWFALL")),
    FieldRequest("altitude_m", ("GEOMETRIC_HEIGHT",), required=True),
    FieldRequest("surface_temperature_k", ("TEMPERATURE",), exclude_keywords=("MIN", "MAX", "ISOBARIC", "HEIGHT_LEVEL"), height_m=None),
    FieldRequest("boundary_layer_height_m", ("BOUNDARY_LAYER_HEIGHT", "HEIGHT_OF_BOUNDARY_LAYER")),
    FieldRequest("precipitable_water_mm", ("PRECIPITABLE_WATER", "TOTAL_COLUMN_WATER_VAPOUR")),
    FieldRequest("temperature_min_2m_k", ("MINIMUM_TEMPERATURE",), height_m=2),
    FieldRequest("temperature_max_2m_k", ("MAXIMUM_TEMPERATURE",), height_m=2),
    FieldRequest("sensible_heat_total", ("SURFACE_SENSIBLE_HEAT_FLUX", "SENSIBLE_HEAT")),
    FieldRequest("latent_heat_total", ("SURFACE_LATENT_HEAT_FLUX", "LATENT_HEAT")),
    FieldRequest("solar_radiation_down_wm2", ("DOWNWARD_SHORT_WAVE_RADIATION", "SURFACE_SOLAR_RADIATION_DOWN")),
    FieldRequest("thermal_radiation_down_wm2", ("DOWNWARD_LONG_WAVE_RADIATION", "SURFACE_THERMAL_RADIATION_DOWN")),
    # --- Couches en altitude débloquées par l'API (v1.2.0) ---
    FieldRequest("temperature_850_k", ("TEMPERATURE",), exclude_keywords=("MIN", "MAX"), pressure_hpa=850),
    FieldRequest("temperature_500_k", ("TEMPERATURE",), exclude_keywords=("MIN", "MAX"), pressure_hpa=500),
    FieldRequest("temperature_300_k", ("TEMPERATURE",), exclude_keywords=("MIN", "MAX"), pressure_hpa=300),
    FieldRequest("wind_u_850_ms", ("U_COMPONENT_OF_WIND",), exclude_keywords=("GUST",), pressure_hpa=850),
    FieldRequest("wind_v_850_ms", ("V_COMPONENT_OF_WIND",), exclude_keywords=("GUST",), pressure_hpa=850),
    FieldRequest("wind_u_500_ms", ("U_COMPONENT_OF_WIND",), exclude_keywords=("GUST",), pressure_hpa=500),
    FieldRequest("wind_v_500_ms", ("V_COMPONENT_OF_WIND",), exclude_keywords=("GUST",), pressure_hpa=500),
    FieldRequest("wind_u_300_ms", ("U_COMPONENT_OF_WIND",), exclude_keywords=("GUST",), pressure_hpa=300),
    FieldRequest("wind_v_300_ms", ("V_COMPONENT_OF_WIND",), exclude_keywords=("GUST",), pressure_hpa=300),
    FieldRequest("humidity_850_pct", ("RELATIVE_HUMIDITY", "HUMIDITY"), pressure_hpa=850),
    FieldRequest("humidity_500_pct", ("RELATIVE_HUMIDITY", "HUMIDITY"), pressure_hpa=500),
    FieldRequest("geopotential_850_gpm", ("GEOPOTENTIAL_HEIGHT", "GEOPOTENTIAL"), pressure_hpa=850, geopotential_to_height=True),
    FieldRequest("geopotential_500_gpm", ("GEOPOTENTIAL_HEIGHT", "GEOPOTENTIAL"), pressure_hpa=500, geopotential_to_height=True),
    FieldRequest("vertical_velocity_500_pas", ("VERTICAL_VELOCITY", "PRESSURE_VERTICAL_VELOCITY", "LAGRANGIAN_TENDENCY_OF_AIR_PRESSURE"), pressure_hpa=500),
    FieldRequest("cin_jkg", ("CONVECTIVE_INHIBITION",)),
    FieldRequest("wind_u_100m_ms", ("U_COMPONENT_OF_WIND",), exclude_keywords=("GUST",), height_m=100),
    FieldRequest("wind_v_100m_ms", ("V_COMPONENT_OF_WIND",), exclude_keywords=("GUST",), height_m=100),
)

GRAVITY = 9.80665


@dataclass
class ResolvedField:
    request: FieldRequest
    coverage_id: str
    indicator: str


@dataclass
class ApiRunResult:
    run_time: datetime | None
    domain: tuple[float, float, float, float]  # min_lat, max_lat, min_lon, max_lon
    precision_degrees: float
    # lead_hour -> field -> bytes GRIB2 bruts (un seul message)
    payload: dict[int, dict[str, bytes]]
    resolved_fields: dict[str, ResolvedField]
    missing_fields: list[str]
    request_count: int
    byte_count: int


def _matches(indicator: str, request: FieldRequest) -> bool:
    upper = indicator.upper()
    if not any(keyword in upper for keyword in request.indicator_keywords):
        return False
    if any(keyword in upper for keyword in request.exclude_keywords):
        return False
    return True


def resolve_indicators(
    capabilities: list[CoverageSummary],
) -> tuple[dict[str, ResolvedField], list[str]]:
    """Associe chaque ``FieldRequest`` à un indicateur réel des capacités WCS.

    Utilise le run le plus récent commun. Journalise chaque correspondance
    trouvée (niveau INFO) pour permettre une vérification manuelle rapide au
    premier run réel.
    """

    runs = sorted({item.run_text for item in capabilities})
    if not runs:
        raise ArpegeApiSourceError("Aucun run dans les capacités WCS ARPEGE")
    latest_run = runs[-1]
    by_indicator: dict[str, CoverageSummary] = {}
    for item in capabilities:
        if item.run_text != latest_run:
            continue
        by_indicator.setdefault(item.indicator, item)

    resolved: dict[str, ResolvedField] = {}
    missing: list[str] = []
    for request in FIELD_REQUESTS:
        candidates = [
            (indicator, summary)
            for indicator, summary in by_indicator.items()
            if _matches(indicator, request)
        ]
        if not candidates:
            missing.append(request.field)
            level = f"{request.pressure_hpa} hPa" if request.pressure_hpa else (
                f"{request.height_m} m" if request.height_m else "surface"
            )
            LOGGER.log(
                logging.ERROR if request.required else logging.WARNING,
                "Indicateur ARPEGE introuvable pour %s (%s, mots-clés %s)",
                request.field,
                level,
                request.indicator_keywords,
            )
            continue
        # Priorité au nom le plus court (moins de suffixes = correspondance
        # la plus directe) en cas d'ambiguïté.
        indicator, summary = min(candidates, key=lambda pair: len(pair[0]))
        resolved[request.field] = ResolvedField(
            request=request, coverage_id=summary.coverage_id, indicator=indicator
        )
        LOGGER.info(
            "Champ '%s' résolu vers l'indicateur ARPEGE '%s' (coverage %s)",
            request.field,
            indicator,
            summary.coverage_id,
        )

    required_missing = [name for name in missing if _field_required(name)]
    if required_missing:
        raise ArpegeApiSourceError(
            f"Champs ARPEGE requis introuvables via l'API : {required_missing}"
        )
    return resolved, missing


def _field_required(field_name: str) -> bool:
    return any(
        request.field == field_name and request.required for request in FIELD_REQUESTS
    )


def lead_hour_schedule(forecast_hours: int) -> list[int]:
    """Reproduit la cadence des paquets ARPEGE (horaire jusqu'à +48 h, puis
    3 h au-delà), pour limiter le nombre de requêtes GetCoverage."""

    hours = [hour for hour in range(0, 49) if hour <= forecast_hours]
    hours += [hour for hour in range(51, forecast_hours + 1, 3)]
    return sorted(set(hours))


def fetch_arpege_api_run(
    api_key: str,
    forecast_hours: int,
    *,
    latitude_range: tuple[float, float] = REQUEST_LATITUDE_RANGE,
    longitude_range: tuple[float, float] = REQUEST_LONGITUDE_RANGE,
    max_workers: int = MAX_WORKERS,
) -> ApiRunResult:
    """Télécharge un run ARPEGE complet via l'API officielle Météo-France.

    Lève ``ArpegeApiSourceError``/``MeteoFranceWCSError`` en cas d'échec ;
    à charge de l'appelant de retomber sur data.gouv.fr le cas échéant.
    """

    client = MeteoFranceWCSClient(
        api_key,
        ARPEGE_API_MODEL_PATH,
        ARPEGE_API_ENTRY_POINT,
    )
    LOGGER.info(
        "Interrogation GetCapabilities (%s, %s°)",
        ARPEGE_API_TERRITORY,
        ARPEGE_API_PRECISION_DEGREES,
    )
    capabilities = client.get_capabilities()
    resolved, missing = resolve_indicators(capabilities)

    reference = next(iter(resolved.values()))
    domain_description = client.describe_coverage(reference.coverage_id)
    min_lat = max(domain_description.min_latitude, latitude_range[0])
    max_lat = min(domain_description.max_latitude, latitude_range[1])
    min_lon = max(domain_description.min_longitude, longitude_range[0])
    max_lon = min(domain_description.max_longitude, longitude_range[1])
    if min_lat >= max_lat or min_lon >= max_lon:
        raise ArpegeApiSourceError(
            "Domaine ARPEGE API incohérent avec la zone demandée : "
            f"API=[{domain_description.min_latitude},{domain_description.max_latitude}]"
            f"x[{domain_description.min_longitude},{domain_description.max_longitude}]"
        )

    run_time: datetime | None = None
    try:
        run_time = datetime.strptime(
            reference.coverage_id.split("___")[1].split("Z")[0], "%Y-%m-%dT%H.%M.%S"
        ).replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        LOGGER.warning(
            "Impossible d'extraire l'horodatage du run depuis %s",
            reference.coverage_id,
        )

    available_horizons = set(domain_description.forecast_horizons_s) or None
    lead_hours = lead_hour_schedule(forecast_hours)
    if available_horizons is not None:
        lead_hours = [
            hour for hour in lead_hours if hour * 3600 in available_horizons
        ]
    if not lead_hours:
        raise ArpegeApiSourceError(
            "Aucune échéance ARPEGE API commune avec la cadence attendue"
        )

    payload: dict[int, dict[str, bytes]] = {hour: {} for hour in lead_hours}
    request_count = 0
    byte_count = 0

    def _download(field_name: str, resolved_field: ResolvedField, hour: int) -> tuple[str, int, bytes]:
        data = client.get_coverage(
            resolved_field.coverage_id,
            time_s=hour * 3600,
            latitude_range=(min_lat, max_lat),
            longitude_range=(min_lon, max_lon),
            pressure_hpa=resolved_field.request.pressure_hpa,
            height_m=resolved_field.request.height_m,
        )
        if REQUEST_PAUSE_SECONDS:
            time.sleep(REQUEST_PAUSE_SECONDS)
        return field_name, hour, data

    jobs = [
        (field_name, resolved_field, hour)
        for field_name, resolved_field in resolved.items()
        for hour in lead_hours
    ]
    LOGGER.info(
        "Téléchargement ARPEGE API : %s requêtes GetCoverage (%s champs × %s "
        "échéances)",
        len(jobs),
        len(resolved),
        len(lead_hours),
    )

    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_download, field_name, resolved_field, hour): (field_name, hour)
            for field_name, resolved_field, hour in jobs
        }
        for future in as_completed(futures):
            field_name, hour = futures[future]
            try:
                _, _, data = future.result()
            except MeteoFranceWCSError as error:
                errors.append(f"{field_name}@+{hour:03d}h: {error}")
                continue
            payload[hour][field_name] = data
            request_count += 1
            byte_count += len(data)

    if errors:
        LOGGER.warning(
            "%s requêtes GetCoverage ont échoué (champs non critiques omis) : %s",
            len(errors),
            "; ".join(errors[:10]) + (" ..." if len(errors) > 10 else ""),
        )

    # Vérifie que les champs requis sont bien présents pour au moins
    # l'échéance +0 h (nécessaire pour l'altitude/le point de départ).
    for field_name in resolved:
        if resolved[field_name].request.required and field_name not in payload.get(0, {}):
            raise ArpegeApiSourceError(
                f"Champ requis '{field_name}' absent de l'échéance +0 h après "
                "téléchargement via l'API"
            )

    return ApiRunResult(
        run_time=run_time,
        domain=(min_lat, max_lat, min_lon, max_lon),
        precision_degrees=ARPEGE_API_PRECISION_DEGREES,
        payload=payload,
        resolved_fields=resolved,
        missing_fields=missing,
        request_count=request_count,
        byte_count=byte_count,
    )
