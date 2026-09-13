"""A deployment that configured MQTT_TOPIC went silently deaf.

THE DEFECT
----------
`mqtt_service.py` carried this comment above its topic constants:

    # The prefix, not the whole topic: the tenant and site are segments of it
    # now (see mqtt_identity). MQTT_TOPIC is still read so an existing
    # deployment that set it to a custom single-tenant topic keeps that topic
    # working via the legacy path below rather than silently going deaf after
    # this upgrade.

`MQTT_TOPIC` was read NOWHERE. A grep across the backend returned exactly one
hit: the comment itself. So the comment described, precisely and in its own
words, the failure it claimed to prevent.

WHY IT MATTERS, CONCRETELY
--------------------------
`backend/.env` on the development machine still reads `MQTT_TOPIC=flowmes/machines`
and sets no `MQTT_LEGACY_TENANT` — the exact pre-upgrade shape shipped in the
old `.env.example` and the old `docker-compose.yml`. Follow that config through
the code:

  * `TOPIC_PREFIX` falls back to its "flowmes" default (MQTT_TOPIC unread),
  * `LEGACY_TENANT` is "" (never set — it did not exist when that .env was
    written),
  * so `mqtt_identity.topic_filters("flowmes", "")` returns
    `["flowmes/+/+/machines"]` and NOTHING subscribes to `flowmes/machines`.

A gateway publishing to the topic the operator configured is heard by nobody.
No error, no rejection warning, no dropped-message log — the messages are never
delivered to this process at all. The health block reports the listener alive,
because it is. This is the one failure mode `mqtt_identity` is written to avoid
("Dropping telemetry is a VISIBLE, recoverable failure"), arrived at by a route
`mqtt_identity` never sees.

WHAT THE FIX MAY AND MAY NOT DO
-------------------------------
It may honour the half of `MQTT_TOPIC` that is unambiguous: the PREFIX. Reading
a variable the operator explicitly set is not guessing.

It may NOT invent the tenant. `MQTT_TOPIC=flowmes/machines` says nothing about
who owns the data, and `mqtt_identity.parse_topic` refuses to attribute a legacy
topic without `MQTT_LEGACY_TENANT` for a reason that has not changed: a wrong
owner is silent and unrecoverable, a dropped message is neither.

So what is left is the thing that was actually missing — SAYING SO. A
misconfiguration that cannot be repaired automatically must be loud, and must
name the variable that repairs it. A guard nobody can hear is the same as no
guard (#588's lesson, in a different register).

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_mqtt_topic_config_is_honoured.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("DATABASE_URL", "sqlite:///./ci.db")

import mqtt_identity  # noqa: E402
import mqtt_service  # noqa: E402

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


def resolve(**env):
    """Drive the resolver with an explicit dict — never the process environment.

    The suite must not depend on what happens to be exported when it runs, and
    must not leave the process changed for whatever runs next in the sweep.
    """
    return mqtt_service.resolve_subscription(env)


def main():
    print("=" * 74)
    print("1. THE PREFIX HALF OF MQTT_TOPIC IS HONOURED")
    print("=" * 74)
    prefix, tenant, site, warnings = resolve(MQTT_TOPIC="acme/machines")
    check("MQTT_TOPIC supplies the prefix when MQTT_TOPIC_PREFIX is unset",
          prefix == "acme", repr(prefix))

    prefix, _, _, _ = resolve(MQTT_TOPIC="acme/machines", MQTT_TOPIC_PREFIX="flowmes")
    check("...but an explicit MQTT_TOPIC_PREFIX wins over the legacy variable",
          prefix == "flowmes", repr(prefix))

    prefix, _, _, warnings = resolve(MQTT_TOPIC="factory/telemetry")
    check("an MQTT_TOPIC that is not a {prefix}/machines topic falls back to the "
          "default prefix rather than inventing one",
          prefix == "flowmes", repr(prefix))
    # Asserted on the REPAIR, not on the topic string. Both warnings this config
    # produces quote MQTT_TOPIC, so `"factory/telemetry" in w` was satisfied by
    # the missing-tenant warning and could not tell the two apart — deleting the
    # shape warning entirely left this green (mutation Z5). The variable that
    # fixes each problem is what distinguishes them.
    check("...and says so, naming MQTT_TOPIC_PREFIX as the repair",
          any("MQTT_TOPIC_PREFIX" in w for w in warnings), repr(warnings))
    check("...as a warning distinct from the missing-tenant one",
          len(warnings) == 2, repr(warnings))

    prefix, _, _, _ = resolve()
    check("an unconfigured deployment still gets the documented default",
          prefix == "flowmes", repr(prefix))

    print()
    print("=" * 74)
    print("2. THE TENANT HALF IS NOT INVENTED — IT IS ANNOUNCED")
    print("=" * 74)
    # The exact shape of backend/.env and the pre-upgrade docker-compose.yml.
    prefix, tenant, site, warnings = resolve(MQTT_TOPIC="flowmes/machines")
    check("MQTT_TOPIC alone does NOT conjure a legacy tenant", tenant == "",
          repr(tenant))
    check("...so the legacy topic is still not subscribed (fail-closed holds)",
          "flowmes/machines" not in mqtt_identity.topic_filters(prefix, tenant),
          str(mqtt_identity.topic_filters(prefix, tenant)))
    check("...and the operator is WARNED, which is the whole fix",
          len(warnings) > 0, "silent, exactly as before")
    check("...by a warning naming the variable that repairs it",
          any("MQTT_LEGACY_TENANT" in w for w in warnings), repr(warnings))
    check("...and naming the topic that is going unheard",
          any("flowmes/machines" in w for w in warnings), repr(warnings))

    prefix, tenant, site, warnings = resolve(MQTT_TOPIC="flowmes/machines",
                                             MQTT_LEGACY_TENANT="ACME")
    check("with MQTT_LEGACY_TENANT set, the legacy topic IS subscribed",
          "flowmes/machines" in mqtt_identity.topic_filters(prefix, tenant),
          str(mqtt_identity.topic_filters(prefix, tenant)))

    print()
    print("=" * 74)
    print("3. DO NOT CRY WOLF")
    print("=" * 74)
    # test_mqtt_boot.py's first lesson: a deployment that is correctly configured
    # must not be told it is broken. A warning that fires for everyone is noise,
    # and noise is how the real one gets missed.
    _, _, _, warnings = resolve(MQTT_TOPIC_PREFIX="flowmes",
                                MQTT_LEGACY_TENANT="ACME", MQTT_LEGACY_SITE="PLANT1")
    check("a fully migrated deployment is warned about nothing",
          warnings == [], repr(warnings))
    _, _, _, warnings = resolve()
    check("a deployment that never set any of these is warned about nothing",
          warnings == [], repr(warnings))
    _, _, _, warnings = resolve(MQTT_TOPIC="")
    check("a blank MQTT_TOPIC counts as unset",
          warnings == [], repr(warnings))

    print()
    print("=" * 74)
    print("4. THE SITE AND TENANT ARE READ THROUGH THE SAME RESOLVER")
    print("=" * 74)
    _, tenant, site, _ = resolve(MQTT_LEGACY_TENANT="  ACME  ",
                                 MQTT_LEGACY_SITE="  PLANT1  ")
    check("surrounding whitespace is stripped, as the module constants did",
          (tenant, site) == ("ACME", "PLANT1"), repr((tenant, site)))

    print()
    print("=" * 74)
    print("5. ONE RESOLUTION, NOT TWO")
    print("=" * 74)
    # The constants the rest of the module actually uses must come from the
    # resolver. If on_connect subscribed under one rule and on_message parsed
    # under another, a message could be delivered and then rejected as
    # "outside prefix" — the two-implementations defect, one function apart.
    src = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "mqtt_service.py"), encoding="utf-8", newline="").read()
    check("the module constants are derived by calling resolve_subscription()",
          "resolve_subscription()" in src, "computed a second time by hand")
    check("MQTT_TOPIC is read by code, not only described by a comment",
          src.count("MQTT_TOPIC\"") + src.count("MQTT_TOPIC'") >= 1,
          "still comment-only")

    print()
    print("=" * 74)
    print("6. THE WARNINGS REACH A LOG, NOT JUST A RETURN VALUE")
    print("=" * 74)
    # A resolver that returns perfect warnings nobody emits is the same defect
    # in a new place.
    emitted = []
    real_warning = mqtt_service.log.warning
    mqtt_service.log.warning = lambda msg, *a, **kw: emitted.append(
        msg % a if a else msg)
    old = {k: os.environ.get(k)
           for k in ("MQTT_BROKER", "MQTT_TOPIC", "MQTT_TOPIC_PREFIX",
                     "MQTT_LEGACY_TENANT")}
    try:
        os.environ["MQTT_BROKER"] = "broker.example"
        # Deliberately NOT the shape of any .env this repo ships. mqtt_service
        # calls load_dotenv() at import, and backend/.env still carries
        # MQTT_TOPIC=flowmes/machines — so the import-time TOPIC_WARNINGS were
        # already non-empty on a developer machine, and a mutation that deleted
        # the re-resolution below and logged the STALE list still looked green
        # here while failing on CI, where there is no .env at all (mutation Z9).
        # A prefix that can only have come from this block proves the listener
        # read the environment as it is at start, not as it was at import.
        os.environ["MQTT_TOPIC"] = "runtime-only/machines"
        os.environ.pop("MQTT_TOPIC_PREFIX", None)
        os.environ.pop("MQTT_LEGACY_TENANT", None)

        class _Client:
            def username_pw_set(self, *a, **kw): pass
            def tls_set(self, *a, **kw): pass
            def connect(self, *a, **kw): pass
            def loop_forever(self): pass

        mqtt_service.start_mqtt_service(client_factory=_Client,
                                        sleep=lambda s: None, run_inline=True)
    finally:
        mqtt_service.log.warning = real_warning
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    check("starting the listener logs the misconfiguration at WARNING",
          any("MQTT_LEGACY_TENANT" in m for m in emitted), repr(emitted))
    check("...about the config as it is at START, not as it was at import",
          any("runtime-only/machines" in m for m in emitted), repr(emitted))
    check("...and the constants on_message parses with were refreshed too",
          mqtt_service.TOPIC_PREFIX == "runtime-only",
          repr(mqtt_service.TOPIC_PREFIX))

    # Put the module back where it was found. The checks above had to run while
    # it held the runtime value, so this cannot live in the finally above.
    (mqtt_service.TOPIC_PREFIX, mqtt_service.LEGACY_TENANT,
     mqtt_service.LEGACY_SITE, mqtt_service.TOPIC_WARNINGS) = \
        mqtt_service.resolve_subscription()

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        for f in failures:
            print("  -", f)
        return 1
    print("MQTT TOPIC CONFIG OK: MQTT_TOPIC supplies the prefix; the missing "
          "tenant is announced, never invented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
