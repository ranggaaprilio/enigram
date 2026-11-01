class EnigramError(Exception):
    """Base error with a message the user can act on."""


class ProviderUnavailableError(EnigramError):
    """A model provider is unreachable, rejected the API key, or lacks a configured model."""
