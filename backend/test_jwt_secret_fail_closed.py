"""JWT signing-key fail-closed tests (security PR3).

auth.SECRET_KEY used to fall back to a constant published in this repository —
anyone reading the source could mint a valid Admin token for any deployment
that forgot to set SECRET_KEY. It now fails closed: production (Railway) with
no key refuses to boot; dev/CI with no key gets a random ephemeral key; a set
key is used verbatim. The key resolves at import time, so each case runs in a
subprocess with a controlled environment.

Run:  python backend/test_jwt_secret_fail_closed.py     (exit 0 = pass)
"""
import os
import subprocess
import sys

_OLD_CONSTANT = "flowmes_super_secret_key_change_in_production"


def _probe(extra_env, code="import auth; print('KEY:' + auth.SECRET_KEY)"):
    """Import auth in a subprocess with a controlled env; return (rc, output).
    SECRET_KEY is passed as '' (empty) to mean 'unset' — load_dotenv() does not
    override an existing env var, so this also masks any local .env value."""
    env = {**os.environ, "SECRET_KEY": "", "RAILWAY_ENVIRONMENT": "", "PRODUCTION": "", **extra_env}
    env = {k: v for k, v in env.items()}
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=os.path.dirname(__file__) or ".",
                         capture_output=True, text=True, timeout=120)
    return out.returncode, (out.stdout or "") + (out.stderr or "")


def test_production_with_no_key_refuses_to_boot():
    rc, out = _probe({"RAILWAY_ENVIRONMENT": "production"})
    assert rc != 0, "production with no SECRET_KEY must refuse to boot"
    assert "SECRET_KEY is not set" in out
    # the explicit PRODUCTION=1 flag fails closed the same way
    rc2, out2 = _probe({"PRODUCTION": "1"})
    assert rc2 != 0 and "SECRET_KEY is not set" in out2
    print("PASS production with no SECRET_KEY refuses to boot (clear message)")


def test_dev_with_no_key_gets_a_random_ephemeral_key():
    rc, out = _probe({})
    assert rc == 0, out
    key = out.split("KEY:", 1)[1].strip()
    assert key and key != _OLD_CONSTANT, "the published constant must never be used"
    assert len(key) >= 64                      # token_urlsafe(64) -> ~86 chars
    assert "ephemeral dev key" in out          # and it says so
    # a second process gets a DIFFERENT key — ephemeral, not derivable
    rc2, out2 = _probe({})
    assert out2.split("KEY:", 1)[1].strip() != key
    print("PASS dev/CI with no SECRET_KEY gets a random, per-process ephemeral key")


def test_a_set_key_is_used_verbatim_everywhere():
    rc, out = _probe({"SECRET_KEY": "unit-test-key-123"})
    assert rc == 0, out
    assert out.split("KEY:", 1)[1].strip() == "unit-test-key-123"
    # ...including in production mode (a set key means production boots fine)
    rc2, out2 = _probe({"SECRET_KEY": "unit-test-key-123", "RAILWAY_ENVIRONMENT": "production"})
    assert rc2 == 0, out2
    assert out2.split("KEY:", 1)[1].strip() == "unit-test-key-123"
    print("PASS a set SECRET_KEY is used verbatim, and production boots with it")


def test_tokens_still_roundtrip_under_the_resolved_key():
    code = ("import auth; t = auth.create_access_token({'sub': 'u', 'role': 'Admin', 'tenant': 'DEFAULT'}); "
            "p = auth.verify_token(t); print('ROUNDTRIP:' + p['sub'] + '/' + p['role'])")
    rc, out = _probe({}, code=code)
    assert rc == 0, out
    assert "ROUNDTRIP:u/Admin" in out
    print("PASS token create/verify roundtrips under the resolved key")


if __name__ == "__main__":
    test_production_with_no_key_refuses_to_boot()
    test_dev_with_no_key_gets_a_random_ephemeral_key()
    test_a_set_key_is_used_verbatim_everywhere()
    test_tokens_still_roundtrip_under_the_resolved_key()
    print("JWT SECRET FAIL-CLOSED OK: prod refuses a missing key; dev gets an ephemeral one; "
          "a set key is used verbatim")
