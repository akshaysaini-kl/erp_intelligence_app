"""Explicit evaluator registry.

The registry maps an ``evaluator_key`` string to a Python callable that is
registered in code. A Business Rule stores only the key. The engine looks the key
up here and never imports, evaluates or executes anything named by database data.
"""

import re
from collections.abc import Callable
from typing import Any

from erp_intelligence.services.business_rules.errors import EvaluatorRegistrationError

EVALUATOR_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


class EvaluatorRegistry:
    """A set of evaluators addressed by key.

    An evaluator is ``callable(context) -> result`` where ``context`` is an
    ``EvaluationContext`` and ``result`` follows the standard result contract.
    """

    def __init__(self) -> None:
        self._evaluators: dict[str, Callable[[Any], Any]] = {}
        self._descriptions: dict[str, str] = {}

    def register(
        self,
        key: str,
        evaluator: Callable[[Any], Any],
        *,
        description: str = "",
        replace: bool = False,
    ) -> None:
        """Register ``evaluator`` under ``key``.

        A key can be registered once. Registering it again raises unless
        ``replace`` is True, so two modules cannot silently shadow each other.
        """
        if not isinstance(key, str) or not EVALUATOR_KEY_PATTERN.fullmatch(key):
            raise EvaluatorRegistrationError(
                f"Invalid evaluator key {key!r}. Use lower-case dotted identifiers, "
                "for example purchase_order.supplier_enabled"
            )
        if not callable(evaluator):
            raise EvaluatorRegistrationError(f"Evaluator for {key!r} is not callable")
        if key in self._evaluators and not replace:
            raise EvaluatorRegistrationError(f"Evaluator key {key!r} is already registered")
        self._evaluators[key] = evaluator
        self._descriptions[key] = description

    def unregister(self, key: str) -> None:
        """Remove ``key``. Removing an unknown key is not an error."""
        self._evaluators.pop(key, None)
        self._descriptions.pop(key, None)

    def get(self, key: Any) -> Callable[[Any], Any] | None:
        """Return the evaluator for ``key``, or None when it is not registered."""
        if not isinstance(key, str):
            return None
        return self._evaluators.get(key)

    def keys(self) -> list[str]:
        return sorted(self._evaluators)

    def describe(self, key: str) -> str:
        return self._descriptions.get(key, "")

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in self._evaluators

    def __len__(self) -> int:
        return len(self._evaluators)


_default_registry: EvaluatorRegistry | None = None


def get_default_registry() -> EvaluatorRegistry:
    """Return the process-wide registry holding every evaluator implemented in code.

    Built-in evaluator modules are imported explicitly here. To add a module of
    evaluators, import it below and call its ``register_all``.
    """
    global _default_registry
    if _default_registry is None:
        # Imported here to avoid a circular import: evaluator modules use the
        # context and result helpers, which do not import the registry.
        from erp_intelligence.services.business_rules.evaluators import purchase_order

        registry = EvaluatorRegistry()
        purchase_order.register_all(registry)
        _default_registry = registry
    return _default_registry
