"""The machine claim: an offer an OEM makes, and only a factory can accept.

ADR-0019. The single rule this module exists to enforce:

    AN OEM CANNOT ATTACH A MACHINE TO A FACTORY. IT CAN ONLY OFFER.

The damage a direct assignment would do is not that the OEM reads the factory's
data — the sharing policy still stands, and a fresh installation shares nothing.
It is that a row appears on that factory's Connected Equipment screen, presented
by AMP as their equipment, from a supplier they have never dealt with, next to
controls inviting them to grant it access. AMP would be lending its credibility
to a stranger.

WHY THE LOGIC IS HERE AND NOT IN THE ROUTES
-------------------------------------------
Two operations decide everything — `accept` and `revoke` — and both are
conditional UPDATEs whose row count IS the security decision. Written inline in a
handler, the pattern reads like ordinary ORM code and the next person "tidies" it
into a read-modify-write, which is correct-looking and racy. Here it can be
stated once, tested once, and mutated once.

THE CODE
--------
15 characters from a 32-symbol alphabet with I, L, O, U and 0, 1 removed (~75
bits), grouped for transcription. Only its SHA-256 is stored; lookup is by hash
of what was presented, so there is no scan and no per-code timing difference.
"""
import hashlib
import secrets
from datetime import datetime, timedelta

import models

# No I, L, O, U (read wrong off a sticker), no 0 or 1 (confused with O and I).
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_GROUPS, _GROUP_LEN = 3, 5

PENDING, CLAIMED, REVOKED, EXPIRED = "Pending", "Claimed", "Revoked", "Expired"

DEFAULT_TTL_DAYS = 30
MAX_TTL_DAYS = 365

# ONE sentence for every failure. Wrong code, expired, revoked, already claimed,
# machine already installed elsewhere — all indistinguishable. Anything else
# tells somebody probing codes which guesses were close.
REFUSAL = ("That claim code is not valid. It may have been mistyped, expired, "
           "already used, or withdrawn by the manufacturer — ask them to issue "
           "a new one.")


class ClaimError(Exception):
    """A claim could not be created. NOT used for claim ATTEMPTS: those all
    return the same refusal, and an exception type per reason is how a caller
    accidentally builds the oracle this module avoids."""


def generate_code():
    """A fresh claim code, grouped for a human to read aloud or type."""
    raw = "".join(secrets.choice(_ALPHABET)
                  for _ in range(_GROUPS * _GROUP_LEN))
    return "AMP-" + "-".join(raw[i:i + _GROUP_LEN]
                             for i in range(0, len(raw), _GROUP_LEN))


def normalise(code):
    """Fold a typed code onto its canonical form BEFORE hashing.

    Somebody reading a sticker over the phone produces 'amp xxxxx xxxxx xxxxx';
    somebody scanning produces 'AMP-XXXXX-XXXXX-XXXXX'. Both must hash to the
    same value or the code only works when transcribed perfectly.
    """
    return "".join(ch for ch in str(code or "").upper()
                   if ch.isalnum())


def hash_code(code):
    return hashlib.sha256(normalise(code).encode("utf-8")).hexdigest()


def code_hint(code):
    """The last four characters — enough to match a row to a sticker during a
    support call, useless as a credential."""
    text = normalise(code)
    return text[-4:] if len(text) >= 4 else ""


def is_expired(claim, now=None):
    """Expiry is decided HERE, at use, not by a sweeper.

    A background job that has not run yet is a window in which an expired
    credential still works. The stored status is a convenience for listing; this
    is the authority.
    """
    now = now or datetime.utcnow()
    return claim.expires_at is not None and claim.expires_at <= now


def create(db, oem_code, installation, actor, ttl_days=None,
           intended_tenant_code=None, now=None):
    """Issue an invitation for one machine. Returns (claim, RAW CODE).

    The raw code is returned exactly once, to be printed or shown, and is not
    recoverable from the row afterwards.
    """
    now = now or datetime.utcnow()
    if installation.factory_tenant_code:
        raise ClaimError(
            "That machine is already installed at a customer. Release it there "
            "first — a machine cannot be offered to two factories at once.")

    live = (db.query(models.MachineClaim)
              .filter(models.MachineClaim.installation_id == installation.id,
                      models.MachineClaim.status == PENDING).all())
    if [c for c in live if not is_expired(c, now)]:
        raise ClaimError(
            "That machine already has an open invitation. Revoke it before "
            "issuing another, so only one code is ever live for a machine.")

    days = DEFAULT_TTL_DAYS if ttl_days is None else int(ttl_days)
    if days < 1 or days > MAX_TTL_DAYS:
        raise ClaimError(f"Expiry must be between 1 and {MAX_TTL_DAYS} days.")

    code = generate_code()
    claim = models.MachineClaim(
        oem_code=oem_code, installation_id=installation.id,
        token_hash=hash_code(code), code_hint=code_hint(code),
        status=PENDING,
        intended_tenant_code=(intended_tenant_code or None),
        expires_at=now + timedelta(days=days),
        created_at=now, created_by=actor)
    db.add(claim)
    db.flush()
    return claim, code


