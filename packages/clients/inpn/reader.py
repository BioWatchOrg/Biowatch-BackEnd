"""Lecture des archives INPN : sites d'une emprise, avec leurs champs de date bruts.

Aucune règle métier ici : chaque lecteur renvoie la géométrie des sites (telle
que publiée, en Lambert-93) et les champs bruts dont `packages/protected_areas`
tire la date de début de protection.

Contenu vérifié le 13/11/2026 :

    NATURA2000.zip ─ NATURA_BDD_<mmaaaa>.zip ─┬ SIG_NATURA/natura_sig.shp   (cd_sig)
                                              └ biotop.csv.csv              (cd_sig -> dates)
    EP.zip ─ EP/EP_<aaaa>_<mm>_CNIG.zip ─ sig/sig_metrop.gpkg   (CdENP, AbreviationENP, DateCreaENP)
    ZNIEFF.zip ─┬ ZNIEFF/SIG_ZNIEFF/ZNIEFF1_G2.shp   (ID_MNHN)
                └ ZNIEFF/REF_ZNIEFF.csv              (NM_SFFZN -> DATE_CREA)

Le nom des archives imbriquées porte l'édition et change à chaque mise à jour :
elles sont repérées par leur motif, jamais par leur nom complet.
"""

import csv
import io
import logging
import re
import shutil
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pyogrio.errors
import pyogrio.raw
import pyproj
import shapely
from shapely.geometry.base import BaseGeometry

from .archives import INPN_DATA_URL, InpnError

logger = logging.getLogger(__name__)

# Emprise (xmin, ymin, xmax, ymax) en Lambert-93.
Bbox = tuple[float, float, float, float]

LAMBERT93_EPSG = 2154

# Types d'espaces protégés retenus comme « réserve naturelle » (`AbreviationENP`) :
# nationales, régionales, de Corse. Les périmètres de protection et les réserves
# biologiques n'en font pas partie.
RESERVE_ABBREVIATIONS = frozenset({"rnn", "rnr", "rnc"})

_NATURA_DATE_FIELDS = ("type", "spa_date_init", "sac_date_init", "date_first_con", "date_prop")
_SHAPEFILE_REQUIRED = (".shp", ".shx", ".dbf", ".prj")
_SHAPEFILE_OPTIONAL = (".cpg",)
# Les CSV de l'INPN ont des champs de texte libre de plusieurs centaines de Ko.
_CSV_FIELD_LIMIT = 16 * 1024 * 1024


@dataclass(frozen=True)
class InpnSite:
    """Un site lu dans une archive INPN : géométrie en Lambert-93 et champs bruts."""

    site_id: str
    geom: BaseGeometry
    attrs: Mapping[str, str] = field(default_factory=dict)


def _find_member(zf: zipfile.ZipFile, pattern: re.Pattern[str], what: str) -> str:
    """Premier membre de l'archive dont le NOM DE FICHIER correspond au motif."""
    for name in zf.namelist():
        if pattern.fullmatch(PurePosixPath(name).name):
            return name
    raise InpnError(f"{what} introuvable dans {zf.filename} : le format de l'archive a changé ?")


def _extract_member(zf: zipfile.ZipFile, member: str, dest: Path) -> Path:
    """Copie un membre vers `dest` (nom choisi ici : rien de l'archive ne décide du chemin)."""
    with zf.open(member) as source, open(dest, "wb") as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)
    return dest


def _extract_nested_zip(archive: Path, pattern: re.Pattern[str], dest: Path, what: str) -> Path:
    with zipfile.ZipFile(archive) as outer:
        return _extract_member(outer, _find_member(outer, pattern, what), dest)


def _extract_shapefile(zf: zipfile.ZipFile, basename: str, work_dir: Path) -> Path:
    """Extrait `<basename>.shp` et ses fichiers compagnons, renvoie le chemin du `.shp`."""
    for extension in _SHAPEFILE_REQUIRED + _SHAPEFILE_OPTIONAL:
        pattern = re.compile(re.escape(basename + extension), re.IGNORECASE)
        try:
            member = _find_member(zf, pattern, f"{basename}{extension}")
        except InpnError:
            if extension in _SHAPEFILE_REQUIRED:
                raise
            continue
        _extract_member(zf, member, work_dir / f"{basename}{extension}")
    return work_dir / f"{basename}.shp"


def _read_csv_index(
    zf: zipfile.ZipFile, member: str, key: str, fields: Sequence[str]
) -> dict[str, dict[str, str]]:
    """CSV de l'archive (`;`, UTF-8) -> `{valeur de key: {champ: valeur}}` pour les champs voulus."""
    previous_limit = csv.field_size_limit(_CSV_FIELD_LIMIT)
    try:
        with zf.open(member) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
            reader = csv.DictReader(text, delimiter=";")
            missing = {key, *fields} - set(reader.fieldnames or ())
            if missing:
                raise InpnError(
                    f"Colonnes absentes de {member} : {sorted(missing)} "
                    "(le format de l'archive a changé ?)"
                )
            return {row[key]: {name: row[name] or "" for name in fields} for row in reader}
    finally:
        csv.field_size_limit(previous_limit)


