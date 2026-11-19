"""Exceptions used inside the Business Rules Engine.

None of these is a business rule failure. A rule that does not pass returns a FAIL
(or NEEDS_INPUT, NEEDS_CONFIRMATION) result. These exceptions mean the rule could
not be evaluated, and the engine reports them as ERROR results.
"""


class BusinessRuleError(Exception):
    """Base class for engine errors."""


class RuleConfigurationError(BusinessRuleError):
    """A rule, assignment or parameter set is malformed or incomplete."""


class RuleDataError(BusinessRuleError):
    """ERP data needed by a rule could not be retrieved or is not trustworthy.

    ``code`` is a short machine-readable reason, for example ``permission_denied``,
    ``request_invalid``, ``unresolved``, ``incomplete`` or ``field_unavailable``.
    """

    def __init__(self, message: str, *, code: str = "data_access") -> None:
        super().__init__(message)
        self.code = code


class EvaluatorContractError(BusinessRuleError):
    """An evaluator returned something that is not a valid standard result."""


class EvaluatorRegistrationError(BusinessRuleError):
    """An evaluator could not be registered."""
