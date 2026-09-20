"""The AMP tool registry: typed, authorized reads a model may ASK for (ADR-0022).

THE CHAIN, AND WHERE A MODEL SITS IN IT
---------------------------------------
    USER -> AUTHENTICATION -> RBAC -> TENANT / OEM / CONSENT -> AMP TOOL -> DATA -> MODEL

A language model never touches the database and never decides who may see what.
It may do one thing: name a tool from this registry and offer arguments. AMP
then decides, in this order, and every "no" is a result rather than an error:

  1. the tool exists (a name, looked up here; never an attribute or a path);
  2. the principal is a factory principal. The tenant comes from the
     authenticated request (`Principal.from_user`), never from the model, and
     an OEM sentinel tenant is refused outright;
  3. the role may call it. A tool's roles are COPIED from the REST route whose
     data it serves (`mirrors`), and test_copilot_tools_no_wider_than_routes.py
     reads the live app to check that no tool admits a role, or skips a plan
     pack, that its route does not;
  4. the tenant's plan licenses the pack that route belongs to;
  5. the arguments validate against the tool's typed parameters. An unknown key
     is refused, so `{"tenant": "FACTORY_B"}` is refused rather than ignored,
     and no parameter may be named like a scope (tenant, oem, company...);
  6. AMP binds the tenant for the duration of the read and runs the handler,
     which calls the same read-model the dashboard uses.

A tool that raises returns FAILED with a generic sentence. The exception text
is logged, not returned, because error strings can carry data.
"""
import re
import time
from dataclasses import dataclass, field

import logging_config
import module_manifest
import tenancy
from ai import evidence as ev

log = logging_config.get_logger(__name__)

# A parameter or argument NAME that would let a caller choose scope. None exists
# today and none may be added: scope is AMP's, from the authenticated request.
_SCOPE_WORDS = re.compile(r"tenant|oem|company|workspace|organi[sz]ation|^org$|^factory$|^site_code$")

MAX_STR = 120


@dataclass(frozen=True)
class Param:
    """One typed parameter. Only int and str exist, both bounded."""
    type: str
    description: str
    required: bool = False
    default: object = None
    minimum: int = None
    maximum: int = None
    max_length: int = MAX_STR

    def __post_init__(self):
        if self.type not in ("int", "str"):
            raise ValueError(f"parameter type {self.type!r} is not supported")
        if self.type == "int" and (self.minimum is None or self.maximum is None):
            raise ValueError("an int parameter must be bounded")

    def schema(self) -> dict:
        if self.type == "int":
            return {"type": "integer", "description": self.description,
                    "minimum": self.minimum, "maximum": self.maximum}
        return {"type": "string", "description": self.description, "maxLength": self.max_length}


@dataclass(frozen=True)
class Principal:
    """Who is asking, as AMP established it. Built by the route, never by a model."""
    tenant: str
    role: str
    username: str = ""
    preview: bool = False

    @classmethod
    def from_user(cls, current_user) -> "Principal":
        """From the authenticated request: the effective tenant (which honours the
        founder's company preview exactly as every read-model route does) and the
        token's own role."""
        user = current_user or {}
        return cls(tenant=tenancy.request_tenant(user), role=str(user.get("role") or ""),
                   username=str(user.get("sub") or ""), preview=tenancy.is_preview(user))


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: object
    mirrors: str
    roles: tuple = ()
    params: dict = field(default_factory=dict)
    view: str = None
    domain: str = ""

    def schema(self) -> dict:
        """The JSON-schema form a model is shown. No scope parameter exists to show."""
        return {"name": self.name, "description": self.description,
                "parameters": {"type": "object", "additionalProperties": False,
                               "properties": {k: p.schema() for k, p in self.params.items()},
                               "required": [k for k, p in self.params.items() if p.required]}}


REGISTRY = {}


def register(tool: Tool) -> Tool:
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", tool.name):
        raise ValueError(f"tool name {tool.name!r} is not a plain identifier")
    if tool.name in REGISTRY:
        raise ValueError(f"tool {tool.name!r} registered twice")
    if not tool.mirrors.startswith("/"):
        raise ValueError(f"{tool.name}: `mirrors` must name the REST route it serves")
    for pname in tool.params:
        if _SCOPE_WORDS.search(pname):
            raise ValueError(f"{tool.name}: parameter {pname!r} names a scope; scope is AMP's")
    REGISTRY[tool.name] = tool
    return tool


def tool(name, description, mirrors, view=None, roles=(), params=None, domain=""):
    """Decorator: register `fn(db, tenant, **args) -> ToolResult` as a tool."""
    def deco(fn):
        register(Tool(name=name, description=description, handler=fn, mirrors=mirrors,
                      roles=tuple(roles), params=dict(params or {}), view=view, domain=domain))
        return fn
    return deco


