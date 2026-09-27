"""Every relative link in docs/ points at a file that exists.

THE BUG. Nine cross-references in accepted ADRs pointed at filenames that were
never in the tree. Each one named the right ADR *number* and glossed it
correctly -- ADR-0027 cited "[ADR-0006](0006-ai-native-shell.md) (the machine
twin)" -- but the slug was invented, so the link 404'd on GitHub and resolved
to nothing in an editor. The record said "extends ADR-0006" while the only path
a reader could click was dead: 0006 is `0006-machine-health-twin.md`.

That is the failure mode this file exists for. An ADR's cross-references ARE
its argument -- "extends", "closes a gap named in", "supersedes" -- and a
decision whose lineage cannot be followed has lost the part the format is for.
Nothing in CI read the links, so nine of them rotted quietly; ADRs are
append-only by convention (README: "never edited once Accepted"), which means a
bad path stays bad unless something fails the build over it.

THE RULE (section 1)
--------------------
Every .md under docs/ is walked. A link whose target is relative -- no scheme,
not a bare `#anchor` -- must resolve to a path that exists, relative to the
file holding the link. Both inline `[text](target)` links and reference
definitions `[label]: target` count, images included. A `#fragment` and a
`?query` are stripped before the check (this guard is about files, not
headings), and `%20` is decoded.

WHAT IS NOT A LINK (section 2)
------------------------------
Fenced blocks and inline code spans are blanked before the scan, so a shell
transcript or a prose example that quotes markdown syntax is not a finding.
External schemes (`https:`, `mailto:`) are skipped -- this guard never touches
the network, so it is as fast and as deterministic offline as on CI.

WHY SECTION 3 EXISTS
--------------------
A link checker that stops recognising links passes forever: silently matching
nothing looks exactly like success. Section 3 runs the extractor over a fixture
holding one of each form and asserts what it finds AND what it ignores, so the
guard fails if it goes blind rather than going quiet. Section 4 asserts the
checker reports a genuinely missing file -- the two together pin both
directions, which is the lesson of the digit-search flake: a test that can only
pass is not a test.

Run:  python backend/test_doc_links.py     (exit 0 = pass)
"""
import io
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, ".."))
DOCS = os.path.join(REPO, "docs")

# [text](target), ![alt](target), with an optional "title" or 'title'. The link
# text may itself contain one level of brackets -- "[see [0006]](x.md)" -- which
# is why the text group is not a flat [^]]*.
INLINE_LINK = re.compile(
    r"""!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*"""      # [text]( or ![alt](
    r"""([^()\s]+|<[^<>]*>)"""                        # the target: bare, or <bracketed>
    r"""(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)""",          # optional title
    re.VERBOSE,
)
# A reference definition: [label]: target  "optional title"
REF_DEFINITION = re.compile(r"""^\s{0,3}\[[^\[\]]+\]:\s*(\S+)""")
# A scheme (https:, mailto:, tel:) -- not a path on disk.
HAS_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
INLINE_CODE = re.compile(r"`+[^`]*`+")


def _blank_code(text):
    """Return the lines of `text` with code blanked out, line numbers preserved.

    Fenced blocks (``` or ~~~) and inline code spans become empty, so markdown
    QUOTED as an example is never mistaken for a link the docs actually make.
    Lines are emptied rather than dropped so a finding's line number still
    matches the file a reader opens.
    """
    out, fence = [], None
    for line in text.splitlines():
        stripped = line.lstrip()
        if fence is None:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence = stripped[:3]
                out.append("")
                continue
        else:
            # A fence closes on its own marker; ``` inside a ~~~ block is content.
            if stripped.startswith(fence):
                fence = None
            out.append("")
            continue
        out.append(INLINE_CODE.sub("", line))
    return out


