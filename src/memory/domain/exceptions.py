"""Domain-specific exceptions."""

class REQLError(Exception):
    """Base exception for REQL."""


class StorageError(REQLError):
    """Raised when the storage adapter cannot complete an operation."""


class ValidationError(REQLError):
    """Raised when a node, edge or query is invalid."""


class NotFoundError(REQLError):
    """Raised when a requested graph object does not exist."""
