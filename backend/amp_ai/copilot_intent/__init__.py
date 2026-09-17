"""AMP-native copilot intent model: proposes WHICH pillar answers a question, nothing more.

Modules (import them directly; this ``__init__`` imports nothing on purpose,
because ``amp_ai.core.purity`` follows package ``__init__`` files and an eager
import here would make every runtime module impure):

  labels      the fixed label set: the router's 15 route names plus ``__none__``
  corpus      the AMP-authored training corpus, its seeded expansion, family split
              and the leakage check against the repo's evaluation questions
  classifier  loads the pinned JSON artifact and turns a question into a
              ``RouteDecision`` (standard library + amp_ai.core only)
  build       trains, selects hyper-parameters and the threshold on validation,
              evaluates against the keyword router, applies the adoption gate
              and writes the artifact + eval JSON. The only module that touches
              the application: it imports the routing harnesses (and through
              them ``ai.assistant``) inside the evaluation, to run the baseline
              on their own in-memory databases. Nothing at runtime imports it.

The model never sees a tenant, a user or the database. Its output is a name from
the allowlist; ``ai.assistant`` runs the tenant-scoped pillar for that name.
"""
