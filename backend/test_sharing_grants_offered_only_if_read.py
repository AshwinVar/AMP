"""A factory is offered a sharing grant exactly when something in AMP reads it.

THE DEFECT
----------
The Connected Equipment screen and every machine claim offered all seven grants
in oem_sharing.ALL_GRANTS, each a checkbox with a plain-English label. Three of
them (SHARE_ALARMS, SHARE_TELEMETRY, SHARE_MAINTENANCE_HISTORY) were read by
nothing: AMP stores no alarms, keeps no per-installation readings for a
manufacturer and builds no maintenance-history view. Before service contracts,
SHARE_DOWNTIME was a fourth. A factory ticking one consented to sharing that
never happened. The manufacturer was then told 'this customer has not shared
alarms', which blamed the customer for data AMP does not hold. The sales docs
already said, honestly, 'no code reads it'; the consent screen said otherwise.

THE RULE
--------
oem_sharing.OFFERED_GRANTS is what a factory is offered and may grant; the
reserved grants stay in the vocabulary, so a policy that already holds one
still parses. This file keeps the list honest in both directions: every offered
grant has a reader, and every grant with a reader is offered.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_sharing_grants_offered_only_if_read.py
"""
import ast
import glob
import os

import oem_sharing

HERE = os.path.dirname(os.path.abspath(__file__))
failures = []

# Where a grant is DEFINED, labelled or listed rather than read. A reference
# inside one of these statements is not a reader.
DEFINITION_TARGETS = {"ALL_GRANTS", "GRANT_LABELS", "OFFERED_GRANTS"} | set(oem_sharing.ALL_GRANTS)
NOT_PRODUCT = ("test_", "audit_", "mutate_", "verify_")


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _definition_nodes(tree):
    """Nodes inside the module-level statements that define the vocabulary."""
    inside = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in DEFINITION_TARGETS for t in stmt.targets):
            inside.update(id(n) for n in ast.walk(stmt))
    return inside


def readers(sources):
    """{grant: [file:line, ...]} for every reference to a grant that is not its definition.

    A reference is the constant's name (SHARE_X, oem_sharing.SHARE_X) or the exact
    string "SHARE_X". Docstrings and comments are not references."""
    found = {g: [] for g in oem_sharing.ALL_GRANTS}
    for name, text in sources:
        tree = ast.parse(text)
        skip = _definition_nodes(tree) if name == "oem_sharing.py" else set()
        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            ref = None
            if isinstance(node, ast.Name) and node.id in found:
                ref = node.id
            elif isinstance(node, ast.Attribute) and node.attr in found:
                ref = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in found:
                ref = node.value
            if ref:
                found[ref].append(f"{name}:{node.lineno}")
    return found


def product_sources():
    out = []
    for path in sorted(glob.glob(os.path.join(HERE, "*.py"))):
        name = os.path.basename(path)
        if name.startswith(NOT_PRODUCT):
            continue
        with open(path, encoding="utf-8") as fh:
            out.append((name, fh.read()))
    return out


def main():
    print("=" * 74)
    print("1. OFFERED IF AND ONLY IF READ")
    print("=" * 74)
    sources = product_sources()
    check(f"the scan read the backend ({len(sources)} modules, oem_sharing and the contracts included)",
          len(sources) > 50 and {"oem_sharing.py", "oem_routes.py", "service_contracts.py"}
          <= {n for n, _ in sources})
    found = readers(sources)
    read = {g for g, where in found.items() if where}
    offered = set(oem_sharing.OFFERED_GRANTS)
    check("every offered grant is read somewhere", offered <= read,
          f"offered but read by nothing: {sorted(offered - read)}")
    check("every grant something reads is offered", read <= offered,
          f"read but never offered: {sorted(read - offered)} at "
          f"{ {g: found[g][:3] for g in read - offered} }")
    check("the reserved grants are still in the vocabulary, so stored policies parse",
          {"SHARE_ALARMS", "SHARE_TELEMETRY", "SHARE_MAINTENANCE_HISTORY"}
          <= set(oem_sharing.ALL_GRANTS) - offered)

    # CONTROLS: the scan finds a reader, ignores a definition and a docstring.
    probe = [("oem_sharing.py", 'SHARE_ALARMS = "SHARE_ALARMS"\n'),
             ("x.py", 'def f(g):\n    """SHARE_TELEMETRY in a docstring"""\n    return "SHARE_ALARMS" in g\n')]
    got = readers(probe)
    check("CONTROL: a use of a grant counts as a reader; its definition and a docstring do not",
          got["SHARE_ALARMS"] == ["x.py:3"] and got["SHARE_TELEMETRY"] == [], str(got))

    print()
    print("=" * 74)
    print("2. THE ONE CHECK FOR GIVING A GRANT")
    print("=" * 74)
    check("an offered grant is allowed", oem_sharing.refused_grants(["SHARE_OPERATING_HOURS"]) is None)
    reason = oem_sharing.refused_grants(["SHARE_OPERATING_HOURS", "SHARE_ALARMS"])
    check("a reserved grant is refused, saying nothing reads it",
          reason is not None and "SHARE_ALARMS" in reason and "share nothing" in reason, str(reason))
    reason = oem_sharing.refused_grants(["SHARE_EVERYTHING"])
    check("an unknown token is refused as unknown", reason is not None and reason.startswith("Unknown"),
          str(reason))
    check("the offer lists exactly the offered grants, labelled",
          [c["key"] for c in oem_sharing.offered_grant_choices()] == list(oem_sharing.OFFERED_GRANTS)
          and all(c["label"] for c in oem_sharing.offered_grant_choices()))
    check("what a manufacturer is told was not shared never names a reserved grant",
          oem_sharing.not_shared({"SHARE_ALARMS", "SHARE_OPERATING_HOURS"})
          == ["SHARE_DOWNTIME", "SHARE_MACHINE_HEALTH", "SHARE_SERVICE_STATUS"])

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_sharing_grants_offered_only_if_read():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
