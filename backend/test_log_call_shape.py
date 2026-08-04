"""Guard: a log call must be able to format itself.

THE BUG THIS PINS, and how it hid. Converting 147 print() statements to log
calls mechanically turned every MULTI-ARGUMENT print into a broken format call:

    print("Payload:", raw)      ->      log.info("Payload:", raw)

print() joins its arguments with a space. logging does not — it treats the
first argument as a %-format string and the rest as its arguments, so that call
is `"Payload:" % raw`, which raises

    TypeError: not all arguments converted during string formatting

Eight of these shipped. They passed every standalone test run and they passed
CI, because logging only formats a record when a handler actually emits it —
and at the level those runs used, nothing emitted. They surfaced only when
pytest attached its own capturing handler at level 0 and every record was
formatted for real.

That is the dangerous shape: a latent crash in the code path that reports
crashes, invisible until the logging is actually turned up — which is exactly
what you do when something has gone wrong in production.

WHAT COUNTS AS CORRECT. Either one argument (a pre-formatted string), or a
first argument whose placeholder count matches the number of arguments that
follow. Lazy %-formatting is preferred over f-strings for logging: the
interpolation is skipped entirely when the level filters the record out.

Run:  python backend/test_log_call_shape.py     (exit 0 = pass)
"""
import ast
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))

SKIP_DIRS = {"venv", "alembic", "__pycache__", "node_modules"}

# Names a logger is bound to in this codebase.
LOGGER_NAMES = {"log", "logger", "_log"}
LEVELS = {"debug", "info", "warning", "warn", "error", "critical", "exception"}


def _log_calls(tree):
    """Every (lineno, n_args, first_arg_literal) for a logger call."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute) or fn.attr not in LEVELS:
            continue
        if not (isinstance(fn.value, ast.Name) and fn.value.id in LOGGER_NAMES):
            continue
        first = node.args[0] if node.args else None
        literal = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
        yield node.lineno, len(node.args), literal


def _placeholder_count(fmt):
    """%-placeholders in a format string, ignoring the literal '%%' escape."""
    return fmt.replace("%%", "").count("%")


def _python_files():
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(".py") and not name.startswith("test_"):
                yield os.path.join(root, name)


def test_every_log_call_can_format_itself():
    """A multi-arg log call whose format string has no placeholders raises the
    moment a handler formats it. That is a crash in the reporting path, which
    only fires when you have turned logging up because something is wrong."""
    offenders = []
    for path in _python_files():
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        rel = os.path.relpath(path, HERE)
        for lineno, n_args, literal in _log_calls(tree):
            extra = n_args - 1
            if extra <= 0:
                continue
            if literal is None:
                continue          # a built string; cannot check statically
            placeholders = _placeholder_count(literal)
            if placeholders != extra:
                offenders.append(
                    f"{rel}:{lineno}: {extra} argument(s) but {placeholders} "
                    f"placeholder(s) in {literal!r}")
    assert not offenders, (
        "log call(s) that will raise TypeError when a handler formats them — "
        "logging is not print(), it does not join arguments with a space:\n  "
        + "\n  ".join(offenders))
    print("PASS every logger call's placeholder count matches its arguments")


def test_the_check_actually_detects_the_broken_shape():
    """CONTROL. Without this, a checker that silently matched nothing would
    pass forever and the guard would be decorative. Feed it the exact broken
    call that shipped and require it to be caught."""
    broken = ast.parse('log.info("Payload:", raw)\n')
    found = list(_log_calls(broken))
    assert len(found) == 1, found
    lineno, n_args, literal = found[0]
    assert n_args == 2 and _placeholder_count(literal) == 0, found

    ok = ast.parse('log.info("Payload: %s", raw)\n')
    lineno, n_args, literal = list(_log_calls(ok))[0]
    assert n_args == 2 and _placeholder_count(literal) == 1

    # A literal %% is an escaped percent sign, not a placeholder.
    assert _placeholder_count("100%% done") == 0
    assert _placeholder_count("%s of %s") == 2
    print("PASS the checker catches the broken shape and clears the correct one")


def test_a_formatted_record_really_would_have_raised():
    """Proof the failure is real rather than theoretical: format the record the
    way a handler does and watch the exception the guard exists to prevent."""
    import logging
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "Payload:", ("raw",), None)
    try:
        rec.getMessage()
    except TypeError as e:
        assert "not all arguments converted" in str(e), str(e)
        print("PASS a multi-arg call with no placeholder raises when formatted")
        return
    raise AssertionError("expected TypeError from the unformattable record")


if __name__ == "__main__":
    test_every_log_call_can_format_itself()
    test_the_check_actually_detects_the_broken_shape()
    test_a_formatted_record_really_would_have_raised()
    print("ALL LOG CALL SHAPE TESTS PASSED")
