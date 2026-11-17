"""The claim notification's "N of M data categories" must reconcile with the
offer the factory actually chose from.

When a factory accepts a machine, oem_subscribers.notify_both_parties_of_claim
raises a notification in the factory's own workspace that says how many data
categories it is now sharing with the manufacturer:

    "You are sharing N of M data categories with them; ..."

M was the literal 7 — the size of the full grant VOCABULARY
(oem_sharing.ALL_GRANTS). But a factory is OFFERED only the grants something in
AMP actually reads: oem_sharing.OFFERED_GRANTS, which is FOUR of the seven. The
other three are reserved and oem_sharing.refused_grants() rejects any attempt to
grant them, so a factory can never give more than four. The headline therefore:

  * understated full consent — a factory sharing everything it CAN was told
    "You are sharing 4 of 7", reading as three categories it deliberately
    withheld, when those three were never on offer; and
  * used a denominator (7) that did not match the basis of the figure beside it
    (the four offered grants the Connected Equipment screen shows and toggles).

The fix counts the OFFERED grants shared against the number offered — the exact
set and basis of oem_sharing.offered_grant_choices — and the intersection drops
a legacy/reserved grant a pre-existing policy may still carry from a count whose
denominator does not include it.

These are UNIT tests over the subscriber: a fake db captures the Notification
rows it builds (no connection needed — Notification is just instantiated), and
we read the factory-copy message back out.

The expected numbers are derived INDEPENDENTLY of the code under test: the
offered set is pinned to the four named grant constants (so if OFFERED_GRANTS
ever loses or gains one, this test's own expected denominator moves with the
named list, not with whatever the message happens to print).

Run:  python backend/test_oem_claim_notification_denominator.py   (exit 0 = pass)
"""
import models
import oem_sharing
from oem_events import MachineClaimed
from oem_subscribers import notify_factory_of_installation  # noqa: F401  (import smoke)
from oem_subscribers import notify_both_parties_of_claim


# Independently-derived expectation: the grants a factory can actually be given
# are exactly these four named constants. Pin the denominator to THIS list, not
# to anything the subscriber prints, so the assertions are not tautological.
EXPECTED_OFFERED = {
    oem_sharing.SHARE_MACHINE_HEALTH,
    oem_sharing.SHARE_OPERATING_HOURS,
    oem_sharing.SHARE_SERVICE_STATUS,
    oem_sharing.SHARE_DOWNTIME,
}
EXPECTED_M = len(EXPECTED_OFFERED)   # 4 — the real number of offered categories


class _FakeDB:
    """Captures the Notification rows the subscriber builds, without a DB.

    oem_subscribers._notify only ever calls db.add(models.Notification(...)); a
    Notification is a plain ORM object that instantiates fine with no session,
    so capturing the adds is enough to read the message back."""

    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)


def _factory_message(granted: str) -> str:
    """The factory-copy notification message for a claim granting `granted`."""
    event = MachineClaimed(
        tenant_code="ACME",
        oem_code="OEM_ALPHA",
        installation_id=1,
        serial_number="SN-1",
        model_code="M-1",
        granted=granted,
    )
    db = _FakeDB()
    notify_both_parties_of_claim(event, db)
    factory = [n for n in db.added
               if getattr(n, "notification_type", None) == "equipment_claimed"
               and n.tenant_code == "ACME"]
    assert len(factory) == 1, f"expected one factory notification, got {len(factory)}"
    return factory[0].message


def test_the_offered_set_is_four_of_seven():
    # Guards the premise of the whole test: the vocabulary is 7, the offer is 4.
    assert set(oem_sharing.OFFERED_GRANTS) == EXPECTED_OFFERED, oem_sharing.OFFERED_GRANTS
    assert len(oem_sharing.ALL_GRANTS) == 7, oem_sharing.ALL_GRANTS
    assert EXPECTED_M == 4
    print("PASS premise: 4 offered grants out of a 7-grant vocabulary")


def test_sharing_all_offered_reads_four_of_four():
    msg = _factory_message(",".join(sorted(EXPECTED_OFFERED)))
    assert f"sharing {EXPECTED_M} of {EXPECTED_M} data categories" in msg, msg
    # The regression itself: the stale literal denominator must be gone.
    assert "of 7 data categories" not in msg, msg
    print(f"PASS all-offered shared -> 'sharing {EXPECTED_M} of {EXPECTED_M}' (never 'of 7')")


def test_sharing_two_offered_reads_two_of_four():
    msg = _factory_message(
        f"{oem_sharing.SHARE_OPERATING_HOURS},{oem_sharing.SHARE_SERVICE_STATUS}")
    assert f"sharing 2 of {EXPECTED_M} data categories" in msg, msg
    assert "of 7" not in msg, msg
    print(f"PASS two offered shared -> 'sharing 2 of {EXPECTED_M}'")


def test_sharing_nothing_reads_nothing():
    # Empty grant string -> the "sharing nothing" branch, no numerator at all.
    msg = _factory_message("")
    assert "sharing nothing with them" in msg, msg
    assert "data categories" not in msg, msg
    print("PASS empty grant -> 'sharing nothing'")


def test_reserved_legacy_grant_is_not_counted():
    # A pre-existing policy may still carry a reserved grant the vocabulary
    # parses but nothing offers (SHARE_ALARMS). It must NOT inflate the count:
    # one offered grant (SHARE_DOWNTIME) + one reserved -> "1 of 4", not "2 of 4"
    # and never "2 of 7".
    assert oem_sharing.SHARE_ALARMS not in oem_sharing.OFFERED_GRANTS
    msg = _factory_message(f"{oem_sharing.SHARE_DOWNTIME},{oem_sharing.SHARE_ALARMS}")
    assert f"sharing 1 of {EXPECTED_M} data categories" in msg, msg
    assert "of 7" not in msg and "2 of" not in msg, msg
    print(f"PASS reserved legacy grant excluded -> 'sharing 1 of {EXPECTED_M}'")


def test_only_reserved_grant_reads_nothing():
    # A policy carrying ONLY reserved grants shares nothing AMP reads or shows,
    # so the honest message is "nothing" — not "1 of 7" for a category the
    # factory cannot manage on the Connected Equipment screen.
    msg = _factory_message(oem_sharing.SHARE_TELEMETRY)
    assert "sharing nothing with them" in msg, msg
    assert "data categories" not in msg, msg
    print("PASS only-reserved grant -> 'sharing nothing'")


def test_duplicate_offered_grant_counts_once():
    # A doubled token must not double-count (the old raw split would have).
    msg = _factory_message(
        f"{oem_sharing.SHARE_DOWNTIME},{oem_sharing.SHARE_DOWNTIME}")
    assert f"sharing 1 of {EXPECTED_M} data categories" in msg, msg
    print(f"PASS duplicate offered grant counted once -> 'sharing 1 of {EXPECTED_M}'")


if __name__ == "__main__":
    test_the_offered_set_is_four_of_seven()
    test_sharing_all_offered_reads_four_of_four()
    test_sharing_two_offered_reads_two_of_four()
    test_sharing_nothing_reads_nothing()
    test_reserved_legacy_grant_is_not_counted()
    test_only_reserved_grant_reads_nothing()
    test_duplicate_offered_grant_counts_once()
    print("ALL OEM CLAIM NOTIFICATION DENOMINATOR TESTS PASSED")
