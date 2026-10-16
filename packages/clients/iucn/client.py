"""Client HTTP minimal pour l'API IUCN Red List v4.

L'API n'expose pas d'export complet : les évaluations se lisent par pages de
100. On ne couvre que ce dont BioWatch a besoin — la version de la Red List et
les évaluations d'un pays — pas l'API entière.

Auth : jeton `IUCN_API_TOKEN` (variable d'environnement), envoyé en en-tête
`Authorization: Bearer`. Le jeton n'est jamais loggé.
"""

import logging
import os
from collections.abc import Iterator
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.iucnredlist.org/api/v4"
TOKEN_VAR = "IUCN_API_TOKEN"
GLOBAL_SCOPE = 1  # évaluation mondiale (vs régionale, ex. Europe)


class IucnApiError(Exception):
    """Erreur d'appel à l'API IUCN (jeton absent, statut HTTP, réponse inattendue)."""


class IucnClient:
    """Lecture de l'API IUCN v4. Utilisable en context manager (ferme la connexion)."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        token = token or os.environ.get(TOKEN_VAR)
        if not token:
            raise IucnApiError(
                f"Jeton IUCN absent : définir {TOKEN_VAR} dans le .env de l'environnement."
            )
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> "IucnClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            response = self._http.get(path, params=params)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.error(
                "IUCN API call failed",
                extra={"event": "iucn.http_error", "context": {"path": path, "error": str(e)}},
            )
            raise IucnApiError(f"Appel IUCN {path} en échec : {e}") from e
        return response

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        """Corps JSON objet, sinon `IucnApiError` (ex. page HTML d'un proxy)."""
        try:
            body = response.json()
        except ValueError as e:
            raise IucnApiError(f"Réponse IUCN non JSON ({response.url.path}).") from e
        if not isinstance(body, dict):
            raise IucnApiError(f"Réponse IUCN inattendue ({response.url.path}) : objet attendu.")
        return body

    @staticmethod
    def _int_header(response: httpx.Response, name: str) -> int:
        """En-tête entier obligatoire : sans lui, on ne sait pas si la liste est complète."""
        try:
            return int(response.headers[name])
        except (KeyError, ValueError) as e:
            raise IucnApiError(
                f"Réponse IUCN sans en-tête `{name}` valide ({response.url.path})."
            ) from e

    def red_list_version(self) -> str:
        """Version courante de la Red List (ex. `"2026-1"`) — sert de `source_version`."""
        body = self._json_object(self._get("/information/red_list_version"))
        version = body.get("red_list_version")
        if not version:
            raise IucnApiError("Réponse IUCN sans `red_list_version`.")
        return str(version)

    def iter_country_assessments(
        self, country: str, scope_code: int = GLOBAL_SCOPE
    ) -> Iterator[dict[str, Any]]:
        """Dernières évaluations des espèces présentes dans `country` (ISO alpha-2).

        Parcourt toutes les pages (en-tête `total-pages`), puis vérifie que le
        nombre d'évaluations reçues égale l'en-tête `total-count` : une liste
        tronquée lève `IucnApiError` au lieu d'être importée comme complète.
        """
        page, total_pages, total_count, received = 1, 1, 0, 0
        while page <= total_pages:
            response = self._get(
                f"/countries/{country}",
                params={"latest": "true", "scope_code": scope_code, "page": page},
            )
            total_pages = self._int_header(response, "total-pages")
            total_count = self._int_header(response, "total-count")
            assessments = self._json_object(response).get("assessments")
            if not isinstance(assessments, list):
                raise IucnApiError(f"Réponse IUCN sans liste `assessments` (page {page}).")
            received += len(assessments)
            yield from assessments
            logger.debug(
                "IUCN page fetched",
                extra={
                    "event": "iucn.page",
                    "context": {"country": country, "page": page, "total_pages": total_pages},
                },
            )
            page += 1
        if received != total_count:
            raise IucnApiError(
                f"Évaluations IUCN incomplètes pour {country} : {received} reçues, "
                f"{total_count} annoncées."
            )
