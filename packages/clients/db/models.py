"""
SQLAlchemy ORM models — single source of truth for the BioWatch schema.
"""

import datetime
import uuid

from geoalchemy2 import Geometry
from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ZonesHex(Base):
    """Spatial source of truth: one row per H3 cell."""

    __tablename__ = "zones_hex"
    __table_args__ = (
        UniqueConstraint("zone_id", "resolution", name="uq_zones_hex_zone_resolution"),
    )

    zone_id: Mapped[str] = mapped_column(String, primary_key=True)
    resolution: Mapped[int] = mapped_column(Integer, nullable=False)
    geom: Mapped[object] = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326), nullable=False
    )
    centroid: Mapped[object | None] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326), nullable=True
    )
    bbox: Mapped[object | None] = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326), nullable=True
    )
    aoi_id: Mapped[str] = mapped_column(String, nullable=False)
    aoi_version: Mapped[str] = mapped_column(String, nullable=False)


class SatelliteFeaturesByZone(Base):
    """Satellite-derived features per zone × bucket × source version."""

    __tablename__ = "satellite_features_by_zone"
    __table_args__ = (
        UniqueConstraint(
            "zone_id", "bucket_id", "source_version", name="uq_satellite_zone_bucket_version"
        ),
    )

    zone_id: Mapped[str] = mapped_column(
        ForeignKey("zones_hex.zone_id", name="fk_satellite_zone"),
        primary_key=True,
    )
    bucket_id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    source_version: Mapped[str] = mapped_column(String, primary_key=True)
    ndvi: Mapped[float | None] = mapped_column(Float, nullable=True)
    ndwi: Mapped[float | None] = mapped_column(Float, nullable=True)
    ndbi: Mapped[float | None] = mapped_column(Float, nullable=True)
    swir: Mapped[float | None] = mapped_column(Float, nullable=True)
    obs_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_pixel_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    cloud_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_valid_data: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OsmFeaturesByZone(Base):
    """Human-pressure features derived from OSM per zone × bucket × source version."""

    __tablename__ = "osm_features_by_zone"
    __table_args__ = (
        UniqueConstraint(
            "zone_id", "bucket_id", "source_version", name="uq_osm_zone_bucket_version"
        ),
    )

    zone_id: Mapped[str] = mapped_column(
        ForeignKey("zones_hex.zone_id", name="fk_osm_zone"),
        primary_key=True,
    )
    bucket_id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    source_version: Mapped[str] = mapped_column(String, primary_key=True)
    building_area_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    road_density_major: Mapped[float | None] = mapped_column(Float, nullable=True)
    road_density_all: Mapped[float | None] = mapped_column(Float, nullable=True)
    urban_landuse_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProtectedAreasByZone(Base):
    """Protected areas applicable to a zone, with temporal validity."""

    __tablename__ = "protected_areas_by_zone"
    __table_args__ = (
        UniqueConstraint(
            "zone_id",
            "protected_area_type",
            "valid_from",
            "source_version",
            name="uq_protected_area_zone_type_validity_version",
        ),
    )

    zone_id: Mapped[str] = mapped_column(
        ForeignKey("zones_hex.zone_id", name="fk_protected_area_zone"),
        primary_key=True,
    )
    protected_area_type: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    valid_from: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    source_version: Mapped[str] = mapped_column(String, primary_key=True)
    valid_to: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    coverage_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SpeciesFeaturesByZone(Base):
    """Biodiversity aggregates per zone × year × source version."""

    __tablename__ = "species_features_by_zone"
    __table_args__ = (
        UniqueConstraint(
            "zone_id", "period_year", "source_version", name="uq_species_zone_period_version"
        ),
    )

    zone_id: Mapped[str] = mapped_column(
        ForeignKey("zones_hex.zone_id", name="fk_species_zone"),
        primary_key=True,
    )
    period_year: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source_version: Mapped[str] = mapped_column(String, primary_key=True)
    species_total_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    species_cr_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    species_en_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    species_vu_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    species_nt_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vulnerability_weighted_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StressScoreByZone(Base):
    """Main business result: ecological stress score per zone × bucket × method."""

    __tablename__ = "stress_score_by_zone"
    __table_args__ = (
        UniqueConstraint(
            "zone_id", "bucket_id", "score_method", name="uq_stress_score_zone_bucket_method"
        ),
    )

    zone_id: Mapped[str] = mapped_column(
        ForeignKey("zones_hex.zone_id", name="fk_stress_score_zone"),
        primary_key=True,
    )
    bucket_id: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    score_method: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    score_global: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_human_pressure: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_vegetation: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_biodiversity: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_dynamic: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_protection: Mapped[float | None] = mapped_column(Float, nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_runs.run_id", name="fk_stress_score_run"), nullable=True, index=True
    )
    computed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class JobRun(Base):
    """Execution traceability for every job run."""

    __tablename__ = "job_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','success','failed','partial')", name="ck_job_runs_status"
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    job_name: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    bucket_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    output_ref: Mapped[str | None] = mapped_column(String, nullable=True)


class JobRunZoneError(Base):
    """Per-zone partial errors for a run, enabling targeted retries."""

    __tablename__ = "job_run_zone_errors"
    __table_args__ = (UniqueConstraint("run_id", "zone_id", name="uq_job_run_zone_error"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("job_runs.run_id", ondelete="CASCADE", name="fk_job_run_zone_errors_run"),
        primary_key=True,
        index=True,
    )
    zone_id: Mapped[str] = mapped_column(
        ForeignKey("zones_hex.zone_id", name="fk_job_run_zone_errors_zone"),
        primary_key=True,
        index=True,
    )
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    failed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