def find_by_code(db, code):
    """The claim a presented code refers to, or None.

    By hash, never by id or serial — a caller that could look a claim up by
    machine would be able to enumerate stock.
    """
    text = normalise(code)
    if not text:
        return None
    return (db.query(models.MachineClaim)
              .filter(models.MachineClaim.token_hash == hash_code(text)).first())


def usable(claim, tenant_code, installation, now=None):
    """Can THIS tenant accept THIS claim right now?

    Returns a bool and nothing else, on purpose. Every caller turns a False into
    the same refusal, so no reason ever reaches the caller and no oracle exists.
    """
    if claim is None or installation is None:
        return False
    if claim.status != PENDING or is_expired(claim, now):
        return False
    # The machine must still be unassigned. A claim for a machine somebody
    # already installed is dead even if the claim row never noticed.
    if installation.factory_tenant_code:
        return False
    # An intended tenant is a HINT the OEM may set; when present it is enforced,
    # and when absent the claim is equally valid. It never assigns anything by
    # itself — acceptance still has to happen.
    if claim.intended_tenant_code and claim.intended_tenant_code != tenant_code:
        return False
    return True


def accept(db, claim, installation, tenant_code, actor, now=None):
    """Take the claim, atomically. True only if THIS caller won it.

    TWO CONDITIONAL UPDATES, AND THE ROW COUNTS ARE THE SECURITY DECISION.

    The database decides who wins, not Python. Read-modify-write here would let
    two administrators who pressed the button in the same second both succeed:
    both read `status == Pending`, both write `Claimed`, and the machine ends up
    assigned to whichever transaction committed last with the other believing it
    succeeded too.

    Both statements are compare-and-set and work identically on PostgreSQL and
    SQLite:

        UPDATE machine_claims       SET status='Claimed' WHERE id=? AND status='Pending'
        UPDATE machine_installations SET factory_tenant_code=? WHERE id=? AND factory_tenant_code IS NULL

    The second is not redundant. It closes the case where the machine was
    assigned by some other path between the check and the write — the claim would
    be spent on a machine that already belongs to somebody.
    """
    now = now or datetime.utcnow()

    won = (db.query(models.MachineClaim)
             .filter(models.MachineClaim.id == claim.id,
                     models.MachineClaim.status == PENDING)
             .update({"status": CLAIMED, "claimed_at": now,
                      "claimed_by": actor, "claimed_tenant_code": tenant_code},
                     synchronize_session=False))
    if won != 1:
        return False

    took = (db.query(models.MachineInstallation)
              .filter(models.MachineInstallation.id == installation.id,
                      models.MachineInstallation.factory_tenant_code.is_(None))
              .update({"factory_tenant_code": tenant_code,
                       "status": "Assigned", "updated_at": now},
                      synchronize_session=False))
    if took != 1:
        # The claim was won but the machine was not. Roll the whole thing back
        # rather than leaving a spent claim against a machine somebody else has:
        # a claim that cannot be used again and did nothing is the worst of both.
        db.rollback()
        return False
    return True


def revoke(db, claim, actor, now=None):
    """Withdraw an unused invitation. True only if it was still open.

    Conditional for the same reason `accept` is: revoking must not race a claim
    into a state where both parties think they won.
    """
    now = now or datetime.utcnow()
    changed = (db.query(models.MachineClaim)
                 .filter(models.MachineClaim.id == claim.id,
                         models.MachineClaim.status == PENDING)
                 .update({"status": REVOKED, "revoked_at": now,
                          "revoked_by": actor}, synchronize_session=False))
    return changed == 1


def public_state(claim, now=None):
    """What the ISSUING OEM may see about its own claim.

    `Pending` is reported as `Expired` once the deadline has passed, so a list
    never shows an invitation as live when presenting it would be refused. The
    stored row is left alone — the state is derived, not swept.
    """
    if claim.status == PENDING and is_expired(claim, now):
        return EXPIRED
    return claim.status
