"""Native copilot intent model: the AMP-authored training corpus is deterministic, split without leakage, and not copied from the evaluation questions.

WHY THIS EXISTS
---------------
The intent model learns ONLY from a corpus AMP wrote itself (amp_ai/copilot_intent/
corpus.py): no tenant question is ever logged or learned from. Three things make
its evaluation meaningful, and each is checked here:

1. DETERMINISM. The corpus is expanded from families with a fixed seed, so the
   committed artifact can be rebuilt and its corpus hash re-derived.
2. NO FAMILY LEAKAGE. A family is one "way of asking" (a template with slot
   synonyms plus free phrasings). Near-identical sentences share a family, and a
   family lives wholly in train, validation or test - otherwise the held-out
   score would grade the model on sentences it memorised.
3. NO COPYING FROM THE EVALUATION SETS. The keyword router is compared with the
   model on question sets that already exist in the repo (test_ai_evaluation.py,
   test_ai_routing_holdout.py, test_ai_routing_holdout2.py). If the corpus
   contained those questions, or near-copies, the comparison would be rigged.
   The leakage check refuses any corpus sentence equal to, or with character
   4-gram Jaccard >= 0.6 against, any loaded evaluation question - and asserts it
   actually LOADED more than 100 of them, because a check that loads nothing
   reports all-clear. It never prints a question.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_amp_ai_intent_corpus.py
"""
import hashlib
import os
import re

from amp_ai.core.text_features import normalize_text
from amp_ai.copilot_intent import corpus as K
from amp_ai.copilot_intent import labels as L

BACKEND = os.path.dirname(os.path.abspath(__file__))
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


def section_families():
    print("\n[families]")
    check("validate_families() accepts the shipped corpus", raises(Exception, K.validate_families) is None,
          str(raises(Exception, K.validate_families)))
    per_label = {}
    for fam in K.FAMILIES:
        per_label.setdefault(fam.label, []).append(fam)
    check("every label in LABELS has families (including __none__)", set(per_label) == set(L.LABELS),
          f"missing={sorted(set(L.LABELS) - set(per_label))} unknown={sorted(set(per_label) - set(L.LABELS))}")
    thin = {label: len(f) for label, f in per_label.items() if len(f) < 12}
    check(">= 12 families per label", not thin, str(thin))
    check("MIN_FAMILIES_PER_LABEL is 12", K.MIN_FAMILIES_PER_LABEL == 12)
    few_free = [f.family_id for f in K.FAMILIES if len(f.free) < 3]
    check(">= 3 free phrasings in every family", not few_free, str(few_free[:5]))
    no_template = [f.family_id for f in K.FAMILIES if not f.templates]
    check(">= 1 template in every family", not no_template, str(no_template[:5]))
    ids = [f.family_id for f in K.FAMILIES]
    check("family ids are unique", len(ids) == len(set(ids)))

    print("\n[validate_families refuses a malformed corpus]")
    good = K.FAMILIES
    one = good[0]
    # Leave EXACTLY 11 families for one label (removing a single family is not enough when a label has 13).
    same_label = [f for f in good if f.label == one.label]
    dropped = {f.family_id for f in same_label[:len(same_label) - (K.MIN_FAMILIES_PER_LABEL - 1)]}
    thin_corpus = tuple(f for f in good if f.family_id not in dropped)
    left = sum(1 for f in thin_corpus if f.label == one.label)
    check(f"fixture really leaves 11 families for {one.label!r}", left == K.MIN_FAMILIES_PER_LABEL - 1, str(left))
    check("fewer than 12 families for a label is refused",
          raises(K.CorpusError, K.validate_families, thin_corpus) is not None)
    exactly = tuple(f for f in good if f.family_id not in set(sorted(dropped)[1:]))
    check("exactly 12 families for a label is accepted (the boundary)",
          sum(1 for f in exactly if f.label == one.label) == K.MIN_FAMILIES_PER_LABEL
          and raises(Exception, K.validate_families, exactly) is None)
    broken = K.Family(one.label, one.family_id + "-x", ("how is {nosuchslot}",), {}, ("a b", "c d", "e f"))
    check("a template naming an unknown slot is refused",
          raises(K.CorpusError, K.validate_families, good + (broken,)) is not None)
    foreign = K.Family("drop_tables", "foreign-1", ("x {NOW}",), {}, ("a b", "c d", "e f"))
    check("a family with a label outside LABELS is refused",
          raises(K.CorpusError, K.validate_families, good + (foreign,)) is not None)
    dup = K.Family(one.label, one.family_id, one.templates, dict(one.slots), one.free)
    check("a duplicate family id is refused", raises(K.CorpusError, K.validate_families, good + (dup,)) is not None)
    thin_free = K.Family(one.label, "thin-free", one.templates, dict(one.slots), one.free[:2])
    check("a family with fewer than 3 free phrasings is refused",
          raises(K.CorpusError, K.validate_families, good + (thin_free,)) is not None)


