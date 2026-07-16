"""Domain-specific exceptions."""

class REQLError(Exception):
    """Base exception for REQL."""


class StorageError(REQLError):
    """Raised when the storage adapter cannot complete an operation."""
