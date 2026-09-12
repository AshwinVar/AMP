"""A customer's production data was written into the shared platform log.

THE DEFECT
----------
`mqtt_service.on_message` logged, at INFO, for EVERY message:

    log.info("\\nRAW MQTT MESSAGE RECEIVED")
    log.info("Topic: %s", msg.topic)
    log.info("Payload: %s", raw_payload)

so the whole decoded body — machine names, production counts, good/reject
splits, and every `readings` value a gateway sends — went into the platform's
default log stream, on a multi-tenant deployment, at the cadence the plant
publishes. A compressor reporting every few seconds writes its owner's
operational data into that stream all day.

Three things wrong with it:

  * **Tenancy.** The stream is shared across tenants and is read by whoever has
    platform log access, and by whatever aggregator the host ships to, for that
    aggregator's retention period — which is not the 14 days
    `docs/RETENTION.md` promises for `iot_telemetry`. AMP's own rule is that a
    customer's operational data is theirs; a log line is not an exception.
  * **Volume.** Unbounded and proportional to ingest, on the one process that
    also serves every request.
  * **Structure.** The leading `\\n` banner splits one record across two lines
    of a stream `logging_config.JsonFormatter` emits as one JSON object per
    line, so the line after it is not parseable as a record.

WHAT THIS KEEPS
---------------
Not a blanket redaction. An engineer standing in the plant needs to see that
data is arriving — the field procedure reads exactly the accept line — so:

  * **INFO** keeps ONE line per accepted message: tenant, site, machine, and the
    status transition. Low cardinality, no counts, no readings.
  * **DEBUG** keeps the raw payload, for the deep case, opt-in via `LOG_LEVEL`.
  * The rejection warnings are untouched. A dropped message must always say why.

Run: DATABASE_URL="sqlite:///./ci.db" python backend/test_mqtt_log_hygiene.py
"""
import json
import logging
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import models
import mqtt_service
from database import Base

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"   [{detail}]" if detail and not condition else ""))


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self):
        return [r.getMessage() for r in self.records]

    def at(self, level):
        return [r.getMessage() for r in self.records if r.levelno == level]


PAYLOAD = {
    "machine": "COMP-01",
    "status": "Running",
    "utilization": 78,
    "downtime": "0 min",
    "total_count": 1000,
    "good_count": 994,
    "rejected_count": 6,
    "readings": {"P1": 7.4, "T_dis": 82, "HrsRun": 14320},
}


def _deliver(level=logging.INFO, payload=None, topic="flowmes/ACME/PLANT1/machines"):
    """Run one real message through on_message, capturing what it logged."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(models.TenantConfig(tenant_code="ACME"))
    db.commit()
    db.close()

    original_session, original_level = mqtt_service.SessionLocal, mqtt_service.log.level
    capture = _Capture()
    mqtt_service.SessionLocal = Session
    mqtt_service.log.addHandler(capture)
    mqtt_service.log.setLevel(level)
    try:
        body = json.dumps(payload if payload is not None else PAYLOAD)
        mqtt_service.on_message(
            None, None,
            SimpleNamespace(topic=topic, payload=body.encode()))
    finally:
        mqtt_service.log.removeHandler(capture)
        mqtt_service.log.setLevel(original_level)
        mqtt_service.SessionLocal = original_session
    return capture


def main():
    print("=" * 74)
    print("1. THE RAW PAYLOAD IS NOT IN THE DEFAULT STREAM")
    print("=" * 74)
    capture = _deliver(logging.INFO)
    joined = "\n".join(capture.messages())
    check(f"the handler logged something at all ({len(capture.records)} records)",
          capture.records, "nothing captured — the rest of this file proves nothing")
    for secret, what in (("994", "the good count"),
                         ("1000", "the total count"),
                         ("7.4", "a pressure reading"),
                         ("14320", "an hour-meter reading"),
                         ("HrsRun", "a gateway tag name")):
        check(f"{what} is not in the INFO stream", secret not in joined,
              f"found {secret!r} in the log")
    check("...and neither is the raw JSON body",
          '"machine":' not in joined and "'machine':" not in joined,
          "the decoded payload is still being logged")

    print()
    print("=" * 74)
    print("2. BUT AN ENGINEER ON SITE CAN STILL SEE IT ARRIVE")
    print("=" * 74)
    # Over-redacting would break the field procedure, which reads exactly this
    # line to prove the gateway is talking to AMP. This is the control: section 1
    # must not be satisfied by logging nothing.
    info = "\n".join(capture.at(logging.INFO))
    check("the machine is named", "COMP-01" in info, info[:200])
    check("...with its status transition", "Running" in info, info[:200])
    check("...and the tenant and site it routed to",
          "ACME" in info and "PLANT1" in info, info[:200])
    # ONE line, not two. There used to be a second INFO record per message — the
    # WebSocket broadcast — naming the same machine again, so the stream carried
    # two records per message for one fact. Mutation testing found it: a mutation
    # that stopped the ACCEPT line naming the machine survived, because the
    # broadcast line was still naming it. Asserted as a count so a third cannot
    # appear either.
    check(f"exactly one INFO record per accepted message "
          f"({len(capture.at(logging.INFO))})",
          len(capture.at(logging.INFO)) == 1, str(capture.at(logging.INFO)))

    print()
    print("=" * 74)
    print("3. EVERY RECORD IS ONE LINE")
    print("=" * 74)
    # JsonFormatter emits one JSON object per line; a record containing a newline
    # splits into a line that cannot be parsed as a record.
    multiline = [m for m in capture.messages() if "\n" in m]
    check(f"no log record contains a newline ({len(multiline)} do)", not multiline,
          str(multiline)[:200])

    print()
    print("=" * 74)
    print("4. THE PAYLOAD IS STILL THERE AT DEBUG")
    print("=" * 74)
    # Removing it entirely would cost the deep-debugging case. It is opt-in, not
    # gone.
    debug = _deliver(logging.DEBUG)
    debug_text = "\n".join(debug.at(logging.DEBUG))
    check("the raw payload is available at DEBUG",
          "14320" in debug_text or "HrsRun" in debug_text,
          f"DEBUG records: {debug.at(logging.DEBUG)[:2]}")

    print()
    print("=" * 74)
    print("5. A DROPPED MESSAGE STILL SAYS WHY")
    print("=" * 74)
    # The rejection warnings are the one thing that must NOT be quieted: a
    # silently dropped message is the failure mode the whole ingest path is
    # written to avoid.
    unrouted = _deliver(logging.INFO, topic="flowmes/NOPE/-/machines")
    warned = "\n".join(unrouted.at(logging.WARNING))
    check("an unprovisioned tenant is still rejected loudly",
          "REJECTED" in warned, f"warnings: {unrouted.at(logging.WARNING)}")
    check("...and the reason names the tenant", "NOPE" in warned, warned[:200])

    bad_shape = _deliver(logging.INFO, payload=["not", "an", "object"])
    check("a non-object payload is still rejected loudly",
          any("skipped" in m.lower() or "reject" in m.lower()
              for m in bad_shape.at(logging.WARNING)),
          str(bad_shape.at(logging.WARNING)))

    print()
    print("=" * 74)
    if failures:
        print(f"{len(failures)} FAILED")
        for failure in failures:
            print(f"  - {failure}")
    else:
        print("ALL CHECKS PASSED")
    print("=" * 74)
    return 1 if failures else 0


def test_mqtt_log_hygiene():
    """The pytest entry point — a suite exposing only main() contributes nothing
    to the coverage job."""
    assert main() == 0, "see the FAIL lines above"


if __name__ == "__main__":
    raise SystemExit(main())
