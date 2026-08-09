"""Who does an inbound MQTT message belong to?

Every telemetry packet must resolve to EXACTLY ONE tenant before it is allowed
to touch the database, and that resolution must not be forgeable by whatever the
edge gateway happens to put in its JSON.

WHY THE TOPIC AND NOT THE PAYLOAD
---------------------------------
The topic is the only part of an MQTT message a broker can enforce. Mosquitto,
EMQX, HiveMQ and AWS IoT all authenticate the client and then restrict which
topic filters it may publish to. So `flowmes/FACTORY_B/PLANT1/machines` is a
claim the BROKER has already checked; `{"tenant": "FACTORY_B"}` inside the body
is a claim the gateway made about itself, and a compromised or misconfigured
gateway can say anything. Reading tenant from the body would mean any customer's
device could write into any other customer's factory by editing one string.

If the payload also carries a tenant/site, it must AGREE with the topic. A
disagreement is not something to silently prefer one side of — it means the
gateway is misconfigured or is being replayed, so the message is rejected.

WHY tenant/site/machine RATHER THAN A GLOBALLY UNIQUE MACHINE NAME
------------------------------------------------------------------
"CNC-01" is not an identifier, it is a label a maintenance engineer painted on a
machine. Three different customers all have one; one customer with two plants has
two. Any scheme that treats the human-readable name as the primary key is one
new customer away from a collision, and the failure mode is silent: telemetry
lands on somebody else's row and looks completely normal.

FAIL-CLOSED, DELIBERATELY
-------------------------
Anything this module cannot resolve with certainty raises RouteError, and the
caller drops the message. Dropping telemetry is a visible, recoverable failure
(the gap shows up in the data and the log says why). Guessing is an invisible,
unrecoverable one: once a packet is written under the wrong tenant there is
nothing left in the row that says it was a guess.
"""
import re

# Identifiers travel inside topic segments, so they must not contain MQTT's
# separator or wildcards ('/', '+', '#'), and must not be empty. Restricting to
# an explicit safe charset rather than blacklisting those three characters means
# a new special character in some future broker cannot widen this by accident.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# A site is optional in the sense that a single-plant customer has nothing
# meaningful to put there, but the COLUMN is never NULL — see models.Machine.
# "-" is the wire spelling of "no site", chosen because an empty topic segment
# ("a//b") is legal MQTT but invisible when reading logs.
NO_SITE_TOKEN = "-"
NO_SITE = ""


class RouteError(Exception):
    """The message cannot be attributed to exactly one tenant."""


class Route:
    """The resolved (tenant, site) a message is allowed to write to."""

    __slots__ = ("tenant", "site")

    def __init__(self, tenant: str, site: str):
        self.tenant = tenant
        self.site = site

    def __eq__(self, other):
        return (isinstance(other, Route)
                and (self.tenant, self.site) == (other.tenant, other.site))

    def __repr__(self):
        return f"Route(tenant={self.tenant!r}, site={self.site!r})"


def topic_filters(prefix: str, legacy_tenant: str = "") -> list:
    """The topic filters to subscribe to.

    `{prefix}/+/+/machines` does NOT match `{prefix}/machines` — MQTT's '+'
    matches exactly one segment and never zero — so the legacy single-tenant
    topic needs its own subscription. It is only subscribed when a legacy tenant
    is configured, so a fresh multi-tenant deployment never even receives
    messages it would have to reject.
    """
    filters = [f"{prefix}/+/+/machines"]
    if legacy_tenant:
        filters.append(f"{prefix}/machines")
    return filters


def _identifier(value, what: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.match(value):
        raise RouteError(f"invalid {what}: {value!r}")
    return value


def parse_topic(topic: str, prefix: str, legacy_tenant: str = "",
                legacy_site: str = NO_SITE) -> Route:
    """Resolve the tenant and site a topic addresses, or raise RouteError.

    `{prefix}/{tenant}/{site}/machines` is the real form. `{prefix}/machines` is
    the pre-multi-tenant form and resolves ONLY to an explicitly configured
    MQTT_LEGACY_TENANT: an existing single-customer deployment keeps working by
    setting one environment variable, and a deployment that has not set it drops
    those messages rather than inventing an owner for them.
    """
    if not isinstance(topic, str):
        raise RouteError(f"topic is not a string: {topic!r}")

    parts = topic.split("/")
    head = parts[: len(prefix.split("/"))]
    if head != prefix.split("/"):
        raise RouteError(f"topic outside prefix {prefix!r}: {topic!r}")
    rest = parts[len(prefix.split("/")):]

    if rest == ["machines"]:
        if not legacy_tenant:
            raise RouteError(
                f"legacy topic {topic!r} carries no tenant and MQTT_LEGACY_TENANT "
                f"is not set — refusing to guess an owner")
        return Route(_identifier(legacy_tenant, "legacy tenant"),
                     NO_SITE if legacy_site in ("", NO_SITE_TOKEN)
                     else _identifier(legacy_site, "legacy site"))

    if len(rest) != 3 or rest[2] != "machines":
        raise RouteError(f"unrecognised topic shape: {topic!r}")

    tenant = _identifier(rest[0], "tenant")
    site_token = rest[1]
    site = NO_SITE if site_token == NO_SITE_TOKEN else _identifier(site_token, "site")
    return Route(tenant, site)


def check_payload_agrees(route: Route, payload: dict) -> None:
    """A payload may restate its tenant/site, but may never contradict the topic.

    Gateways commonly echo their identity into the body, and that echo is
    genuinely useful for debugging. It is checked rather than trusted: if the
    body says FACTORY_A while the broker authorised FACTORY_B, exactly one of
    those is right and nothing here can tell which, so the message is dropped.
    """
    claimed_tenant = payload.get("tenant") or payload.get("tenant_code")
    if claimed_tenant is not None and claimed_tenant != route.tenant:
        raise RouteError(
            f"payload tenant {claimed_tenant!r} contradicts topic tenant "
            f"{route.tenant!r}")

    claimed_site = payload.get("site")
    if claimed_site is not None:
        normalised = NO_SITE if claimed_site == NO_SITE_TOKEN else claimed_site
        if normalised != route.site:
            raise RouteError(
                f"payload site {claimed_site!r} contradicts topic site "
                f"{route.site!r}")


def machine_identifier(payload: dict) -> str:
    """The machine label within its (tenant, site). Validated to the same charset
    as tenant/site: it is not a topic segment today, but it IS half of a database
    identity, and an unbounded string there invites both collisions and junk rows
    from a malfunctioning gateway."""
    return _identifier(payload.get("machine"), "machine")
