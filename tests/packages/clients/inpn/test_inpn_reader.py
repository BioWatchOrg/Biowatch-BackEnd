"""Tests pour clients.inpn.reader — lecture d'archives fabriquées sur le modèle des vraies.

Les archives réelles font 130 à 220 Mo : on en reproduit la structure (zips
imbriqués, noms de fichiers, colonnes) avec deux ou trois sites.
"""

import zipfile
from pathlib import Path

import numpy as np
import pyogrio.raw
import pytest
from clients.inpn import (
    INPN_SOURCES,
    InpnError,
    read_natura2000_sites,
    read_reserve_sites,
    read_znieff1_sites,
)
from shapely.geometry import box

# Emprise de lecture (Lambert-93) : les sites « dedans » la touchent, « dehors » non.
BBOX = (0.0, 0.0, 1000.0, 1000.0)
INSIDE = box(100, 100, 200, 200)
INSIDE_2 = box(500, 500, 600, 600)
OUTSIDE = box(5000, 5000, 5100, 5100)


def _write_layer(path, geoms, fields, *, crs="EPSG:2154", layer=None, multi=False):
    pyogrio.raw.write(
        path,
        np.array([g.wkb for g in geoms], dtype=object),
        [np.array(values, dtype=object) for values in fields.values()],
        list(fields),
        geometry_type="MultiPolygon" if multi else "Polygon",
        crs=crs,
        layer=layer,
        promote_to_multi=multi or None,
    )