def validate_args(t: Tool, args) -> tuple:
    """(clean args, [errors]). Strict: unknown keys, wrong types and out-of-range
    values are errors. A numeric string for an int is accepted (models often send
    "7"), nothing else is coerced."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {}, ["arguments must be an object"]
    errors, clean = [], {}
    for key in args:
        if not isinstance(key, str) or key not in t.params:
            errors.append(f"unknown argument {str(key)[:40]!r}")
    for key, p in t.params.items():
        if key not in args or args[key] is None:
            if p.required:
                errors.append(f"{key} is required")
            elif p.default is not None:
                clean[key] = p.default
            continue
        v = args[key]
        if p.type == "int":
            if isinstance(v, bool):
                errors.append(f"{key} must be a whole number")
                continue
            if isinstance(v, str) and re.fullmatch(r"\s*-?\d{1,9}\s*", v):
                v = int(v)
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            if not isinstance(v, int):
                errors.append(f"{key} must be a whole number")
                continue
            if v < p.minimum or v > p.maximum:
                errors.append(f"{key} must be between {p.minimum} and {p.maximum}")
                continue
            clean[key] = v
        else:
            if not isinstance(v, str):
                errors.append(f"{key} must be text")
                continue
            v = v.strip()
            if not v or len(v) > p.max_length:
                errors.append(f"{key} must be 1-{p.max_length} characters")
                continue
            clean[key] = v
    return clean, errors


def _licensed_packs(tenant):
    """The tenant's licensed packs through THE plan gate's own reader (cached,
    fail-open exactly like the middleware). Indirected so a test can bind it to
    its own in-memory database."""
    import plan_gate   # lazy: plan_gate pulls in the platform routes
    return plan_gate.licensed_packs(tenant)


def pack_of(t: Tool):
    return module_manifest.pack_for_path(t.mirrors.split("{")[0].rstrip("/") or "/")


def permitted(t: Tool, principal: Principal) -> bool:
    """Role check only: the question a catalog asks before showing a tool."""
    return not t.roles or principal.role in t.roles


def run_tool(db, principal: Principal, name, args=None) -> ev.ToolResult:
    """Run one tool for one principal, or say why not. Never raises for a
    caller's input; a handler failure becomes FAILED."""
    t = REGISTRY.get(name) if isinstance(name, str) else None
    if t is None:
        return ev.refusal(str(name)[:80], ev.NOT_FOUND, "There is no such AMP tool.")
    if (not isinstance(principal, Principal) or not principal.tenant
            or tenancy.is_reserved_tenant_code(principal.tenant)):
        return ev.refusal(t.name, ev.NOT_PERMITTED,
                          "The Copilot answers for a factory workspace only.")
    if not permitted(t, principal):
        # The refusal does NOT name the roles that would be allowed. A tool result
        # can travel to the language model as the draft it is asked to word
        # (orchestrator._compose -> llm.phrase), and AMP tells a model nothing
        # about who is asking -- not the tenant, not the username, not the role.
        # test_copilot_local_provider.py section 5 asserts exactly that on every
        # request body. Naming the permitted roles would also tell any user the
        # shape of the role model, which is not theirs to learn from a refusal.
        return ev.refusal(t.name, ev.NOT_PERMITTED,
                          "Your role can't see this in AMP. Someone with wider access in your "
                          "workspace can open it.")
    pack = pack_of(t)
    if not module_manifest.pack_licensed(pack, frozenset()):
        packs = _licensed_packs(principal.tenant)
        if packs is not None and not module_manifest.pack_licensed(pack, packs):
            label = module_manifest.pack_labels().get(pack, pack)
            return ev.refusal(t.name, ev.NOT_LICENSED,
                              f"That's part of the {label} pack, which this workspace doesn't include.")
    clean, errors = validate_args(t, args)
    if errors:
        return ev.refusal(t.name, ev.INVALID_ARGUMENTS, "; ".join(errors)[:300])
    token = tenancy.set_current_tenant(principal.tenant)
    started = time.perf_counter()
    try:
        result = t.handler(db, principal.tenant, **clean)
    except Exception as e:   # noqa: BLE001 - a failed read is a result, not a 500
        log.info("[COPILOT TOOL] %s failed for %s: %s", t.name, principal.tenant, type(e).__name__)
        return ev.refusal(t.name, ev.FAILED, "That read failed, so nothing is answered from it.")
    finally:
        tenancy.reset_current_tenant(token)
    if not isinstance(result, ev.ToolResult):
        return ev.refusal(t.name, ev.FAILED, "That read failed, so nothing is answered from it.")
    result.elapsed_ms = round((time.perf_counter() - started) * 1000)
    return result


def catalog(principal: Principal) -> list:
    """The tools this principal's role may call, as schemas, in a stable order.
    Licence is checked at run time, where the refusal can say which pack."""
    return [REGISTRY[n].schema() for n in sorted(REGISTRY) if permitted(REGISTRY[n], principal)]
