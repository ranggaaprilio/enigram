class EnigramError(Exception):
    """Base error with a message the user can act on."""


class OmlxUnavailableError(EnigramError):
    """OMLX is unreachable, rejected the API key, or lacks a configured model."""
