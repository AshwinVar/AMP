"""AMP-native failure-risk model: will this machine start a NEW breakdown in the next 7 days?

Modules (import them directly; this ``__init__`` imports nothing on purpose, so
importing the pure modules never drags a database layer in behind them):

  history        MachineHistory, the ONE lookback truncation, labels
  synthetic      the documented synthetic fleet the base model is trained on
  features       the ONE feature extraction (standard library + duration)
  baseline_rule  adapter: the existing rule scorer on a MachineHistory
  build          generate -> split -> fit -> evaluate once -> artifact
  db_history     read a tenant's MachineHistory from the database (bounded)
  predict        score histories with the pinned, hash-verified artifact

Trained and evaluated on synthetic machines only; not evidence of accuracy on
real plants.
"""
