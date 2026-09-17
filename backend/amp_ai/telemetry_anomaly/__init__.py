"""AMP-native telemetry anomaly scoring (capability C).

Modules (import them directly; this ``__init__`` imports nothing on purpose,
because ``amp_ai.core.purity`` follows package ``__init__`` files and an eager
import here would make every pure module below impure):

  synthetic     independent synthetic machine telemetry for EVALUATION only
  series        5-minute bucket medians and running / not-running labels
  baseline      robust per-state baselines, calibrated window score, minimum data
  build_eval    the held-out evaluation against two baselines, and its gate
  db_telemetry  bounded, tenant-filtered reads of a machine's telemetry
  service       score_machine: tenant check -> consent -> data -> score

WHAT IS LEARNED, AND FROM WHOM
------------------------------
Nothing is trained ahead of time on customer data and nothing is persisted.
Each request fits a baseline from THAT machine's own last 14 days of telemetry
(which is learning from tenant data, so ``service.score_machine`` refuses
without the tenant's ``telemetry_baseline`` consent), scores the last hour
against it, and throws the baseline away. The committed evaluation JSON is the
only artifact; it was produced from synthetic machines.
"""
