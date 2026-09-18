"""Wall-clock budgets in tests, and the one place that knows when they mean nothing.

A few suites assert that a model build finishes within N seconds, because a
model nobody can rebuild in CI is a claim, not a measurement. CI's coverage job
runs every suite in one pytest process under coverage's line tracer, which slows
pure-Python arithmetic several-fold: the full failure-risk build took 37 s bare
and 152-160 s traced locally, and 108 s on a CI runner. A time taken under the
tracer measures the tracer, so a budget judged there fails at random (it failed
two of PR #610's coverage runs and passed a third).

THE RULE, for a budget on code timed IN THIS PROCESS:
  * the standalone entry (``python test_X.py``, which the backend job runs on
    every PR) always judges it, so no mistake here can switch a budget off;
  * the pytest entry judges it only when ``traced()`` is False.

A budget on a SUBPROCESS is judged everywhere: the tracer is in this process,
and pytest-cov does not instrument child processes by default
(test_amp_ai_intent_build.py times one).
"""
import sys


def traced():
    """Is a line tracer (coverage, a debugger) instrumenting this process?"""
    monitoring = getattr(sys, "monitoring", None)   # Python 3.12+: coverage's sysmon core
    return sys.gettrace() is not None or bool(
        monitoring and monitoring.get_tool(monitoring.COVERAGE_ID))
