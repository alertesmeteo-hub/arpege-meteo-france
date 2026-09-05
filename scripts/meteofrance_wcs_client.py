#!/usr/bin/env python3
"""Client générique pour les services WCS 2.0.1 du portail API Météo-France.

Ce module ne connaît rien d'ARPEGE en particulier : il sait parler WCS 2.0.1
(GetCapabilities / DescribeCoverage / GetCoverage) au portail
``public-api.meteofrance.fr`` avec l'authentification par clé d'application
(en-tête ``apikey``), telle que documentée sur
https://portail-api.meteofrance.fr/web/fr/api/arpege et implémentée par le
client communautaire MAIF/meteole (vérifié le 2026-09-05 en lisant
``meteole/src/meteole/clients.py`` et ``meteole/src/meteole/forecast.py`` sur
GitHub — c'est actuellement la meilleure source publique de requêtes WCS
Météo-France réellement fonctionnelles, avec ``hacf-fr/meteofrance-api`` et
``CyrilJl/MeteoFetch``).

Éléments confirmés par cette lecture (mais PAS testés ici faute de clé API
réelle dans cet environnement — cf. README/rapport) :

- URL de base : ``https://public-api.meteofrance.fr/public/``
- Authentification par clé applicative : en-tête HTTP ``apikey: <clé>``
  (pas de flux OAuth2 nécessaire quand on utilise directement une clé
  d'application créée sur le portail ; le portail propose *aussi* un flux
  ``/token`` avec ``application_id`` pour les jetons applicatifs classiques,
  mais ce n'est pas le chemin utilisé ici).
- Chemin d'un service : ``<modèle>/<version_api>/wcs/<entry_point>/<opération>``,
  par ex. ``arpege/1.0/wcs/MF-NWP-GLOBAL-ARPEGE-005-EURAT-WCS/GetCoverage``.
- ``GetCapabilities`` : paramètres ``service=WCS&version=2.0.1&language=eng``.
- ``DescribeCoverage`` : paramètre supplémentaire ``coverageid=<id>``.
- ``GetCoverage`` : ``coverageid``, ``format=application/wmo-grib`` (GRIB2
  brut, directement lisible par eccodes — confirmé par le code MAIF/meteole
  qui décode ensuite ce flux avec cfgrib/eccodes) et des ``subset`` répétés :
  ``pressure(850)``, ``height(2)``, ``time(<secondes depuis le run>)``,
  ``lat(min,max)``, ``lon(min,max)``.
- Un ``coverageId`` a la forme ``<INDICATEUR>___<RUN>Z[_<INTERVALLE>]``, par
  exemple ``TEMPERATURE__ISOBARIC_SURFACE___2026-09-05T00.00.00Z`` (le run est
  séparé par un triple underscore, l'intervalle éventuel par un underscore
  simple après le ``Z``).

Ce qui N'EST PAS confirmé empiriquement (aucune clé API disponible dans cet
environnement de développement) : les noms exacts des indicateurs pour
ARPEGE-005-EURAT (ils suivent vraisemblablement le même schéma que ceux
observés dans meteole/AROME — ``TEMPERATURE__ISOBARIC_SURFACE``,
``U_COMPONENT_OF_WIND__ISOBARIC_SURFACE``, etc. — mais seule une vraie requête
``GetCapabilities`` avec une clé valide permet de les lister avec certitude).
C'est pourquoi ``arpege_api_source.py`` interroge toujours ``GetCapabilities``
en premier et résout les indicateurs par correspondance de motifs plutôt que
de coder en dur une liste qui pourrait être fausse.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests
import xmltodict

LOGGER = logging.getLogger("arpege.meteofrance_wcs")

DEFAULT_BASE_URL = "https://public-api.meteofrance.fr/public/"
WCS_VERSION = "2.0.1"


class MeteoFranceWCSError(RuntimeError):
    """Erreur générique lors d'un appel WCS Météo-France."""


class MeteoFranceWCSAuthError(MeteoFranceWCSError):
    """Clé API manquante, invalide ou expirée (HTTP 401/403)."""


@dataclass(frozen=True)
class CoverageSummary:
    coverage_id: str
    indicator: str
    run_text: str
    interval: str
    title: str


@dataclass(frozen=True)
class CoverageDomain:
    forecast_horizons_s: tuple[int, ...]
    heights_m: tuple[int, ...]
    pressures_hpa: tuple[int, ...]
    min_latitude: float
    max_latitude: float
    min_longitude: float
    max_longitude: float


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