def link_targets(text):
    """Yield (line_number, target) for every link in `text`, code excluded.

    Targets are returned exactly as written. Deciding which ones name a file on
    disk is _is_relative's job, so a caller can see everything that was found.
    """
    for lineno, line in enumerate(_blank_code(text), 1):
        for match in INLINE_LINK.finditer(line):
            yield lineno, match.group(1)
        definition = REF_DEFINITION.match(line)
        if definition:
            yield lineno, definition.group(1)


def _is_relative(target):
    """True if `target` should name a path on disk."""
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    if not target or target.startswith("#"):
        return False            # a heading in this same file
    if target.startswith("//") or HAS_SCHEME.match(target):
        return False            # //cdn.example.com, https:, mailto:
    return True


def _resolve(md_path, target):
    """The filesystem path `target`, written in `md_path`, points at."""
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    path = target.split("#", 1)[0].split("?", 1)[0]
    path = path.replace("%20", " ")
    if not path:
        return None             # was a pure #fragment after all
    if path.startswith("/"):
        # Root-relative: GitHub resolves these from the repository root.
        return os.path.normpath(os.path.join(REPO, path.lstrip("/")))
    return os.path.normpath(os.path.join(os.path.dirname(md_path), path))


def markdown_files(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__")]
        for name in filenames:
            if name.endswith(".md"):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


def broken_links(root):
    """Every (file, line, target) under `root` whose target does not exist."""
    broken = []
    for md_path in markdown_files(root):
        with io.open(md_path, "r", encoding="utf-8") as handle:
            text = handle.read()
        for lineno, target in link_targets(text):
            if not _is_relative(target):
                continue
            resolved = _resolve(md_path, target)
            if resolved is None:
                continue
            if not os.path.exists(resolved):
                broken.append((os.path.relpath(md_path, REPO).replace(os.sep, "/"), lineno, target))
    return broken


failures = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        failures.append(label + (f" [{detail}]" if detail else ""))


# ---------------------------------------------------------------- section 1
def section_1_every_relative_link_resolves():
    print("\n1. Every relative link under docs/ points at something that exists")
    broken = broken_links(DOCS)
    detail = "; ".join(f"{f}:{n} -> {t}" for f, n, t in broken)
    check(f"{len(markdown_files(DOCS))} markdown files, no broken relative links",
          not broken, detail)
    if broken:
        print()
        for f, n, t in broken:
            print(f"    {f}:{n}  ->  {t}   (no such path)")


# ---------------------------------------------------------------- section 2
def section_2_the_docs_surface_is_actually_being_read():
    """A guard over an empty file set is a guard over nothing."""
    print("\n2. The walk reaches the docs it claims to cover")
    files = markdown_files(DOCS)
    check("docs/ holds markdown files to check", len(files) > 10, str(len(files)))
    adrs = [f for f in files if os.sep + "adr" + os.sep in f]
    check("the ADR directory is inside the walk", len(adrs) > 10, str(len(adrs)))
    # The ADRs are where the cross-references live; if this count collapses the
    # guard has stopped reading the thing it was written for.
    total = 0
    for md_path in adrs:
        with io.open(md_path, "r", encoding="utf-8") as handle:
            total += sum(1 for _, t in link_targets(handle.read()) if _is_relative(t))
    check("the ADRs contain relative links the guard can see", total > 50, str(total))


# ---------------------------------------------------------------- section 3
FIXTURE = """# Title

An [ADR-0006](0006-machine-health-twin.md) link and an ![image](img/x.png).
An [external](https://example.com/a.md) and a [mail](mailto:a@b.co) link.
A [heading](#context) and a [file plus heading](0007-read-models.md#decision).
A [titled](target.md "The Title") link and a [spaced](<a b.md>) one.
A [query](page.md?v=2) link and a [root](/docs/README.md) one.

[ref]: ./relative-def.md

```
A fenced [code](never-a-link.md) block.
```

Inline `[code](also-never.md)` span.
"""


def section_3_the_extractor_sees_every_form_and_ignores_the_rest():
    print("\n3. The extractor recognises the real link forms (and only those)")
    found = list(link_targets(FIXTURE))
    targets = [t for _, t in found]

    for name, target in [
        ("a plain relative link", "0006-machine-health-twin.md"),
        ("an image", "img/x.png"),
        ("a link with a #fragment", "0007-read-models.md#decision"),
        ('a link with a "title"', "target.md"),
        ("an <angle-bracketed> target", "<a b.md>"),
        ("a ?query target", "page.md?v=2"),
        ("a /root-relative target", "/docs/README.md"),
        ("a [ref]: definition", "./relative-def.md"),
    ]:
        check(f"finds {name}", target in targets, f"got {targets}")

    for name, target in [
        ("a fenced code block", "never-a-link.md"),
        ("an inline code span", "also-never.md"),
    ]:
        check(f"ignores {name}", target not in targets, f"got {targets}")

    relative = [t for t in targets if _is_relative(t)]
    for name, target in [("an https: URL", "https://example.com/a.md"),
                         ("a mailto: link", "mailto:a@b.co"),
                         ("a bare #anchor", "#context")]:
        check(f"does not treat {name} as a path", target not in relative, f"got {relative}")

    # The fragment and the query are the heading/version, not the filename.
    fixture_dir = os.path.join(DOCS, "adr", "x.md")
    check("strips the #fragment before resolving",
          os.path.basename(_resolve(fixture_dir, "0007-read-models.md#decision")) == "0007-read-models.md")
    check("strips the ?query before resolving",
          os.path.basename(_resolve(fixture_dir, "page.md?v=2")) == "page.md")
    check("decodes %20 in a target",
          os.path.basename(_resolve(fixture_dir, "a%20b.md")) == "a b.md")
    check("resolves a /root-relative target from the repository root",
          _resolve(fixture_dir, "/docs/README.md") == os.path.normpath(os.path.join(REPO, "docs/README.md")))


# ---------------------------------------------------------------- section 4
def section_4_the_checker_fails_on_a_link_that_is_actually_broken(tmp_root=None):
    """Pin the other direction: a missing target must be REPORTED, not passed over.

    Section 1 asserting "no broken links" is only meaningful if this holds --
    otherwise a checker that reported nothing, ever, would look identical.
    """
    print("\n4. A genuinely missing target is reported")
    import shutil
    import tempfile
    root = tmp_root or tempfile.mkdtemp(prefix="doclinks-")
    try:
        nested = os.path.join(root, "adr")
        os.makedirs(nested, exist_ok=True)
        with io.open(os.path.join(nested, "real.md"), "w", encoding="utf-8") as handle:
            handle.write("ok\n")
        with io.open(os.path.join(nested, "index.md"), "w", encoding="utf-8") as handle:
            handle.write("[good](real.md) [bad](0006-ai-native-shell.md) "
                         "[ok-external](https://example.com)\n")
        broken = broken_links(root)
        check("reports exactly the one broken link", len(broken) == 1, str(broken))
        check("names the missing target",
              broken and broken[0][2] == "0006-ai-native-shell.md", str(broken))
        check("names the file and line that holds it",
              broken and broken[0][0].endswith("index.md") and broken[0][1] == 1, str(broken))
    finally:
        if tmp_root is None:
            shutil.rmtree(root, ignore_errors=True)


def main():
    print("=" * 74)
    print("DOC LINKS - every relative link in docs/ resolves")
    print("=" * 74)
    section_1_every_relative_link_resolves()
    section_2_the_docs_surface_is_actually_being_read()
    section_3_the_extractor_sees_every_form_and_ignores_the_rest()
    section_4_the_checker_fails_on_a_link_that_is_actually_broken()

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for failure in failures:
            print(f"  - {failure}")
        print()
        print("A relative link must point at a path that exists. If an ADR moved,")
        print("repoint the link (docs/adr/README.md lists every ADR's real filename).")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_doc_links():
    """The pytest entry point (the coverage job collects module-level test_ functions)."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
