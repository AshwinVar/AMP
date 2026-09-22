"""AMP Copilot tools (ADR-0022): typed, authorized reads over the read-models.

`registry` holds the types and the one entry point, `run_tool`; `factory`
registers the tools. Importing this package registers every tool exactly once.
"""
from ai.tools.registry import (REGISTRY, Param, Principal, Tool, catalog, permitted,  # noqa: F401
                               run_tool, validate_args)
from ai.tools import factory  # noqa: F401  (registers the tools)
from ai.tools import actions  # noqa: F401  (registers the action drafts; imports factory's helpers)
