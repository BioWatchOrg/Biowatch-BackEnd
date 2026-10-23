import json
from pathlib import Path

import pystac
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def stac_items() -> list[pystac.Item]:
    """Trois vrais items Planetary Computer (tuile 31UDQ, bandes utiles seulement) :
    deux versions de traitement de la même acquisition du 2023-12-11 (05.10 et
    05.09), et une acquisition du 2022-01-23 en baseline 03.00."""
    features = json.loads((FIXTURES / "items.json").read_text())["features"]
    return [pystac.Item.from_dict(feature) for feature in features]
