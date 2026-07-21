"""Simulator heartbeat state — a tiny shared holder.

The background simulation loop lives in main (it's an app-lifecycle task), but
its heartbeat is surfaced by the /platform/status endpoint, which lives in
core_routes. Neither may import the other (ADR-0009: route modules never import
main), so the shared state sits here in a leaf module both can import:

  * `tenants`   — which tenants the sim animates (env-driven, read-only);
  * `last_tick` / `tick_count` — updated by the loop each pass, read by status.

The loop mutates via attribute assignment (`sim_state.tick_count += 1`), which
works across modules; a bare `global` would not.
"""
import os

import tenancy

# Tenants whose factories are ANIMATED by the simulator (comma-separated env,
# default: only the founder demo workspace). A customer tenant with real machine
# data must never be ticked — the sim would overwrite real statuses with random
# ones. Opt a demo tenant in via SIM_TENANTS=DEFAULT,APEX.
tenants = [t.strip() for t in os.environ.get("SIM_TENANTS", tenancy.DEFAULT_TENANT).split(",") if t.strip()]

# Sim-loop heartbeat, surfaced (founder-only) in /platform/status so "is the sim
# running, and over which tenants?" is answerable from the app instead of from
# the Railway logs.
last_tick = None
tick_count = 0
