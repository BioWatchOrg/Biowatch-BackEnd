import datetime
import logging
import time
from collections.abc import Sequence

import requests
from pystac_client import Client
from pystac_client.exceptions import APIError
from pystac_client.stac_api_io import StacApiIO
from urllib3.util.retry import Retry

from .exceptions import PlanetaryComputerError
from .models import Scene

logger = logging.getLogger(__name__)

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-2-l2a"

# Scènes trop nuageuses écartées dès la recherche : inutile de télécharger une
# image presque entièrement couverte. Les nuages restants sont masqués pixel par
# pixel (SCL). Changer ce seuil change les résultats : incrémenter la version
# du pipeline d'extraction.
MAX_SCENE_CLOUD_COVER = 70.0

_SEARCH_TIMEOUT_S = 60
# Retry réseau de la recherche STAC (POST) : erreurs de connexion, 429 et 5xx,
# avec backoff exponentiel. Respecte Retry-After quand le serveur l'envoie.
_SEARCH_RETRY = Retry(
    total=5,
    backoff_factor=2,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=None,
    respect_retry_after_header=True,
)


def _default_client() -> Client:
    return Client.open(
        STAC_URL,
        stac_io=StacApiIO(max_retries=_SEARCH_RETRY, timeout=_SEARCH_TIMEOUT_S),
    )


def search_scenes(
    bbox: Sequence[float],
    start: datetime.date,
    end: datetime.date,
    *,
    max_cloud_cover: float = MAX_SCENE_CLOUD_COVER,
    client: Client | None = None,
) -> list[Scene]:
    """Scènes Sentinel-2 L2A qui touchent `bbox` entre `start` et `end` (inclus).

    `bbox` = (min_lon, min_lat, max_lon, max_lat) en WGS84. Les URL des images
    ne sont pas signées ici : elles le sont juste avant lecture (`open_tile`),
    pour qu'un jeton ne puisse pas expirer entre la recherche et la lecture.
    Passer `client` pour réutiliser une connexion (ou en test).
    """
    started = time.monotonic()
    # Intervalle STAC fermé : borne de fin explicite en fin de journée, sinon
    # "2025-05-31" pourrait être lu comme minuit et exclure le dernier jour.
    interval = f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z"
    try:
        search = (client or _default_client()).search(
            collections=[COLLECTION],
            bbox=list(bbox),
            datetime=interval,
            query={"eo:cloud_cover": {"lt": max_cloud_cover}},
        )
        items = list(search.items())
    except (APIError, requests.RequestException) as e:
        logger.error(
            "planetary computer search failed",
            extra={
                "event": "planetary_computer.search_error",
                "context": {"interval": interval, "error": str(e)},
            },
        )
        raise PlanetaryComputerError(f"STAC search failed for {interval}: {e}") from e

    scenes = [Scene.from_item(item) for item in items]
    logger.info(
        "planetary computer search done",
        extra={
            "event": "planetary_computer.search_done",
            "context": {
                "interval": interval,
                "n_scenes": len(scenes),
                "n_tiles": len({scene.tile for scene in scenes}),
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        },
    )
    return scenes
