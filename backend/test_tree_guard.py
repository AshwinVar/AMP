"""tree_guard has to actually catch the edit it was written for.

tree_guard.py exists because a read-only investigation agent edited
`backend/oem_sharing.py` — disabling a consent gate — and did not restore it.
An instruction not to edit had already been ignored once, so the control is a
hash manifest rather than a stronger instruction.

A control nobody tests is the same shape as the instruction it replaced: it
looks like protection and is not measured. These tests replay the failure —
snapshot, modify one tracked file, verify — against a throwaway git repository,
never the real one. `REPO` and `MANIFEST` are module globals precisely so they
can be pointed somewhere harmless here.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_tree_guard.py
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

import tree_guard

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def _git(repo, *args):
    return subprocess.run(["git"] + list(args), cwd=repo, capture_output=True,
                          text=True)


def _fixture_repo():
    """A real git repo with three tracked files, in a temp directory.

    A real one, not a stub: tree_guard shells out to `git ls-files`, so a fake
    would test the mock rather than the tool.
    """
    repo = tempfile.mkdtemp(prefix="amp_tg_test_")
    _git(repo, "init", "-q")
    for name, body in (("a.py", "print('a')\n"),
                       ("b.py", "print('b')\n"),
                       ("sub/c.txt", "c\n")):
        path = os.path.join(repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        io.open(path, "w", encoding="utf-8", newline="").write(body)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return repo


class _Pointed:
    """Point tree_guard's module globals at the fixture, and put them back."""

    def __init__(self, repo):
        self.repo = repo
        self.manifest = os.path.join(repo, "manifest.json")

    def __enter__(self):
        self._old = (tree_guard.REPO, tree_guard.MANIFEST)
        tree_guard.REPO, tree_guard.MANIFEST = self.repo, self.manifest
        return self

    def __exit__(self, *exc):
        tree_guard.REPO, tree_guard.MANIFEST = self._old
        return False


