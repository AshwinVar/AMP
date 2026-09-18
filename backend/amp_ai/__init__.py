"""AMP-native AI: models AMP trains itself, in pure Python, with no customer data by default.

See docs/adr/0020-amp-native-ai.md (added with the integration phase) for the
authorization chain, the consent model and the adoption gates.

This package ``__init__`` deliberately imports NOTHING. ``ai/__init__.py``
imports about forty modules eagerly, which is why nothing "offline" can import
from ``ai``; importing ``amp_ai.core.metrics`` must never drag a database
module in behind it. ``amp_ai.core.purity`` follows package ``__init__`` files
transitively, so an eager import added here would fail the purity suites.
"""
