"""The gateway's configuration. Data, never code.

THE TEST THIS FILE HAS TO PASS is a controls engineer changing a register
address at 4pm on a commissioning day, restarting the gateway, and seeing the
right number — without a Python file being edited, without a deploy, and without
anyone from AMP on the call. If a normal pilot mapping cannot be expressed here,
this file has failed and the sprint's tag-mapping rule is not met.

SECRETS ARE NOT CONFIGURATION. A config file gets emailed, pasted into a ticket,
copied to a second gateway and checked into a customer's repo. So this loader
REFUSES a literal password: credentials are named here and read from the
environment, which is the one place a value can live without travelling with the
file. The refusal is not advisory — a config with `password: hunter2` does not
start, and says exactly what to write instead.

VALIDATION IS A GATE, NOT A WARNING. Everything is checked before a single
socket is opened: every machine has a name, every protocol is one AMP actually
speaks, every mapping passes `mapping.validate()`. A gateway that starts and
then fails on the twentieth packet has already written nonsense into a plant's
history; one that refuses to start has not.
"""
import json
import os

from . import mapping as mapping_mod

try:                                     # pragma: no cover - optional
    import yaml
    YAML = True
except ImportError:                      # pragma: no cover
    yaml = None
    YAML = False

PROTOCOLS = ("opcua", "modbus")

# Keys that must never hold a value in the file itself. The `_env` form of each
# is the supported way to say where the value lives.
SECRET_KEYS = ("password", "key", "gateway_key", "secret", "token", "passphrase")

DEFAULT_POLL_INTERVAL_S = 2.0


class ConfigError(ValueError):
    """A configuration AMP Edge refuses to run, naming what to fix."""


