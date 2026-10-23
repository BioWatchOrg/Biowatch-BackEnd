"""Calcul pixel à pixel sur les images Sentinel-2 L2A (ADR-004).

Fonctions pures (numpy, sans réseau ni DB) : conversion des valeurs brutes en
réflectance, masque des pixels exploitables, indices spectraux et composite
mensuel. Les tableaux suivent la convention `(date, y, x)` quand ils portent
plusieurs dates, `(y, x)` sinon.

Toute modification d'une constante ou d'une formule de ce module change les
valeurs produites : incrémenter `EXTRACTION_PIPELINE_VERSION`.
"""

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.floating]

# Baseline de traitement ESA à partir de laquelle les produits L2A portent un
# offset de -1000 DN (BOA_ADD_OFFSET), soit -0.1 en réflectance. Planetary
# Computer ne le déclare pas dans les métadonnées STAC (`raster:bands`) : on
# l'applique par règle, d'après `s2:processing_baseline`.
OFFSET_BASELINE = (4, 0)
L2A_OFFSET = -0.1
L2A_SCALE = 0.0001


def offset_for_baseline(baseline: str) -> float:
    """Offset de réflectance à appliquer à un produit L2A selon sa baseline.

    `baseline` est la valeur STAC `s2:processing_baseline`, ex. "02.12", "05.11".
    """
    major, minor = (int(part) for part in baseline.split("."))
    return L2A_OFFSET if (major, minor) >= OFFSET_BASELINE else 0.0


def dn_to_reflectance(
    dn: npt.ArrayLike, scale: float = L2A_SCALE, offset: float = 0.0
) -> FloatArray:
    """Valeurs brutes (DN) -> réflectance en float32.

    DN = 0 est le nodata Sentinel-2 ; NaN apparaît quand le chargeur a déjà
    converti le nodata (odc-stac sans métadonnées de type). Les deux donnent NaN.
    """
    raw = np.asarray(dn, dtype=np.float32)
    reflectance = raw * np.float32(scale) + np.float32(offset)
    reflectance[np.isnan(raw) | (raw == 0)] = np.nan
    return reflectance