def _check_verify_without_a_snapshot_is_an_error_not_a_pass():
    """The dangerous failure mode: no manifest reported as "clean"."""
    repo = _fixture_repo()
    try:
        with _Pointed(repo):
            check("verify with no snapshot exits 2, not 0", tree_guard.verify() == 2)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def _check_an_untouched_tree_is_clean():
    repo = _fixture_repo()
    try:
        with _Pointed(repo) as p:
            check("snapshot succeeds", tree_guard.snapshot() == 0)
            check("the manifest lists all three tracked files",
                  len(json.load(io.open(p.manifest, encoding="utf-8"))["files"]) == 3)
            check("verify on an untouched tree is CLEAN (exit 0)",
                  tree_guard.verify() == 0)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def _verify_output():
    """Run verify() and return (exit code, what it printed).

    The exit code alone is too weak an assertion. It says something moved; the
    whole value of the tool is saying WHICH file and WHAT happened to it, and an
    exit-code-only test let a real misclassification through — see
    _check_a_deleted_file_is_reported_as_deleted.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = tree_guard.verify()
    return rc, buf.getvalue()


def _check_the_edit_that_caused_this_tool_is_caught():
    """Replay of the real event: one tracked file silently modified."""
    repo = _fixture_repo()
    try:
        with _Pointed(repo):
            tree_guard.snapshot()
            # The shape of the original stray edit: a consent check replaced by
            # a constant. One line, in a file nobody was working on.
            io.open(os.path.join(repo, "b.py"), "w", encoding="utf-8",
                    newline="").write("if True:\n    print('b')\n")
            rc, out = _verify_output()
            check("verify exits non-zero on a modified tracked file", rc == 1)
            check("...and names it under MODIFIED",
                  "MODIFIED (1)" in out and "b.py" in out, out.strip()[:110])
            check("...and does not also claim it was deleted",
                  "DELETED" not in out, out.strip()[:110])
            check("...and leaves the untouched files unmentioned",
                  "a.py" not in out, out.strip()[:110])
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def _check_a_deleted_file_is_reported_as_deleted():
    """A deletion must be reported as a deletion, and only once.

    This started as `rc == 1` and nothing else, and it PASSED while
    tree_guard listed every deleted file under MODIFIED as well: _digest()
    returns None for a missing file, and `None != <hash>` put it in `changed`
    too. Mutation testing exposed it — deleting the `deleted` branch outright
    changed no result — which is the tell for an assertion that is too weak
    rather than a branch that is redundant.
    """
    repo = _fixture_repo()
    try:
        with _Pointed(repo):
            tree_guard.snapshot()
            os.remove(os.path.join(repo, "a.py"))
            rc, out = _verify_output()
            check("verify exits non-zero on a deleted tracked file", rc == 1)
            check("...and reports it as DELETED",
                  "DELETED (1)" in out and "a.py" in out, out.strip()[:110])
            check("...and NOT also as MODIFIED",
                  "MODIFIED" not in out, out.strip()[:110])
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def _check_a_newly_tracked_file_is_caught():
    repo = _fixture_repo()
    try:
        with _Pointed(repo):
            tree_guard.snapshot()
            io.open(os.path.join(repo, "d.py"), "w", encoding="utf-8",
                    newline="").write("print('d')\n")
            _git(repo, "add", "d.py")
            check("verify exits non-zero when a file joins the index",
                  tree_guard.verify() == 1)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def _check_an_untracked_scratch_file_is_ignored():
    """Only TRACKED files are the subject. A scratch file is not a stray edit,
    and a guard that shouts about every temp file gets switched off."""
    repo = _fixture_repo()
    try:
        with _Pointed(repo):
            tree_guard.snapshot()
            io.open(os.path.join(repo, "notes.txt"), "w", encoding="utf-8",
                    newline="").write("scratch\n")
            check("an untracked file leaves the tree CLEAN", tree_guard.verify() == 0)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def _check_digest_is_content_based_and_survives_a_rewrite():
    """Byte-identical content must hash the same, or every save would look like
    an edit and the signal would be worthless."""
    repo = _fixture_repo()
    try:
        with _Pointed(repo):
            before = tree_guard._digest("a.py")
            body = io.open(os.path.join(repo, "a.py"), encoding="utf-8",
                           newline="").read()
            io.open(os.path.join(repo, "a.py"), "w", encoding="utf-8",
                    newline="").write(body)
            check("re-writing identical bytes does not change the digest",
                  tree_guard._digest("a.py") == before)
            check("a missing file digests as None, rather than raising",
                  tree_guard._digest("nope.py") is None)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def _check_the_cli_reports_usage_rather_than_guessing():
    """`python tree_guard.py` with no verb must not default to either action —
    guessing `verify` would print a verdict nobody asked for."""
    r = subprocess.run([sys.executable, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "tree_guard.py")],
        capture_output=True, text=True, timeout=60)
    check("a bare invocation exits 2 and prints usage",
          r.returncode == 2 and "usage:" in r.stdout, r.stdout.strip()[:80])


def main():
    print("=" * 74)
    print("TREE GUARD — the control that replaced an ignored instruction")
    print("=" * 74)
    for fn in (_check_verify_without_a_snapshot_is_an_error_not_a_pass,
               _check_an_untouched_tree_is_clean,
               _check_the_edit_that_caused_this_tool_is_caught,
               _check_a_deleted_file_is_reported_as_deleted,
               _check_a_newly_tracked_file_is_caught,
               _check_an_untracked_scratch_file_is_ignored,
               _check_digest_is_content_based_and_survives_a_rewrite,
               _check_the_cli_reports_usage_rather_than_guessing):
        print(f"\n{fn.__name__}")
        fn()

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


def test_tree_guard():
    """The pytest entry point.

    The scenarios above are `_check_*`, not `test_*`, deliberately: pytest would
    call them one by one, and `check()` RECORDS a failure rather than raising —
    so a broken guard would report green, which is the exact failure mode this
    file exists to prevent in tree_guard itself.
    """
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
