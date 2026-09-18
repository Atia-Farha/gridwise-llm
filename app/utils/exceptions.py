"""Custom application exceptions."""


class GridWiseError(Exception):
    """Base class for all GridWise errors."""


class LLMInterpretationError(GridWiseError):
    """Raised when the LLM call fails or returns unusable output."""


class GuardrailError(GridWiseError):
    """Raised when guardrail validation fails in an unrecoverable way."""


class OptimizationError(GridWiseError):
    """Raised when the LP solver cannot find a feasible solution."""


class ReplayValidationError(GridWiseError):
    """Raised when the post-solve replay check detects a violated constraint."""
