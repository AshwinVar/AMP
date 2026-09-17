"""AMP-native AI core: standard-library numerical primitives shared by every capability.

Modules (import them directly; this ``__init__`` imports nothing on purpose):

  rng            seeded, stream-separated randomness; blake2b feature hashing
  linalg         dense Gauss-Jordan solve / invert for small systems
  standardize    feature standardisation with training-median imputation
  logistic       binary logistic regression (Newton/IRLS) and Platt calibration
  multinomial    sparse multinomial logistic regression (full-batch GD)
  text_features  text normalisation and signed hashed n-gram features
  robust         median/MAD baselines, Ledoit-Wolf covariance, empirical calibration
  split          leakage-proof entity/time and group splits
  metrics        ROC-AUC, PR-AUC, precision@k, Brier, ECE, F1, bootstrap, McNemar
  artifact       JSON model artifacts verified against a hash pinned in code
  explain        per-feature contributions of a linear model
  ledger         counts test-set evaluations so a test set adopts at most once
  contracts      the frozen types the integration layer codes against
  purity         AST guards: stdlib only, no forbidden imports, no dynamic code

Rules every module here follows:
  * standard library only (enforced by test_amp_ai_core_purity.py);
  * floats and lists, no hidden global state, never the built-in ``hash()``;
  * inputs are validated loudly: a NaN, a missing feature or an empty split
    raises instead of quietly becoming a number.
"""
