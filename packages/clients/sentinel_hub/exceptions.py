class SentinelHubAuthError(Exception):
    """Raised when the Sentinel Hub (CDSE) OAuth token request fails."""


class SentinelHubRequestError(Exception):
    """Raised when a Sentinel Hub Statistical API request fails or returns an
    unexpected payload (non-429 error, or a response shape we don't recognize)."""
