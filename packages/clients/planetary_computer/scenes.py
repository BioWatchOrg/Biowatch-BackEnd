"""Préparation des scènes avant lecture (fonctions pures, sans réseau)."""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .models import Scene


def _baseline_key(scene: Scene) -> tuple[tuple[int, ...], str]:
    # Comparaison numérique de la baseline ("10.00" > "04.00"), puis l'id de
    # scène (il finit par la date de traitement) pour départager de façon stable.
    return tuple(int(part) for part in scene.baseline.split(".")), scene.scene_id


def deduplicate_scenes(scenes: Iterable[Scene]) -> list[Scene]:
    """Une seule scène par acquisition (tuile + date), la plus récemment traitée.

    Planetary Computer publie certaines acquisitions plusieurs fois, avec des
    versions de traitement différentes (ex. 05.09 et 05.10 en 2022-2023). Les
    garder toutes compterait deux fois la même observation dans la médiane.
    """
    best: dict[tuple[str, object], Scene] = {}
    for scene in scenes:
        key = (scene.tile, scene.acquired_at)
        current = best.get(key)
        if current is None or _baseline_key(scene) > _baseline_key(current):
            best[key] = scene
    return sorted(best.values(), key=lambda s: (s.tile, s.acquired_at))


def group_by_tile(scenes: Iterable[Scene]) -> dict[str, list[Scene]]:
    """Scènes regroupées par tuile MGRS, triées par date d'acquisition."""
    groups: dict[str, list[Scene]] = defaultdict(list)
    for scene in scenes:
        groups[scene.tile].append(scene)
    return {tile: sorted(groups[tile], key=lambda s: s.acquired_at) for tile in sorted(groups)}


def tile_footprints(groups: Mapping[str, Sequence[Scene]]) -> dict[str, BaseGeometry]:
    """Emprise couverte par chaque tuile sur la période (union de ses scènes), WGS84.

    Une scène peut ne couvrir qu'une partie de sa tuile (bord de fauchée du
    satellite) : l'union donne la zone réellement observée sur la période.
    """
    return {
        tile: unary_union([scene.footprint for scene in scenes]) for tile, scenes in groups.items()
    }
