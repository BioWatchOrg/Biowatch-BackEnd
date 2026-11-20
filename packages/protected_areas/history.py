"""Fin de protection (`valid_to`) : report des lignes fermées d'une édition à la suivante.

L'INPN ne publie que l'édition courante de chaque archive et ne donne pas de
date de fin. Une zone couverte dans l'édition précédente et absente de la
nouvelle est donc fermée à la date de la nouvelle édition, et la ligne fermée
est recopiée d'édition en édition : la `source_version` la plus récente porte
à elle seule tout l'historique connu (lignes ouvertes + lignes fermées).
"""

import datetime
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from geo import H3Cell
from protected_areas.coverage import ZoneCoverage

# Part des zones ouvertes de l'édition précédente au-delà de laquelle on refuse
# de fermer : une telle chute vient d'une archive tronquée ou d'une lecture
# ratée, pas d'un déclassement. Ne s'applique qu'à partir de
# `MIN_CLOSURES_FOR_GUARD` fermetures, sinon une petite AOI (3 zones dont 1
# déclassée) bloquerait le job à tort.
DEFAULT_MAX_CLOSED_SHARE = 0.2
MIN_CLOSURES_FOR_GUARD = 10


class TooManyClosuresError(Exception):
    """La nouvelle édition ferait fermer une part anormale des zones de l'édition précédente."""


@dataclass(frozen=True)
class PreviousRow:
    """Une ligne de `protected_areas_by_zone` de l'édition précédente."""

    zone_id: H3Cell
    valid_from: datetime.date
    valid_to: datetime.date | None
    coverage_ratio: float | None


def carry_over_closed_rows(
    current: Mapping[H3Cell, ZoneCoverage],
    previous: Sequence[PreviousRow],
    *,
    closed_on: datetime.date | None,
    failed_zones: Collection[H3Cell] = (),
    max_closed_share: float = DEFAULT_MAX_CLOSED_SHARE,
) -> tuple[list[PreviousRow], int]:
    """Lignes fermées à écrire avec la nouvelle édition, et nombre de fermetures nouvelles.

    - Ligne déjà fermée dans l'édition précédente : reportée telle quelle, sauf
      si la zone est de nouveau couverte avec le même `valid_from` (même clé
      primaire : la ligne ouverte la remplace).
    - Ligne ouverte dont la zone n'est plus couverte : fermée, `valid_to` =
      `closed_on` (date de la nouvelle édition), dernière couverture connue.
    - `closed_on=None` (même archive, seul le calcul a changé) : aucune nouvelle
      fermeture, une zone qui disparaît vient alors du calcul et non de la source.
    - Zone de `failed_zones` (calcul en erreur dans ce run) : jamais fermée, son
      absence du résultat n'est pas une fin de protection.

    Lève `TooManyClosuresError` si plus de `max_closed_share` des lignes
    ouvertes de l'édition précédente seraient fermées.
    """
    failed = set(failed_zones)
    carried: list[PreviousRow] = []
    n_open = 0
    n_closed = 0
    for row in previous:
        covered = current.get(row.zone_id)
        if row.valid_to is not None:
            if covered is None or covered.valid_from != row.valid_from:
                carried.append(row)
            continue
        n_open += 1
        if covered is not None or closed_on is None or row.zone_id in failed:
            continue
        n_closed += 1
        carried.append(
            PreviousRow(
                zone_id=row.zone_id,
                valid_from=row.valid_from,
                valid_to=closed_on,
                coverage_ratio=row.coverage_ratio,
            )
        )

    if n_closed >= MIN_CLOSURES_FOR_GUARD and n_closed > max_closed_share * n_open:
        raise TooManyClosuresError(
            f"{n_closed} zones sur {n_open} ne sont plus couvertes "
            f"(plus de {max_closed_share:.0%}) : archive tronquée ou lecture incomplète ? "
            "Rien n'est écrit. Si la baisse est réelle, relancer avec --max_closed_share 1."
        )
    return carried, n_closed
