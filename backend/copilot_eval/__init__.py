"""The AMP Copilot evaluation harness (ADR-0022).

    fixtures   the three-factory acceptance environment and its oracle
    cases      the question dataset and the adversarial prompts
    fakes      scripted stand-ins for a language model, one behaviour each
    harness    run the Copilot over the dataset and score it

`python -m copilot_eval` prints the scorecard for AMP's own engine and for every
scripted behaviour; test_copilot_eval.py is the CI gate over the same numbers.
"""
