"""Exceptions raised by REQL statements."""
from __future__ import annotations


class REQLError(ValueError):
    """Base class for REQL parse/evaluation errors."""


class REQLSyntaxError(REQLError):
    """The query statement is syntactically invalid."""


class REQLEvaluationError(REQLError):
    """The query statement is valid but cannot be evaluated."""