def load(path) -> dict:
    """Read a YAML or JSON config off disk. Does not validate — `validate()` does."""
    if not os.path.exists(path):
        raise ConfigError(f"no configuration at {path}")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith((".yaml", ".yml")):
        if not YAML:
            raise ConfigError(
                "this config is YAML but PyYAML is not installed. Either install the edge "
                "requirements or write the same config as .json.")
        try:
            data = yaml.safe_load(text)
        except Exception as e:                       # noqa: BLE001
            raise ConfigError(f"{path} is not valid YAML: {e}")
    else:
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ConfigError(f"{path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"{path} should contain a mapping at the top level")
    return data


def validate(raw: dict) -> dict:
    """Every problem at once, then a resolved config or ConfigError.

    All problems, not the first: an engineer fixing a tag list wants the whole
    list, not twelve restarts.
    """
    problems = []
    raw = raw or {}

    _refuse_literal_secrets(raw, problems, path="")

    amp = raw.get("amp") or {}
    if not amp.get("host"):
        problems.append("amp.host: where to publish to (the AMP broker) is missing")
    for field in ("tenant", "site"):
        if not amp.get(field):
            problems.append(f"amp.{field}: required — it is how AMP knows whose data this is, "
                            f"and which site of theirs")
    tenant, site = str(amp.get("tenant") or ""), str(amp.get("site") or "")
    for label, value in (("tenant", tenant), ("site", site)):
        if value and not _is_topic_segment(value):
            problems.append(f"amp.{label}: {value!r} cannot be part of an MQTT topic — letters, "
                            f"numbers, dot, dash and underscore only")

    gateway = raw.get("gateway") or {}
    if gateway.get("id") and not gateway.get("key_env"):
        problems.append("gateway.key_env: a gateway id was given with no key_env, so nothing can "
                        "be signed. AMP issues both together.")
    if gateway.get("key_env") and not os.environ.get(str(gateway["key_env"])):
        problems.append(f"gateway.key_env names {gateway['key_env']}, which is not set in this "
                        f"process's environment")

    machines = raw.get("machines")
    if not machines:
        problems.append("machines: nothing to read — at least one machine is required")
    resolved = []
    for i, entry in enumerate(machines or []):
        resolved.append(_machine(entry, i, problems))

    names = [m.get("name") for m in resolved if m.get("name")]
    for name in set(names):
        if names.count(name) > 1:
            problems.append(f"machines: {name!r} appears {names.count(name)} times. Two entries of "
                            f"one name at one site are the same machine to AMP, and the second "
                            f"would overwrite the first.")

    if problems:
        raise ConfigError("; ".join(problems))

    return {
        "amp": dict(amp),
        "gateway": dict(gateway),
        "buffer": dict(raw.get("buffer") or {}),
        "machines": resolved,
    }


def _machine(entry, i, problems) -> dict:
    where = f"machines[{i}]"
    if not isinstance(entry, dict):
        problems.append(f"{where} is not an object")
        return {}
    name = entry.get("name")
    if not name:
        problems.append(f"{where}.name: required — it must match the machine already in AMP, "
                        f"exactly, or AMP will register a second one")
    where = f"machines[{name or i}]"
    protocol = str(entry.get("protocol") or "").lower()
    if protocol not in PROTOCOLS:
        problems.append(f"{where}.protocol: {protocol or 'missing'!r} — AMP Edge speaks "
                        f"{' and '.join(PROTOCOLS)}. Anything else is not supported, whatever a "
                        f"datasheet says the PLC can do.")
    connection = entry.get("connection") or {}
    if not isinstance(connection, dict):
        problems.append(f"{where}.connection is not an object")
        connection = {}
    if protocol == "opcua" and not connection.get("url"):
        problems.append(f"{where}.connection.url: required for opcua, e.g. opc.tcp://10.0.0.5:4840")
    if protocol == "modbus" and not connection.get("host"):
        problems.append(f"{where}.connection.host: required for modbus, e.g. 10.0.0.5")

    try:
        poll = float(entry.get("poll_interval") or DEFAULT_POLL_INTERVAL_S)
    except (TypeError, ValueError):
        problems.append(f"{where}.poll_interval is not a number")
        poll = DEFAULT_POLL_INTERVAL_S
    if poll <= 0:
        problems.append(f"{where}.poll_interval must be greater than zero")
    if poll < 0.2:
        # Not refused: a press genuinely needs fast sampling. But a 50ms poll of
        # forty tags is 800 reads a second at the PLC, which is how a gateway
        # takes a production line down, and nobody should reach it by accident.
        problems.append(f"{where}.poll_interval of {poll}s polls the PLC very hard. If a signal "
                        f"really is that fast, subscribe to it (opcua) rather than polling.")

    tags = entry.get("tags") or []
    mappings = []
    if not tags:
        problems.append(f"{where}.tags: no tags mapped, so this machine would report nothing")
    else:
        try:
            mappings = mapping_mod.validate(tags)
        except mapping_mod.MappingError as e:
            problems.append(f"{where}.tags: {e}")

    return {
        "name": name,
        "protocol": protocol,
        "connection": _resolve_env(connection, problems, where),
        "poll_interval": poll,
        "tags": tags,
        "mappings": mappings,
    }


def _resolve_env(connection: dict, problems=None, where="connection") -> dict:
    """`password_env: PLC_PW` becomes `password: <value>`, in memory only.

    The resolved dict is handed to the adapter and never written anywhere: it
    does not appear in `describe()`, in the diagnostic export, or in a log line.
    """
    out = {}
    for key, value in connection.items():
        if key.endswith("_env"):
            name = str(value)
            resolved = os.environ.get(name)
            if resolved is None:
                # COLLECTED, not raised. Raising here aborted the whole
                # validation pass, so an engineer with three unset variables was
                # told about one, fixed it, and was told about the next -- the
                # exact round-tripping this module's docstring promises not to
                # do.
                message = (f"{where}.connection.{key} names the environment variable {name}, "
                           f"which is not set")
                if problems is None:
                    raise ConfigError(message)
                problems.append(message)
                continue
            out[key[:-4]] = resolved
        else:
            out[key] = value
    return out


def _refuse_literal_secrets(node, problems, path):
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key).lower() in SECRET_KEYS and value not in (None, ""):
                problems.append(
                    f"{here}: a secret must not be written in the config file. Use {key}_env "
                    f"with the NAME of an environment variable instead, and set the value in "
                    f"the environment (config files get emailed; environments do not).")
            _refuse_literal_secrets(value, problems, here)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _refuse_literal_secrets(item, problems, f"{path}[{i}]")


def _is_topic_segment(value: str) -> bool:
    """The same shape AMP's own mqtt_identity enforces, kept in step deliberately."""
    if not value or len(value) > 64:
        return False
    if not (value[0].isalnum()):
        return False
    return all(c.isalnum() or c in "_.-" for c in value)


def redact(config: dict) -> dict:
    """A copy safe to print, export or attach to a support ticket.

    JSON-safe by construction: the parsed `mappings` objects are dropped rather
    than serialised, because `tags` already holds the same thing as plain data
    and a diagnostic export that raises while being written is no export at all.
    """
    def scrub(node):
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if key == "mappings":
                    continue
                if str(key).lower() in SECRET_KEYS:
                    out[key] = "<redacted>"
                else:
                    out[key] = scrub(value)
            return out
        if isinstance(node, list):
            return [scrub(v) for v in node]
        return node
    return scrub(config)
