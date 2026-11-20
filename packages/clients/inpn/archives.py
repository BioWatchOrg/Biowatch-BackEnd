"""Archives de l'INPN : date de publication et téléchargement.

Depuis la reconstruction du site (cyberattaque de juillet 2025), l'INPN publie
chaque référentiel sous la forme d'une archive à nom fixe, écrasée à chaque
mise à jour (https://inpn.mnhn.fr/referentiels-donnees). L'en-tête HTTP
`Last-Modified` est donc le seul repère d'édition commun à toutes les archives.

Le serveur renvoie 429 après quelques requêtes rapprochées : une seule requête
par archive, et reprise avec attente croissante.
"""

import datetime
import logging
import os
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

INPN_DATA_URL = "https://assets.patrinat.fr/files/donnees"

USER_AGENT = "biowatch-backend (job update_protected_areas)"

# Attente avant chaque nouvel essai (429, 5xx, coupure réseau) : 3 reprises.
RETRY_DELAYS_S = (5.0, 30.0, 120.0)
# Plafond d'un `Retry-After` annoncé par le serveur.
MAX_RETRY_AFTER_S = 300.0
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_CHUNK_SIZE = 1024 * 1024

# Remplaçable dans les tests, pour ne pas attendre réellement.
_sleep = time.sleep


class InpnError(Exception):
    """Archive INPN introuvable, incomplète ou d'un format inattendu."""


class _Retryable(Exception):
    """Échec d'un essai qui mérite une reprise (`delay` imposé par le serveur, s'il y en a un)."""

    def __init__(self, reason: str, delay: float | None = None):
        super().__init__(reason)
        self.delay = delay


def _new_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(60.0),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _raise_if_retryable(response: httpx.Response) -> None:
    if response.status_code not in _RETRYABLE_STATUSES:
        return
    retry_after = response.headers.get("Retry-After", "")
    delay = min(float(retry_after), MAX_RETRY_AFTER_S) if retry_after.isdigit() else None
    raise _Retryable(f"HTTP {response.status_code}", delay)


def _with_retries[T](url: str, attempt: Callable[[], T]) -> T:
    """Rejoue `attempt` après chaque échec réessayable, puis abandonne avec `InpnError`."""
    delays = iter(RETRY_DELAYS_S)
    while True:
        try:
            return attempt()
        except (httpx.TransportError, _Retryable) as e:
            retry_delay = next(delays, None)
            if retry_delay is None:
                raise InpnError(f"Archive INPN injoignable après reprises ({url}) : {e}") from e
            delay = e.delay if isinstance(e, _Retryable) and e.delay is not None else retry_delay
            logger.warning(
                "INPN request failed, retrying",
                extra={
                    "event": "inpn.retry",
                    "context": {"url": url, "error": str(e), "delay_s": delay},
                },
            )
            _sleep(delay)


def archive_last_modified(url: str, client: httpx.Client | None = None) -> datetime.date:
    """Date de publication de l'archive (en-tête `Last-Modified`, UTC), sans la télécharger."""
    own_client = client is None
    http = client or _new_client()

    def attempt() -> datetime.date:
        response = http.head(url)
        _raise_if_retryable(response)
        if response.status_code != 200:
            raise InpnError(f"Archive INPN introuvable ({url}) : HTTP {response.status_code}")
        header = response.headers.get("Last-Modified")
        if not header:
            raise InpnError(f"Archive INPN sans en-tête Last-Modified ({url}).")
        try:
            return parsedate_to_datetime(header).astimezone(datetime.UTC).date()
        except ValueError as e:
            raise InpnError(f"En-tête Last-Modified illisible ({url}) : {header!r}") from e

    try:
        return _with_retries(url, attempt)
    finally:
        if own_client:
            http.close()


def download_archive(url: str, dest_dir: Path, client: httpx.Client | None = None) -> Path:
    """Télécharge l'archive dans `dest_dir` et renvoie son chemin.

    Écrit dans `<nom>.part` puis renomme une fois la taille vérifiée contre
    `Content-Length` : une coupure ne laisse jamais un fichier tronqué qui
    aurait l'air complet.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / os.path.basename(url)
    partial = target.with_name(target.name + ".part")
    own_client = client is None
    http = client or _new_client()
    log_ctx = {"url": url, "path": str(target)}

    def attempt() -> int:
        size = 0
        with http.stream("GET", url) as response:
            _raise_if_retryable(response)
            if response.status_code != 200:
                raise InpnError(f"Archive INPN introuvable ({url}) : HTTP {response.status_code}")
            expected = response.headers.get("Content-Length")
            with open(partial, "wb") as f:
                for chunk in response.iter_bytes(_CHUNK_SIZE):
                    f.write(chunk)
                    size += len(chunk)
        if expected is not None and size != int(expected):
            raise _Retryable(f"taille reçue {size}, attendue {expected}")
        return size

    logger.info(
        "downloading INPN archive", extra={"event": "inpn.download.start", "context": log_ctx}
    )
    started = time.monotonic()
    try:
        size = _with_retries(url, attempt)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
        if own_client:
            http.close()
    logger.info(
        "downloaded INPN archive",
        extra={
            "event": "inpn.download.done",
            "context": {
                **log_ctx,
                "bytes": size,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        },
    )
    return target
