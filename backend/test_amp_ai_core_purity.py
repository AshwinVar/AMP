"""AMP-native AI core: standard library only, no dynamic code, no new dependencies.

WHY THIS EXISTS
---------------
The founder's rule for AMP-native AI is that training and inference run on the
Python standard library alone: no numpy, scikit-learn, torch or onnx, nothing
new in requirements.txt, no network, no downloaded weights, and model content is
never pickled, eval'd or exec'd. A rule like that erodes one convenient import
at a time, so it is enforced structurally here, by an AST scan rather than a
text search (a text search for ``eval(`` would match ``build_eval.py``, and a
text search for ``import numpy`` misses ``importlib.import_module("numpy")``).

The scanner lives in ``amp_ai.core.purity`` so every capability's builder can
run the same check on its own files. It follows imports of LOCAL modules
transitively - an ``amp_ai`` package ``__init__`` that eagerly imports a
database module makes every "pure" submodule impure the moment it is imported,
which a per-file scan would miss.

Two guards in this file exist because a structural check that finds nothing
reports all-clear: every scan asserts a minimum number of files, and the helper
is self-tested against files that MUST fail.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_core_purity.py
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile

from amp_ai.core import purity as P

BACKEND = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(BACKEND, "amp_ai", "core")

EXPECTED_CORE = {"__init__", "rng", "linalg", "standardize", "logistic", "multinomial",
                 "text_features", "robust", "split", "metrics", "artifact", "explain",
                 "contracts", "purity", "ledger"}

# requirements.txt as it stood when AMP-native AI was built. Adding ANY runtime
# dependency (numpy, scikit-learn, onnxruntime, ...) must be a deliberate,
# reviewed change to this pin, not a line that slips into a diff.
PINNED_REQUIREMENTS = [
    "fastapi==0.136.1",
    "uvicorn[standard]==0.46.0",
    "sqlalchemy==2.0.49",
    "alembic==1.18.5",
    "psycopg2-binary==2.9.12",
    "paho-mqtt==2.1.0",
    "python-jose[cryptography]==3.5.0",
    "python-dotenv==1.2.2",
    "websockets==16.0",
    "bcrypt==4.2.1",
    "python-multipart==0.0.28",
    "sentry-sdk[fastapi]>=2.0,<3.0",
    # Reviewed 2026-09-18 (ADR-0021, limitation 10): the IANA time-zone database
    # for zoneinfo. Service-contract periods run in the contract's own zone, and
    # Windows and slim images carry no system tz data. Data files only: no
    # compiled code, no network, nothing a model could use.
    "tzdata==2026.2",
]

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))
    if not condition:
        failures.append(f"{label}: {detail}")


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc as e:  # noqa: BLE001
        return e
    return None


def write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path


def core_files():
    return sorted(glob.glob(os.path.join(CORE, "*.py")))


def section_real_code():
    print("\n[amp_ai/core is standard library only]")
    files = core_files()
    names = {os.path.splitext(os.path.basename(f))[0] for f in files}
    check("found exactly the 15 planned core modules", names == EXPECTED_CORE,
          f"missing={sorted(EXPECTED_CORE - names)} extra={sorted(names - EXPECTED_CORE)}")
    check("assert_stdlib_only(core, min_files=15) passes",
          raises(P.PurityViolation, P.assert_stdlib_only, files, min_files=15) is None,
          str(raises(P.PurityViolation, P.assert_stdlib_only, files, min_files=15)))
    check("core imports nothing from the application (models/database/tenancy/ai/...)",
          raises(P.PurityViolation, P.assert_no_forbidden_imports, files,
                 {"models", "database", "tenancy", "ai", "predictive_engine", "sqlalchemy", "fastapi",
                  "oee_contract", "main"},
                 min_files=15) is None)

    everything = sorted(glob.glob(os.path.join(BACKEND, "amp_ai", "**", "*.py"), recursive=True))
    check("all of amp_ai/** has no pickle/marshal/eval/exec (>= 15 files scanned)",
          raises(P.PurityViolation, P.assert_no_dynamic_code, everything, min_files=15) is None,
          str(raises(P.PurityViolation, P.assert_no_dynamic_code, everything, min_files=15)))

    with open(os.path.join(BACKEND, "requirements.txt"), encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh.read().splitlines() if ln.strip() and not ln.strip().startswith("#")]
    check("requirements.txt is unchanged from the pinned copy", lines == PINNED_REQUIREMENTS,
          f"added={sorted(set(lines) - set(PINNED_REQUIREMENTS))} removed={sorted(set(PINNED_REQUIREMENTS) - set(lines))}")

    # The AST scan says what the code imports; this says what importing it DOES,
    # in a clean interpreter, including anything pulled in transitively.
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import importlib\n"
        "for m in %r: importlib.import_module('amp_ai.core.' + m)\n"
        "bad = sorted(n for n in sys.modules if n.split('.')[0] in "
        "{'numpy','scipy','sklearn','torch','onnx','onnxruntime','pandas','sqlalchemy','models','database',"
        "'tenancy','ai','fastapi','requests','urllib3','pickle','socket','http','ssl'})\n"
        "print('BAD=' + ','.join(bad))\n"
    ) % (BACKEND, sorted(EXPECTED_CORE - {"__init__"}))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8")
    check("importing every core module in a fresh interpreter succeeds", r.returncode == 0, r.stderr[-400:])
    check("... and loads no third-party, application, pickle or network module",
          r.stdout.strip() == "BAD=", r.stdout.strip())


def section_self_test():
    print("\n[the scanner refuses what it must refuse]")
    root = tempfile.mkdtemp(prefix="amp_ai_purity_")
    try:
        def violation(fn, *args, **kwargs):
            return raises(P.PurityViolation, fn, *args, **kwargs)

        cases = {
            "import numpy": "import numpy as np\n",
            "from sklearn": "from sklearn.linear_model import LogisticRegression\n",
            "import pickle (stdlib, still forbidden)": "import pickle\n",
            "from urllib.request": "from urllib.request import urlopen\n",
            "import socket": "import socket\n",
            "eval(...)": "x = eval('1 + 1')\n",
            "exec(...)": "exec('x = 1')\n",
            "compile(...)": "code = compile('1', 'f', 'eval')\n",
            "__import__(...)": "os = __import__('os')\n",
            "importlib.import_module(...)": "import importlib\nnp = importlib.import_module('numpy')\n",
            "builtins.eval": "import builtins\nbuiltins.eval('1')\n",
            "an alias of eval": "f = eval\n",
            "an import hidden in a function": "def f():\n    import torch\n",
            "a local non-stdlib module": "import helper_module\n",
        }
        write(root, "helper_module.py", "X = 1\n")
        for label, source in cases.items():
            path = write(root, "case.py", source)
            check(f"refuses {label}", violation(P.assert_stdlib_only, [path], min_files=1) is not None)

        ok = write(root, "ok.py", "import re\nimport math\nfrom collections import Counter\n"
                                  "pattern = re.compile('x')\n")
        check("allows stdlib imports and re.compile (an attribute, not the builtin)",
              violation(P.assert_stdlib_only, [ok], min_files=1) is None,
              str(violation(P.assert_stdlib_only, [ok], min_files=1)))

        dur = write(root, "uses_duration.py", "import helper_module\n")
        check("extra_allowed admits a named local module",
              violation(P.assert_stdlib_only, [dur], min_files=1, extra_allowed={"helper_module"}) is None)
        np_file = write(root, "np.py", "import numpy\n")
        check("extra_allowed cannot admit a forbidden module",
              violation(P.assert_stdlib_only, [np_file], min_files=1, extra_allowed={"numpy"}) is not None)

        # Transitive: a pure-looking module whose sibling (or package __init__) is impure.
        write(root, "pkg/__init__.py", "")
        write(root, "pkg/sibling.py", "import numpy\n")
        leaf = write(root, "pkg/leaf.py", "from .sibling import thing\n")
        check("a relative import of an impure sibling is refused (followed transitively)",
              violation(P.assert_stdlib_only, [leaf], min_files=1) is not None)
        write(root, "pkg2/__init__.py", "from . import db\n")
        write(root, "pkg2/db.py", "import sqlalchemy\n")
        write(root, "pkg2/pure.py", "import math\n")
        user = write(root, "user.py", "from pkg2.pure import x\n")
        check("an eager package __init__ that imports a non-stdlib module is refused",
              violation(P.assert_stdlib_only, [user], min_files=1, extra_allowed={"pkg2"}) is not None)
        write(root, "pkg3/__init__.py", "")
        write(root, "pkg3/a.py", "from .b import y\nimport math\n")
        write(root, "pkg3/b.py", "import re\n")
        check("a pure relative import chain passes",
              violation(P.assert_stdlib_only, [os.path.join(root, "pkg3", "a.py")], min_files=1) is None)
        unresolved = write(root, "pkg3/c.py", "from .missing import z\n")
        check("an unresolvable relative import is refused (it cannot be verified)",
              violation(P.assert_stdlib_only, [unresolved], min_files=1) is not None)
        broken = write(root, "broken.py", "def (:\n")
        check("a file that does not parse is refused", violation(P.assert_stdlib_only, [broken], min_files=1) is not None)

        print("\n[a guard that matches nothing must not report all-clear]")
        check("fewer files than min_files is refused",
              violation(P.assert_stdlib_only, [ok], min_files=2) is not None)
        # Alongside a real file, so min_files is satisfied and only the missing path can fail it.
        check("a path that does not exist is refused, even when enough other files were found",
              violation(P.assert_stdlib_only, [ok, os.path.join(root, "absent.py")], min_files=1) is not None)
        check("... for assert_no_forbidden_imports too",
              violation(P.assert_no_forbidden_imports, [ok, os.path.join(root, "absent.py")], {"numpy"},
                        min_files=1) is not None)
        check("min_files below 1 is a programming error",
              raises(ValueError, P.assert_stdlib_only, [ok], min_files=0) is not None)
        for name in ("one", "two", "three"):
            write(root, f"pure_dir/{name}.py", "import math\n")
        check("a directory is expanded to its .py files (3 found, min_files=3 passes)",
              violation(P.assert_stdlib_only, [os.path.join(root, "pure_dir")], min_files=3) is None)
        check("... and counted honestly (min_files=4 fails)",
              violation(P.assert_stdlib_only, [os.path.join(root, "pure_dir")], min_files=4) is not None)

        print("\n[assert_no_forbidden_imports]")
        m1 = write(root, "m1.py", "import models\n")
        m2 = write(root, "m2.py", "from models import Machine\n")
        m3 = write(root, "m3.py", "import modelsx\n")
        m4 = write(root, "m4.py", "from ai.maintenance import is_reactive\n")
        m5 = write(root, "m5.py", "import amp_ai_extra\n")
        m6 = write(root, "m6.py", "from ai import prediction\n")
        write(root, "bridge.py", "import database\n")
        m7 = write(root, "m7.py", "import bridge\n")
        forbidden = {"models", "database", "ai", "predictive_engine"}
        check("import models refused", violation(P.assert_no_forbidden_imports, [m1], forbidden, min_files=1) is not None)
        check("from models import X refused", violation(P.assert_no_forbidden_imports, [m2], forbidden, min_files=1) is not None)
        check("modelsx is not models", violation(P.assert_no_forbidden_imports, [m3], forbidden, min_files=1) is None)
        check("from ai.maintenance refused", violation(P.assert_no_forbidden_imports, [m4], forbidden, min_files=1) is not None)
        check("amp_ai_extra is not ai", violation(P.assert_no_forbidden_imports, [m5], forbidden, min_files=1) is None)
        check("from ai import prediction refused (as ai.prediction)",
              violation(P.assert_no_forbidden_imports, [m6], {"ai.prediction"}, min_files=1) is not None)
        check("a forbidden import reached through a local module is refused",
              violation(P.assert_no_forbidden_imports, [m7], forbidden, min_files=1) is not None)
        check("min_files enforced", violation(P.assert_no_forbidden_imports, [m3], forbidden, min_files=2) is not None)

        print("\n[assert_no_dynamic_code]")
        d1 = write(root, "d1.py", "import marshal\n")
        d2 = write(root, "d2.py", "exec('1')\n")
        d3 = write(root, "d3.py", "import json\nfrom build_eval import x\n")
        check("marshal refused", violation(P.assert_no_dynamic_code, [d1], min_files=1) is not None)
        check("exec refused", violation(P.assert_no_dynamic_code, [d2], min_files=1) is not None)
        check("a module NAMED build_eval is not eval", violation(P.assert_no_dynamic_code, [d3], min_files=1) is None)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    print("=" * 74)
    print("AMP-native AI core: purity")
    print("=" * 74)
    section_real_code()
    section_self_test()
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


def test_amp_ai_core_purity():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