def section_expansion():
    print("\n[expansion]")
    a = K.build_corpus()
    b = K.build_corpus()
    check("build_corpus() is deterministic", a == b)
    check("corpus_sha256 is deterministic", K.corpus_sha256(a) == K.corpus_sha256(b))
    check(f"corpus size is about 3-5k examples ({len(a)})", 3000 <= len(a) <= 5000, str(len(a)))
    per_family = {}
    for ex in a:
        per_family[ex.family] = per_family.get(ex.family, 0) + 1
    check("MAX_PER_FAMILY is 20 (within the planned cap of 40; see corpus.py AUTHORING PROTOCOL 5)",
          K.MAX_PER_FAMILY == 20)
    check("no family exceeds MAX_PER_FAMILY", max(per_family.values()) <= K.MAX_PER_FAMILY,
          str(max(per_family.values())))
    check("every family contributes examples", set(per_family) == {f.family_id for f in K.FAMILIES})
    fams = {f.family_id: f for f in K.FAMILIES}
    missing_free = [f.family_id for f in K.FAMILIES
                    if not {normalize_text(t) for t in f.free if K.sentence_digest(t) not in K.DECONTAMINATED}
                    <= {normalize_text(e.text) for e in a if e.family == f.family_id}]
    check("every free phrasing that is not decontaminated is kept", not missing_free, str(missing_free[:3]))
    labels_by_text = {}
    families_by_text = {}
    for ex in a:
        key = normalize_text(ex.text)
        labels_by_text.setdefault(key, set()).add(ex.label)
        families_by_text.setdefault(key, set()).add(ex.family)
    check("no normalised sentence carries two labels", all(len(v) == 1 for v in labels_by_text.values()))
    check("no normalised sentence appears in two families", all(len(v) == 1 for v in families_by_text.values()))
    check("no empty sentence", all(normalize_text(ex.text) for ex in a))
    check("every example's label matches its family's label", all(fams[ex.family].label == ex.label for ex in a))
    check("no corpus sentence starts with a find/locate prefix (those never reach the model)",
          not [ex for ex in a if normalize_text(ex.text).startswith(("find ", "where is ", "wheres ", "locate ",
                                                                     "look up ", "lookup ", "search "))])
    c = K.build_corpus(seed=K.EXPANSION_SEED + 1)
    check("a different expansion seed samples differently", K.corpus_sha256(c) != K.corpus_sha256(a))
    edited = list(a)
    edited[0] = K.Example(edited[0].text + " x", edited[0].label, edited[0].family)
    check("editing one sentence changes corpus_sha256", K.corpus_sha256(edited) != K.corpus_sha256(a))

    print("\n[a template that would explode is refused rather than sampled blindly]")
    big = K.Family("oee", "big", ("{a} {b} {c} {d}",),
                   {"a": tuple(f"a{i}" for i in range(20)), "b": tuple(f"b{i}" for i in range(20)),
                    "c": tuple(f"c{i}" for i in range(20)), "d": tuple(f"d{i}" for i in range(20))},
                   ("x y", "y z", "z w"))
    check("a template with > MAX_TEMPLATE_COMBINATIONS combinations is refused",
          raises(K.CorpusError, K.expand_family, big, seed=1, cap=40) is not None)


def section_decontamination():
    print("\n[decontamination: flagged sentences are named by digest and really removed]")
    digests = K.DECONTAMINATED
    check("DECONTAMINATED is non-empty (the leakage check did flag sentences once)", len(digests) > 0,
          str(len(digests)))
    check("every entry is 16 lowercase hex characters",
          all(isinstance(d, str) and re.fullmatch(r"[0-9a-f]{16}", d) for d in digests))
    candidates = {}
    for fam in K.FAMILIES:
        for text in K.candidate_sentences(fam):
            candidates.setdefault(K.sentence_digest(text), set()).add(fam.family_id)
    stale = sorted(d for d in digests if d not in candidates)
    check("every digest names a sentence some family can produce (no stale entry matches nothing)",
          not stale, f"{len(stale)} stale")
    corpus = K.build_corpus()
    present = [e.family for e in corpus if K.sentence_digest(e.text) in digests]
    check("no decontaminated sentence is in the corpus", not present, str(present[:3]))
    raw = K.build_corpus(decontaminated=frozenset())
    removed = [e for e in raw if K.sentence_digest(e.text) in digests]
    check("without the list, the same expansion WOULD contain listed sentences (the list is load-bearing)",
          len(removed) > 0, str(len(removed)))
    check("sentence_digest ignores case and punctuation (it names the normalised sentence)",
          K.sentence_digest("How is OEE?") == K.sentence_digest("how is oee"))
    check("sentence_digest is salted (not the bare SHA-256 of the sentence)",
          K.sentence_digest("how is oee") != hashlib.sha256(b"how is oee").hexdigest()[:16])
    fam = K.FAMILIES[0]
    doomed = frozenset(K.sentence_digest(t) for t in fam.free[:len(fam.free) - K.MIN_FREE_PHRASINGS + 1])
    check("a family left with fewer than 3 surviving free phrasings is refused",
          raises(K.CorpusError, K.validate_families, decontaminated=doomed) is not None)
    first = K.expand_family(fam, seed=K.EXPANSION_SEED, cap=K.MAX_PER_FAMILY, decontaminated=frozenset())
    target = K.sentence_digest(first[-1].text)
    refilled = K.expand_family(fam, seed=K.EXPANSION_SEED, cap=K.MAX_PER_FAMILY,
                               decontaminated=frozenset({target}))
    check("removing a sentence skips it and the family refills from its other renderings",
          all(K.sentence_digest(e.text) != target for e in refilled)
          and (len(refilled) == len(first) or len(first) < K.MAX_PER_FAMILY), f"{len(first)} -> {len(refilled)}")


