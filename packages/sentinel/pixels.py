"""Calcul pixel à pixel sur les images Sentinel-2 L2A (ADR-004).

Fonctions pures (numpy, sans réseau ni DB) : conversion des valeurs brutes en
réflectance, masque des pixels exploitables, indices spectraux et composite
mensuel. Les tableaux suivent la convention `(date, y, x)` quand ils portent
plusieurs dates, `(y, x)` sinon.

Toute modification d'une constante ou d'une formule de ce module change les
valeurs produites : incrémenter `EXTRACTION_PIPELINE_VERSION`.
"""

import warnings
from collections.abc import Mapping

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.floating]
IntArray = npt.NDArray[np.integer]
BoolArray = npt.NDArray[np.bool_]

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


# Classes SCL (Scene Classification Layer) conservées : 4 végétation, 5 sol nu /
# non végétalisé (dont une bonne partie du bâti), 6 eau. Tout le reste est exclu :
# 0 nodata, 1 saturé/défectueux, 2 zone sombre, 3 ombre de nuage, 7 non classifié,
# 8-9 nuages, 10 cirrus, 11 neige. La classe 7 ne pèse que 0,3 % des pixels à
# Paris (spike ADR-004) : l'exclure ne vide pas les zones urbaines.
SCL_KEEP = (4, 5, 6)

# Réflectance de surface plausible : au-delà, artefact (saturation, reflet).
REFLECTANCE_MAX = 1.5


def valid_pixel_mask(scl: npt.ArrayLike, *reflectances: FloatArray) -> BoolArray:
    """Pixels exploitables : classe SCL conservée ET chaque réflectance dans ]0 ; 1,5].

    `scl` peut arriver en float avec NaN pour le nodata (odc-stac sans
    métadonnées de type) : NaN n'appartient à aucune classe conservée.
    """
    mask = np.isin(np.nan_to_num(np.asarray(scl, dtype=np.float32)), SCL_KEEP)
    for reflectance in reflectances:
        with np.errstate(invalid="ignore"):
            mask &= (reflectance > 0) & (reflectance <= REFLECTANCE_MAX)
    return mask


# Noms des sorties : ce sont aussi les colonnes de satellite_features_by_zone.
INDEX_NAMES = ("ndvi", "ndwi", "ndbi", "swir")


def normalized_difference(a: FloatArray, b: FloatArray) -> FloatArray:
    """(a - b) / (a + b), NaN quand a + b = 0 ou qu'une entrée est NaN."""
    with np.errstate(divide="ignore", invalid="ignore"):
        result = (a - b) / (a + b)
    result[~np.isfinite(result)] = np.nan
    return result


def compute_indices(
    green: FloatArray, red: FloatArray, nir: FloatArray, swir: FloatArray, valid: BoolArray
) -> dict[str, FloatArray]:
    """Indices par pixel et par date, NaN partout où `valid` est faux.

    - NDVI = (B08 - B04) / (B08 + B04) : vigueur de la végétation.
    - NDWI (McFeeters) = (B03 - B08) / (B03 + B08) : eau libre.
    - NDBI = (B11 - B08) / (B11 + B08) : surfaces bâties.
    - SWIR = réflectance B11 (bande brute, pas un indice).
    """
    indices = {
        "ndvi": normalized_difference(nir, red),
        "ndwi": normalized_difference(green, nir),
        "ndbi": normalized_difference(swir, nir),
        "swir": swir.astype(np.float32, copy=True),
    }
    for values in indices.values():
        values[~valid] = np.nan
    return indices


def monthly_composite(
    indices: Mapping[str, FloatArray], valid: BoolArray
) -> tuple[dict[str, FloatArray], IntArray]:
    """Composite mensuel : médiane par pixel sur les dates valides (axe 0).

    On prend la médiane des indices, pas les indices d'une médiane de bandes :
    plus robuste aux résidus de nuages. Renvoie aussi `n_obs`, le nombre de
    dates valides par pixel (0 => médianes NaN). Les doublons d'une même
    acquisition sont absorbés par la médiane.
    """
    with warnings.catch_warnings():
        # Pixel sans aucune date valide : nanmedian renvoie NaN, c'est voulu.
        warnings.filterwarnings("ignore", "All-NaN slice encountered", RuntimeWarning)
        medians = {name: np.nanmedian(values, axis=0) for name, values in indices.items()}
    n_obs = valid.sum(axis=0)
    return medians, n_obs