def _read_layer(
    path: Path, bbox: Bbox, columns: Sequence[str]
) -> tuple[list[BaseGeometry], dict[str, list[str]]]:
    """Entités de la couche qui touchent `bbox` : géométries et colonnes demandées (en texte)."""
    try:
        meta, _fids, wkb, data = pyogrio.raw.read(path, bbox=bbox, columns=list(columns))
    except (pyogrio.errors.DataSourceError, pyogrio.errors.DataLayerError) as e:
        raise InpnError(f"Couche illisible ({path.name}) : {e}") from e

    try:
        epsg = pyproj.CRS.from_user_input(meta["crs"]).to_epsg() if meta["crs"] else None
    except pyproj.exceptions.CRSError:
        epsg = None
    if epsg != LAMBERT93_EPSG:
        raise InpnError(
            f"Projection inattendue pour {path.name} : {meta['crs']!r} (attendu EPSG:2154)."
        )

    by_name = dict(zip(meta["fields"], data, strict=True))
    missing = set(columns) - set(by_name)
    if missing:
        raise InpnError(
            f"Champs absents de {path.name} : {sorted(missing)} (le format de l'archive a changé ?)"
        )
    geoms = list(shapely.from_wkb(wkb)) if wkb is not None else []
    values = {
        name: ["" if value is None else str(value) for value in by_name[name]] for name in columns
    }
    return geoms, values


_NATURA_INNER_RE = re.compile(r"NATURA_BDD_.*\.zip", re.IGNORECASE)
_NATURA_BIOTOP_RE = re.compile(r"biotop\.csv(\.csv)?", re.IGNORECASE)
_EP_INNER_RE = re.compile(r"EP_.*_CNIG\.zip", re.IGNORECASE)
_EP_METROP_RE = re.compile(r"sig_metrop\.gpkg", re.IGNORECASE)
_ZNIEFF_REF_RE = re.compile(r"REF_ZNIEFF\.csv", re.IGNORECASE)


def read_natura2000_sites(archive: Path, work_dir: Path, bbox: Bbox) -> list[InpnSite]:
    """Sites Natura 2000 (ZPS et SIC/ZSC) qui touchent `bbox`, avec leurs dates de désignation."""
    try:
        inner = _extract_nested_zip(
            archive, _NATURA_INNER_RE, work_dir / "natura_bdd.zip", "Base Natura 2000"
        )
        with zipfile.ZipFile(inner) as zf:
            shp = _extract_shapefile(zf, "natura_sig", work_dir)
            biotop = _read_csv_index(
                zf, _find_member(zf, _NATURA_BIOTOP_RE, "biotop.csv"), "cd_sig", _NATURA_DATE_FIELDS
            )
    except zipfile.BadZipFile as e:
        raise InpnError(f"Archive Natura 2000 illisible ({archive.name}) : {e}") from e
    geoms, values = _read_layer(shp, bbox, ["cd_sig"])
    return [
        InpnSite(site_id=cd_sig, geom=geom, attrs=biotop.get(cd_sig, {}))
        for cd_sig, geom in zip(values["cd_sig"], geoms, strict=True)
        if geom is not None
    ]


def read_reserve_sites(archive: Path, work_dir: Path, bbox: Bbox) -> list[InpnSite]:
    """Réserves naturelles (RNN, RNR, RNC) de métropole qui touchent `bbox`."""
    try:
        inner = _extract_nested_zip(
            archive, _EP_INNER_RE, work_dir / "ep_cnig.zip", "Base espaces protégés (CNIG)"
        )
        with zipfile.ZipFile(inner) as zf:
            gpkg = _extract_member(
                zf,
                _find_member(zf, _EP_METROP_RE, "sig_metrop.gpkg"),
                work_dir / "sig_metrop.gpkg",
            )
    except zipfile.BadZipFile as e:
        raise InpnError(f"Archive espaces protégés illisible ({archive.name}) : {e}") from e
    geoms, values = _read_layer(gpkg, bbox, ["CdENP", "AbreviationENP", "DateCreaENP"])
    return [
        InpnSite(site_id=code, geom=geom, attrs={"DateCreaENP": created})
        for code, abbreviation, created, geom in zip(
            values["CdENP"], values["AbreviationENP"], values["DateCreaENP"], geoms, strict=True
        )
        if geom is not None and abbreviation.strip().lower() in RESERVE_ABBREVIATIONS
    ]


def read_znieff1_sites(archive: Path, work_dir: Path, bbox: Bbox) -> list[InpnSite]:
    """ZNIEFF continentales de type I de métropole qui touchent `bbox`.

    Une zone sans fiche dans `REF_ZNIEFF.csv` (14 sur 17 199 dans l'édition
    06/2025) est renvoyée sans `DATE_CREA`.
    """
    try:
        with zipfile.ZipFile(archive) as zf:
            shp = _extract_shapefile(zf, "ZNIEFF1_G2", work_dir)
            reference = _read_csv_index(
                zf, _find_member(zf, _ZNIEFF_REF_RE, "REF_ZNIEFF.csv"), "NM_SFFZN", ["DATE_CREA"]
            )
    except zipfile.BadZipFile as e:
        raise InpnError(f"Archive ZNIEFF illisible ({archive.name}) : {e}") from e
    geoms, values = _read_layer(shp, bbox, ["ID_MNHN"])
    return [
        InpnSite(site_id=znieff_id, geom=geom, attrs=reference.get(znieff_id, {}))
        for znieff_id, geom in zip(values["ID_MNHN"], geoms, strict=True)
        if geom is not None
    ]


@dataclass(frozen=True)
class InpnSource:
    """Une archive INPN et la fonction qui en lit les sites d'une emprise."""

    archive_url: str
    read_sites: Callable[[Path, Path, Bbox], list[InpnSite]]


# Clés = valeurs de `protected_areas_by_zone.protected_area_type`.
INPN_SOURCES: dict[str, InpnSource] = {
    "natura2000": InpnSource(f"{INPN_DATA_URL}/natura2000/NATURA2000.zip", read_natura2000_sites),
    "reserve": InpnSource(f"{INPN_DATA_URL}/ep/EP.zip", read_reserve_sites),
    "znieff_type1": InpnSource(f"{INPN_DATA_URL}/znieff/ZNIEFF.zip", read_znieff1_sites),
}