def _zip(path: Path, members: dict[str, Path | bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            if isinstance(content, Path):
                zf.write(content, name)
            else:
                zf.writestr(name, content)
    return path


def _shapefile_members(src_dir: Path, basename: str, prefix: str) -> dict[str, Path | bytes]:
    return {f"{prefix}{p.name}": p for p in sorted(src_dir.iterdir()) if p.stem == basename}


BIOTOP_CSV = (
    '"type";"sitecode";"cd_sig";"spa_date_init";"sac_date_init";"date_first_con";"date_prop";"site_name"\n'
    '"A";"FR1110025";"I098FR1110025";"2006-04-06";"";"";"";"Étang de Saint-Quentin"\n'
    '"B";"FR1100795";"I098FR1100795";"";"";"2004-12-07";"1999-03-31";"Massif de Fontainebleau"\n'
)


@pytest.fixture
def natura_archive(tmp_path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    _write_layer(
        src / "natura_sig.shp",
        [INSIDE, INSIDE_2, OUTSIDE],
        {
            "cd_sig": ["I098FR1110025", "I098FR1100795", "I098FR9999999"],
            "type_espac": ["ZPS", "SIC", "SIC"],
        },
    )
    inner = _zip(
        tmp_path / "NATURA_BDD_122024.zip",
        {
            "NATURA_BDD_122024/biotop.csv.csv": BIOTOP_CSV.encode("utf-8"),
            **_shapefile_members(src, "natura_sig", "NATURA_BDD_122024/SIG_NATURA/"),
        },
    )
    return _zip(
        tmp_path / "NATURA2000.zip",
        {"NATURA_BDD_122024.zip": inner, "NATURA_PDF_122024.zip": b"PK"},
    )


@pytest.fixture
def ep_archive(tmp_path) -> Path:
    gpkg = tmp_path / "sig_metrop.gpkg"
    _write_layer(
        gpkg,
        [INSIDE, INSIDE_2, box(300, 300, 400, 400), OUTSIDE],
        {
            "CdENP": ["FR3600096", "FR9300030", "FR0000001", "FR3600001"],
            "AbreviationENP": ["rnn", "RNR", "apb", "rnn"],
            "DateCreaENP": ["1989-07-17", "2009-10-22", "2001-01-01", "1961-03-15"],
        },
        layer="sig_metrop",
        multi=True,
    )
    inner = _zip(
        tmp_path / "EP_2026_10_CNIG.zip", {"sig/sig_metrop.gpkg": gpkg, "sig/sig_reu.gpkg": b""}
    )
    return _zip(
        tmp_path / "EP.zip",
        {
            "EP/EP_2026_10_CNIG.zip": inner,
            "EP/EP_2026_10_COVADIS.zip": b"PK",
            "EP/readme.txt": b"READ ME",
        },
    )


REF_ZNIEFF_CSV = (
    "﻿ID_ZNIEFF;NM_SFFZN;LB_ZN;TY_ZONE;DATE_CREA;TX_GENE\n"
    '1525;110001146;BOIS DE LOUZAIE;1;20100608150317;"<p>Texte libre;\n sur deux lignes</p>"\n'
)


@pytest.fixture
def znieff_archive(tmp_path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    _write_layer(
        src / "ZNIEFF1_G2.shp",
        [INSIDE, INSIDE_2],
        {"ID_MNHN": ["110001146", "110001159"], "NOM": ["Bois de Louzaie", "Sans fiche"]},
    )
    _write_layer(src / "ZNIEFF2_G2.shp", [OUTSIDE], {"ID_MNHN": ["110000000"]})
    return _zip(
        tmp_path / "ZNIEFF.zip",
        {
            "ZNIEFF/REF_ZNIEFF.csv": REF_ZNIEFF_CSV.encode("utf-8"),
            **_shapefile_members(src, "ZNIEFF1_G2", "ZNIEFF/SIG_ZNIEFF/"),
            **_shapefile_members(src, "ZNIEFF2_G2", "ZNIEFF/SIG_ZNIEFF/"),
        },
    )


@pytest.fixture
def work_dir(tmp_path) -> Path:
    path = tmp_path / "work"
    path.mkdir()
    return path


# --- Natura 2000 ------------------------------------------------------------------


def test_natura_sites_in_the_bbox_come_with_their_designation_fields(natura_archive, work_dir):
    sites = read_natura2000_sites(natura_archive, work_dir, BBOX)

    assert [site.site_id for site in sites] == ["I098FR1110025", "I098FR1100795"]
    assert sites[0].geom.equals(INSIDE)
    assert sites[0].attrs["type"] == "A"
    assert sites[0].attrs["spa_date_init"] == "2006-04-06"
    assert sites[1].attrs["date_first_con"] == "2004-12-07"


def test_natura_site_absent_from_biotop_has_no_attributes(natura_archive, work_dir):
    sites = read_natura2000_sites(natura_archive, work_dir, (4000.0, 4000.0, 6000.0, 6000.0))
    assert [(site.site_id, dict(site.attrs)) for site in sites] == [("I098FR9999999", {})]


def test_natura_archive_without_its_database_is_rejected(tmp_path, work_dir):
    archive = _zip(tmp_path / "NATURA2000.zip", {"NATURA_PDF_122024.zip": b"PK"})
    with pytest.raises(InpnError, match="Base Natura 2000 introuvable"):
        read_natura2000_sites(archive, work_dir, BBOX)


# --- Réserves --------------------------------------------------------------------


def test_only_nature_reserves_are_kept(ep_archive, work_dir):
    """L'arrêté de biotope (`apb`) est dans le même fichier mais n'est pas une réserve."""
    sites = read_reserve_sites(ep_archive, work_dir, BBOX)

    assert [(site.site_id, site.attrs["DateCreaENP"]) for site in sites] == [
        ("FR3600096", "1989-07-17"),
        ("FR9300030", "2009-10-22"),  # abréviation en majuscules : acceptée
    ]


# --- ZNIEFF ------------------------------------------------------------------------


def test_znieff_type1_reads_its_record_date(znieff_archive, work_dir):
    sites = read_znieff1_sites(znieff_archive, work_dir, BBOX)

    assert [site.site_id for site in sites] == ["110001146", "110001159"]
    assert sites[0].attrs == {"DATE_CREA": "20100608150317"}
    assert sites[1].attrs == {}  # sans fiche dans REF_ZNIEFF.csv : gardée, sans date


def test_znieff_reads_type1_only(znieff_archive, work_dir):
    """La couche de type II est dans la même archive : elle n'est pas lue."""
    sites = read_znieff1_sites(znieff_archive, work_dir, (4000.0, 4000.0, 6000.0, 6000.0))
    assert sites == []


# --- formats inattendus -------------------------------------------------------------


def test_layer_in_another_projection_is_rejected(tmp_path, work_dir):
    src = tmp_path / "src"
    src.mkdir()
    _write_layer(src / "ZNIEFF1_G2.shp", [INSIDE], {"ID_MNHN": ["1"]}, crs="EPSG:4326")
    archive = _zip(
        tmp_path / "ZNIEFF.zip",
        {
            "ZNIEFF/REF_ZNIEFF.csv": REF_ZNIEFF_CSV.encode(),
            **_shapefile_members(src, "ZNIEFF1_G2", ""),
        },
    )
    with pytest.raises(InpnError, match="Projection inattendue"):
        read_znieff1_sites(archive, work_dir, BBOX)


def test_renamed_column_is_reported(tmp_path, work_dir):
    src = tmp_path / "src"
    src.mkdir()
    _write_layer(src / "ZNIEFF1_G2.shp", [INSIDE], {"ID_ZNIEFF": ["1"]})
    archive = _zip(
        tmp_path / "ZNIEFF.zip",
        {
            "ZNIEFF/REF_ZNIEFF.csv": REF_ZNIEFF_CSV.encode(),
            **_shapefile_members(src, "ZNIEFF1_G2", ""),
        },
    )
    with pytest.raises(InpnError, match="Champs absents.*ID_MNHN"):
        read_znieff1_sites(archive, work_dir, BBOX)


def test_csv_without_its_key_column_is_reported(znieff_archive, tmp_path, work_dir):
    with zipfile.ZipFile(znieff_archive) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    members["ZNIEFF/REF_ZNIEFF.csv"] = b"ID_ZNIEFF;LB_ZN\n1;x\n"
    archive = _zip(tmp_path / "ZNIEFF_v2.zip", members)
    with pytest.raises(InpnError, match="Colonnes absentes.*NM_SFFZN"):
        read_znieff1_sites(archive, work_dir, BBOX)


def test_shapefile_missing_a_mandatory_part_is_reported(tmp_path, work_dir):
    archive = _zip(tmp_path / "ZNIEFF.zip", {"ZNIEFF/SIG_ZNIEFF/ZNIEFF1_G2.shp": b""})
    with pytest.raises(InpnError, match=r"ZNIEFF1_G2\.shx introuvable"):
        read_znieff1_sites(archive, work_dir, BBOX)


def test_unreadable_layer_is_reported(tmp_path, work_dir):
    members = {
        f"ZNIEFF/SIG_ZNIEFF/ZNIEFF1_G2{ext}": b"pas un shapefile"
        for ext in (".shp", ".shx", ".dbf", ".prj")
    }
    members["ZNIEFF/REF_ZNIEFF.csv"] = REF_ZNIEFF_CSV.encode()
    with pytest.raises(InpnError, match="Couche illisible"):
        read_znieff1_sites(_zip(tmp_path / "ZNIEFF.zip", members), work_dir, BBOX)


@pytest.mark.parametrize("reader", [read_natura2000_sites, read_reserve_sites, read_znieff1_sites])
def test_corrupted_archive_is_reported(tmp_path, work_dir, reader):
    archive = tmp_path / "cassée.zip"
    archive.write_bytes(b"ceci n'est pas un zip")
    with pytest.raises(InpnError, match="illisible"):
        reader(archive, work_dir, BBOX)


def test_every_protected_area_type_has_a_source():
    from protected_areas import PROTECTED_AREA_TYPES

    assert set(INPN_SOURCES) == set(PROTECTED_AREA_TYPES)
    assert all(source.archive_url.startswith("https://") for source in INPN_SOURCES.values())
