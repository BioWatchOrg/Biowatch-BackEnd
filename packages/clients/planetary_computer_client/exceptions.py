class PlanetaryComputerError(Exception):
    """Raised when the Planetary Computer STAC catalog or an image read fails,
    or returns metadata we don't recognize (missing tile, baseline, CRS...)."""
