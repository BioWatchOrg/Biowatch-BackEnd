"""Test de bout en bout contre le vrai Planetary Computer (réseau requis).

Ignoré par défaut (CI et `uv run pytest`). À lancer à la main :

    BIOWATCH_NETWORK_TESTS=1 uv run pytest tests/packages/clients/planetary_computer_client/test_network.py
"""

import datetime
import os

import numpy as np
import pytest
from shapely.geometry import box

from clients.planetary_computer_client import deduplicate_scenes, group_by_tile, search_scenes
from clients.planetary_computer_client.reader import open_tile, read_block

pytestmark = pytest.mark.skipif(
    os.environ.get("BIOWATCH_NETWORK_TESTS") != "1",
    reason="test réseau : BIOWATCH_NETWORK_TESTS=1 pour le lancer",
)

# ~1 km autour de Notre-Dame : quelques blocs seulement, quelques Mo à lire.
PARIS = box(2.343, 48.849, 2.357, 48.857)


def test_search_open_and_read_real_tile():
    scenes = search_scenes(PARIS.bounds, datetime.date(2025, 5, 1), datetime.date(2025, 5, 31))
    assert scenes, "aucune scène trouvée sur Paris en mai 2025"
    groups = group_by_tile(deduplicate_scenes(scenes))
    tile, tile_scenes = next(iter(groups.items()))

    stack = open_tile(tile_scenes, PARIS)
    block = read_block(stack, stack.windows()[0])

    assert stack.epsg == 32631
    assert len(stack.baselines) == len(tile_scenes)
    assert block["red"].shape[0] == len(tile_scenes)
    assert block["red"].dtype == np.uint16
    assert np.any(block["red"] > 0)
    assert set(np.unique(block["scl"])) <= set(range(12))
