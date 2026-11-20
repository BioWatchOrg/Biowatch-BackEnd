"""Date de début de protection (`valid_from`) d'un site, à partir de ses champs bruts INPN."""

import datetime
from collections.abc import Mapping

from protected_areas.versions import ProtectedAreaType

# `DATE_CREA` vaut 1982-01-01 pour 43 % des ZNIEFF de type I (valeur par défaut
# de la base, année de lancement de l'inventaire). Même valeur pour les zones du
# shapefile sans fiche dans `REF_ZNIEFF.csv` : elles existent, on ne les perd pas.
ZNIEFF_DEFAULT_VALID_FROM = datetime.date(1982, 1, 1)

# Champs de date d'un site Natura 2000, par ordre de préférence. Les champs
# `*_init` portent la PREMIÈRE désignation ; `spa_date` / `sac_date` portent le
# dernier arrêté (différent pour 548 sites) et ne doivent pas servir ici.
_NATURA_DATE_FIELDS: dict[str, tuple[str, ...]] = {
    "A": ("spa_date_init",),  # ZPS (directive Oiseaux)
    "B": ("sac_date_init", "date_first_con", "date_prop"),  # SIC / ZSC (directive Habitats)
}
_NATURA_ALL_DATE_FIELDS = ("spa_date_init", "sac_date_init", "date_first_con", "date_prop")


def _parse_date(value: str | None) -> datetime.date | None:
    """`AAAA-MM-JJ…` ou `AAAAMMJJ…` -> date ; vide, `NULL` ou illisible -> `None`."""
    text = (value or "").strip()
    if not text or text.upper() == "NULL":
        return None
    digits = text[:10].replace("-", "") if "-" in text[:10] else text[:8]
    try:
        return datetime.datetime.strptime(digits, "%Y%m%d").date()
    except ValueError:
        return None


def site_valid_from(
    area_type: ProtectedAreaType, attrs: Mapping[str, str | None]
) -> datetime.date | None:
    """Date depuis laquelle le site protège son périmètre, ou `None` si la source n'en donne pas.

    - `reserve` : `DateCreaENP` (date de création de la réserve).
    - `natura2000` : première désignation, selon le type du site (`type` = A ou B).
    - `znieff_type1` : `DATE_CREA`, qui est une date d'enregistrement en base et
      non une date scientifique ; à défaut, `ZNIEFF_DEFAULT_VALID_FROM`.
    """
    if area_type == "reserve":
        return _parse_date(attrs.get("DateCreaENP"))
    if area_type == "znieff_type1":
        return _parse_date(attrs.get("DATE_CREA")) or ZNIEFF_DEFAULT_VALID_FROM
    site_type = (attrs.get("type") or "").strip().upper()
    for field in _NATURA_DATE_FIELDS.get(site_type, _NATURA_ALL_DATE_FIELDS):
        parsed = _parse_date(attrs.get(field))
        if parsed is not None:
            return parsed
    return None
