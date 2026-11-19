"""Generic Business Rules Engine.

Public entry points:

* ``BusinessRulesEngine.evaluate`` runs the rules assigned to a target DocType and
  operation and returns a structured report. It never creates, changes or submits
  an ERP document.
* ``get_default_registry`` returns the registry of evaluators implemented in code.

See ``BUSINESS_RULES_ENGINE.md`` at the app root for how to add a rule and an
evaluator.
"""
