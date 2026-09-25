"""AMP Edge Gateway — the piece that runs INSIDE the factory.

Nothing in here imports the AMP backend, and nothing in the AMP backend imports
this. That separation is the whole architecture: AMP Cloud must never hold an
open socket into a customer's PLC network, and this package must be installable
on a plant PC that has never heard of FastAPI.

    PLC  ->  adapter  ->  tag mapper  ->  normalizer  ->  local queue  ->  MQTT  ->  AMP

Everything flows outward. The gateway dials AMP; AMP never dials the gateway.
"""