class MeteoFranceWCSClient:
    """Client HTTP WCS 2.0.1 minimal, autour de ``requests``.

    Ne connaît qu'un seul "entry point" WCS à la fois (ex. un domaine/résolution
    ARPEGE donné) ; instancier un client par domaine interrogé.
    """

    def __init__(
        self,
        api_key: str,
        model_base_path: str,
        entry_point: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        session: requests.Session | None = None,
        timeout: tuple[float, float] = (15.0, 120.0),
        max_retries: int = 2,
        retry_delay_seconds: float = 3.0,
        user_agent: str = "alertes-meteo.com/arpege-meteofrance-france/1.2",
    ) -> None:
        if not api_key:
            raise MeteoFranceWCSAuthError("METEOFRANCE_API_KEY est vide ou absente")
        self.base_url = base_url.rstrip("/") + "/"
        self.model_base_path = model_base_path.strip("/")
        self.entry_point = entry_point.strip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.session = session or requests.Session()
        self.session.headers.update({"apikey": api_key, "User-Agent": user_agent})

    @property
    def _service_path(self) -> str:
        return f"{self.model_base_path}/{self.entry_point}"

    def _request(self, operation: str, params: dict[str, Any]) -> requests.Response:
        url = self.base_url + f"{self._service_path}/{operation}"
        query = {"service": "WCS", "version": WCS_VERSION, **params}
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(url, params=query, timeout=self.timeout)
            except requests.RequestException as error:
                last_error = error
                LOGGER.warning(
                    "Erreur réseau WCS %s (%s/%s) : %s",
                    operation,
                    attempt,
                    self.max_retries,
                    error,
                )
                time.sleep(self.retry_delay_seconds * attempt)
                continue

            if response.status_code == 200:
                return response
            if response.status_code in (401, 403):
                raise MeteoFranceWCSAuthError(
                    f"Authentification refusée par l'API Météo-France "
                    f"({response.status_code}) sur {url} : {response.text[:500]}"
                )
            if response.status_code == 404:
                raise MeteoFranceWCSError(
                    f"Ressource WCS introuvable ({url}) : {response.text[:500]}"
                )
            if response.status_code in (429, 500, 502, 503, 504):
                LOGGER.warning(
                    "Réponse %s de l'API Météo-France sur %s (%s/%s), nouvelle "
                    "tentative",
                    response.status_code,
                    operation,
                    attempt,
                    self.max_retries,
                )
                time.sleep(self.retry_delay_seconds * attempt)
                continue
            raise MeteoFranceWCSError(
                f"Réponse inattendue {response.status_code} sur {url} : "
                f"{response.text[:500]}"
            )
        raise MeteoFranceWCSError(
            f"Échec de la requête WCS {operation} sur {url} après "
            f"{self.max_retries} tentatives"
        ) from last_error

    def get_capabilities(self) -> list[CoverageSummary]:
        response = self._request("GetCapabilities", {"language": "eng"})
        payload = xmltodict.parse(response.text)
        try:
            contents = payload["wcs:Capabilities"]["wcs:Contents"]
            summaries = _as_list(contents.get("wcs:CoverageSummary"))
        except KeyError as error:
            raise MeteoFranceWCSError(
                f"XML GetCapabilities inattendu ({self._service_path})"
            ) from error

        results: list[CoverageSummary] = []
        for item in summaries:
            coverage_id = str(item.get("wcs:CoverageId") or "")
            if "___" not in coverage_id:
                continue
            indicator, remainder = coverage_id.split("___", 1)
            run_text, _, interval = remainder.partition("Z")
            results.append(
                CoverageSummary(
                    coverage_id=coverage_id,
                    indicator=indicator,
                    run_text=run_text + "Z",
                    interval=interval.lstrip("_"),
                    title=str(item.get("ows:Title") or ""),
                )
            )
        if not results:
            raise MeteoFranceWCSError(
                f"Aucune coverage exposée par {self._service_path} "
                "(GetCapabilities vide ou format inattendu)"
            )
        return results

    def describe_coverage(self, coverage_id: str) -> CoverageDomain:
        response = self._request(
            "DescribeCoverage", {"coverageid": coverage_id}
        )
        payload = xmltodict.parse(response.text)
        try:
            description = payload["wcs:CoverageDescriptions"][
                "wcs:CoverageDescription"
            ]
            grid_axes = _as_list(
                description["gml:domainSet"]["gmlrgrid:ReferenceableGridByVectors"][
                    "gmlrgrid:generalGridAxis"
                ]
            )
            envelope = description["gml:boundedBy"]["gml:EnvelopeWithTimePeriod"]
        except KeyError as error:
            raise MeteoFranceWCSError(
                f"XML DescribeCoverage inattendu pour {coverage_id}"
            ) from error

        def axis_values(name: str) -> tuple[int, ...]:
            for axis in grid_axes:
                info = axis.get("gmlrgrid:GeneralGridAxis", {})
                if str(info.get("gmlrgrid:gridAxesSpanned")) != name:
                    continue
                raw = str(info.get("gmlrgrid:coefficients") or "").split()
                return tuple(int(float(value)) for value in raw)
            return ()

        lower = [float(value) for value in str(envelope["gml:lowerCorner"]).split()]
        upper = [float(value) for value in str(envelope["gml:upperCorner"]).split()]
        axis_labels = str(envelope.get("@axisLabels") or "").split()
        if "long" in axis_labels and "lat" in axis_labels:
            idx_long = axis_labels.index("long")
            idx_lat = axis_labels.index("lat")
        else:
            idx_long, idx_lat = 0, 1
        return CoverageDomain(
            forecast_horizons_s=axis_values("time"),
            heights_m=axis_values("height"),
            pressures_hpa=axis_values("pressure"),
            min_longitude=lower[idx_long],
            min_latitude=lower[idx_lat],
            max_longitude=upper[idx_long],
            max_latitude=upper[idx_lat],
        )

    def get_coverage(
        self,
        coverage_id: str,
        *,
        time_s: int,
        latitude_range: tuple[float, float],
        longitude_range: tuple[float, float],
        pressure_hpa: int | None = None,
        height_m: int | None = None,
        grib_format: str = "application/wmo-grib",
    ) -> bytes:
        subsets: list[str] = []
        if pressure_hpa is not None:
            subsets.append(f"pressure({pressure_hpa})")
        if height_m is not None:
            subsets.append(f"height({height_m})")
        subsets.append(f"time({int(time_s)})")
        subsets.append(f"lat({latitude_range[0]},{latitude_range[1]})")
        subsets.append(f"lon({longitude_range[0]},{longitude_range[1]})")
        params = {
            "coverageid": coverage_id,
            "format": grib_format,
            "subset": subsets,
        }
        response = self._request("GetCoverage", params)
        return response.content

    def latest_run(self, summaries: Iterable[CoverageSummary]) -> str:
        runs = sorted({item.run_text for item in summaries})
        if not runs:
            raise MeteoFranceWCSError("Aucun run disponible dans les capacités WCS")
        return runs[-1]
