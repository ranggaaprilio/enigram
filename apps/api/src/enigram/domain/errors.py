class EnigramError(Exception):
    """Base error with a message the user can act on."""


class ProviderUnavailableError(EnigramError):
    """A model provider is unreachable, rejected the API key, or lacks a configured model."""


class Forbidden(EnigramError):
    """The caller's API key may not make this change."""


class NotFound(EnigramError):
    """The thing asked for does not exist."""
