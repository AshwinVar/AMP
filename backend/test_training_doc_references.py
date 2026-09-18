"""Every file the training docs name still exists.

THE DEFECT
----------
docs/training/ is how the founder (and whoever joins) learns where things live:
the module map, the code-change guide, the handbook, the data flows, the course
script. They name files in backticks, 728 of them on 2026-09-18. Nothing checked
that the files still exist. AMP-MODULE-MAP.md listed
`IndustrialGatewaySection.tsx` under Industrial IoT. #389 deleted it (one of 14
unmounted components), and the map kept sending readers to it. It is the same
failure as a stranded mutation anchor (#635, #638): a correct change elsewhere,
a reference that silently stops meaning anything, and nothing that runs to
notice.

THE RULE
--------
Every backticked token in docs/training/*.md that names a source or config file
resolves to a tracked file. It may be written from the repo root, from backend/,
frontend/ or docs/, or as a path suffix of a tracked file (`alembic/versions/...`),
or as a bare file name that some tracked file carries. The only exceptions are
the RECIPE PLACEHOLDERS below: files a recipe tells you to create, which by
construction do not exist. A placeholder that starts existing is flagged too,
so the list cannot silently grow stale.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_training_doc_references.py
"""
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files the docs name as things to CREATE, in "add a module / a metric" recipes.
RECIPE_PLACEHOLDERS = {
    "backend/energy_routes.py", "backend/ai/energy.py", "ai/energy.py",
    "frontend/components/EnergySection.tsx", "backend/test_energy_routes.py",
    "mutate_energy.py", "ai/mymetric.py", "MyMetricSnapshot.tsx", "docs/adr/00XX-title.md",
}

_EXT = r"(?:py|ts|tsx|mjs|js|md|yml|yaml|json|sql|sh|toml|ini|cfg)"
_TOKEN = re.compile(r"`([^`\s]+?\." + _EXT + r")(?::\d+(?:-\d+)?)?`")
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def tracked_files():
    out = subprocess.run(["git", "-C", ROOT, "ls-files"], capture_output=True, text=True)
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.split()
    # No git (a source archive): walk the tree instead.
    found = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "venv", ".next", "__pycache__")]
        found += [os.path.relpath(os.path.join(base, f), ROOT).replace(os.sep, "/") for f in files]
    return found


def resolver(tracked):
    exact = set(tracked)
    names = {os.path.basename(t) for t in tracked}

    def resolves(ref):
        ref = ref[2:] if ref.startswith("./") else ref
        if any(base + ref in exact for base in ("", "backend/", "frontend/", "docs/")):
            return True
        if "/" not in ref:
            return ref in names
        return any(t.endswith("/" + ref) for t in tracked)
    return resolves


def references(text):
    """(line number, reference) for every backticked file name; globs and
    placeholders written with <...> or {...} are patterns, not files."""
    for line_no, line in enumerate(text.splitlines(), 1):
        for m in _TOKEN.finditer(line):
            ref = m.group(1)
            if not any(c in ref for c in "*<{"):
                yield line_no, ref


def main():
    tracked = tracked_files()
    resolves = resolver(tracked)
    docs = sorted(t for t in tracked if t.startswith("docs/training/") and t.endswith(".md"))
    check("the training docs were found", len(docs) >= 5, str(docs))

    dangling, seen_placeholders, total = [], set(), 0
    for doc in docs:
        with open(os.path.join(ROOT, doc), encoding="utf-8") as fh:
            for line_no, ref in references(fh.read()):
                total += 1
                if ref in RECIPE_PLACEHOLDERS:
                    seen_placeholders.add(ref)
                    continue
                if not resolves(ref):
                    dangling.append(f"{doc}:{line_no} {ref}")
    check(f"every file the training docs name exists ({total} references)", not dangling,
          "; ".join(dangling))
    check("the scan read real references (a guard that reads nothing passes everything)",
          total > 500, str(total))
    existing = sorted(p for p in RECIPE_PLACEHOLDERS if resolves(p))
    check("no recipe placeholder has started to exist (then it is a real reference)",
          not existing, str(existing))
    unused = sorted(RECIPE_PLACEHOLDERS - seen_placeholders)
    check("every recipe placeholder is still used by a doc", not unused, str(unused))

    print("\n  controls on the resolver:")
    check("CONTROL: a deleted file does not resolve",
          not resolves("frontend/components/IndustrialGatewaySection.tsx")
          and not resolves("IndustrialGatewaySection.tsx"))
    check("CONTROL: a dot-directory path resolves (.github/workflows/ci.yml)",
          resolves(".github/workflows/ci.yml"))
    check("CONTROL: a bare name and a backend-relative path both resolve",
          resolves("oee_contract.py") and resolves("ai/oee.py"))

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


def test_training_doc_references():
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