def section_split():
    print("\n[family split]")
    corpus = K.build_corpus()
    train, val, test = K.split_corpus(corpus, seed=K.SPLIT_SEED)
    train2, val2, test2 = K.split_corpus(corpus, seed=K.SPLIT_SEED)
    check("split is deterministic", (train, val, test) == (train2, val2, test2))
    check("split covers the corpus exactly once", len(train) + len(val) + len(test) == len(corpus))
    ft, fv, fs = ({e.family for e in part} for part in (train, val, test))
    check("families are disjoint across train/val/test", not (ft & fv) and not (ft & fs) and not (fv & fs))
    for name, part in (("train", train), ("val", val), ("test", test)):
        check(f"every label has families in {name}", {e.label for e in part} == set(L.LABELS),
              str(sorted(set(L.LABELS) - {e.label for e in part})))
    n_fam = len(ft | fv | fs)
    check("about 70/15/15 of families", abs(len(fs) / n_fam - 0.15) < 0.04 and abs(len(fv) / n_fam - 0.15) < 0.04,
          f"train={len(ft)} val={len(fv)} test={len(fs)}")
    other = K.split_corpus(corpus, seed=K.SPLIT_SEED + 1)
    check("a different split seed gives a different split", other[2] != test)


def section_leakage():
    print("\n[leakage against the repo's evaluation questions]")
    from amp_ai.copilot_intent import build as B

    sets = B.load_external_question_sets(BACKEND)
    questions = [q for rows in sets.values() for q, _label in rows]
    check("more than 100 evaluation questions were loaded (the check is not vacuous)",
          len(questions) > 100, str(len(questions)))
    check("all five evaluation sets were found", set(sets) == set(B.EXTERNAL_SETS), str(sorted(sets)))
    corpus = K.build_corpus()
    report = K.leakage_report([e.text for e in corpus], questions)
    check("report says it checked every loaded question", report["questions_checked"] == len(set(
        normalize_text(q) for q in questions)), str(report["questions_checked"]))
    check("no corpus sentence equals an evaluation question", report["exact"] == 0, str(report["exact"]))
    check("no corpus sentence has char-4-gram Jaccard >= 0.6 with an evaluation question",
          report["near"] == 0, str(report["near"]))

    print("\n[the leakage check catches what it must (self-test, no question printed)]")
    q = questions[0]
    planted = [e.text for e in corpus[:50]] + [q]
    r = K.leakage_report(planted, questions)
    check("an exact copy is caught", r["exact"] >= 1)
    words = normalize_text(q).split(" ")
    near = " ".join(words + ["please"]) if len(words) >= 5 else " ".join(words + ["now"])
    r = K.leakage_report([near], [q])
    check("a near copy (one word appended) is caught", r["near"] + r["exact"] >= 1, str(r))
    r = K.leakage_report(["completely unrelated words about canteen lunch menus"], [q])
    check("an unrelated sentence is not flagged", r["near"] == 0 and r["exact"] == 0)
    check("an empty question list is refused (a check that loads nothing reports all-clear)",
          raises(K.CorpusError, K.leakage_report, ["a b c"], []) is not None)
    check("jaccard of identical 4-gram sets is 1", K.jaccard(K.char_ngrams("abcdef"), K.char_ngrams("abcdef")) == 1.0)
    check("the leakage threshold is 0.6", K.LEAKAGE_JACCARD == 0.6)


def main():
    print("=" * 74)
    print("AMP-native copilot intent: corpus")
    print("=" * 74)
    section_families()
    section_expansion()
    section_decontamination()
    section_split()
    section_leakage()
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


def test_amp_ai_intent_corpus():
    """pytest entry point; CI runs this file as a standalone script."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
