"""AMP AI platform (ADR-0003).

AI as a platform, not scattered scripts: business modules consume ``ai.<service>``
through stable contracts (``ai.base``), and the platform *subscribes to the
domain event stream* (``ai.subscribers``) to turn factory events into
predictions, recommendations and insights — rule-first, LLM-optional, per-tenant.

Concrete engines (today: rule-based) live behind these services, so a scorer can
become an ML model or an LLM later without any consumer changing.
"""
from ai import base, prediction, recommendations, copilot, insights, twin, impact, pulse, roster, trends, downtime, quality, production, oee, inventory, flow, shift, losses, briefing  # noqa: F401  (re-export the platform surface)
