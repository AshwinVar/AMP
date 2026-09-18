"""Loads modules.json — the single source of truth for AMP's plug-and-play
modules. Both the plan-gate (plan_gate.py) and the GET /modules endpoint read
the manifest through here, so the pack / route / view definitions live in ONE
editable file instead of being duplicated across the backend gate and the
frontend nav. Adding or removing a module is a one-file change (modules.json).
"""
import json
import os

import logging_config

log = logging_config.get_logger(__name__)

_PATH = os.path.join(os.path.dirname(__file__), "modules.json")


def _load():
    with open(_PATH, encoding="utf-8") as f:
        return json.load(f)


try:
    MANIFEST = _load()
    PACKS = MANIFEST.get("packs", [])
except Exception as e:  # pragma: no cover - the manifest ships with the code
    log.info(f"[MODULES] manifest load failed, using empty manifest: {e}")
    MANIFEST = {"version": 0, "plans": [], "packs": []}
    PACKS = []


def path_packs():
    """[(route_prefix, pack_id)] for every GATED pack — the plan-gate's map."""
    out = []
    for p in PACKS:
        if p.get("gated"):
            for r in p.get("routes", []):
                out.append((r, p["id"]))
    return out


def pack_for_path(path, prefixes=None):
    """The gated module pack a request path belongs to, or None if ungated.
    Longest matching prefix wins. The plan gate (plan_gate.pack_for_path) and the
    approval lock (approvals.decision_api_pack) both resolve through here, so a
    path belongs to the same pack wherever the question is asked."""
    best = None
    for prefix, pack in (path_packs() if prefixes is None else prefixes):
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, pack)
    return best[1] if best else None


def enabled_pack_ids(enabled_modules):
    """The pack ids in a TenantConfig.enabled_modules CSV, as a frozenset."""
    return frozenset(m for m in (enabled_modules or "").split(",") if m)


def pack_licensed(pack, packs):
    """THE plan-gate rule: may a tenant licensed for ``packs`` use ``pack``?
    An ungated path (pack None) and the always-open packs (core, admin) are
    usable by everyone; anything else needs its pack in the licence."""
    return pack is None or pack in always_open_packs() or pack in packs


def pack_labels():
    """pack id -> human label (used in the plan-gate's 403 message)."""
    return {p["id"]: p["label"] for p in PACKS}


def always_open_packs():
    """Pack ids the API never gates — core basics + account admin (gated=false)."""
    return {p["id"] for p in PACKS if not p.get("gated")}


def valid_pack_ids():
    """The set of pack ids the manifest defines — used to validate a tenant's
    enabled_modules so an unknown/typo'd pack can't be stored."""
    return {p["id"] for p in PACKS}


def plan_bundles():
    """{plan_name: [pack_id, ...]} — which packs each subscription plan bundles,
    derived from each pack's ``plans``. The SaaS admin applies one of these to a
    tenant (POST /tenant-configs/{code}/apply-plan), so assigning a plan sets the
    exact modules that appear in that tenant's AMP."""
    return {
        plan: [p["id"] for p in PACKS if plan in p.get("plans", [])]
        for plan in MANIFEST.get("plans", [])
    }


def plan_modules(plan):
    """A manifest plan's bundle as a TenantConfig.enabled_modules CSV. The one
    place a plan's packs are read: SaaS Admin (platform_routes.apply_plan_tier),
    the founder's apply-plan, first-seen tenants and the model default all come
    through here (test_plan_bundles_one_rule.py). KeyError for an unknown plan."""
    return ",".join(plan_bundles()[plan])


def packs_for_tenant(enabled_ids):
    """Every pack annotated ``enabled`` for a tenant whose subscription is
    ``enabled_ids`` (the pack ids from TenantConfig.enabled_modules). This is the
    shape the frontend renders its nav from — a module appears only when its
    pack is in the tenant's subscription."""
    enabled = set(enabled_ids or [])
    return [
        {
            "id": p["id"],
            "label": p["label"],
            "description": p.get("description", ""),
            "tagline": p.get("tagline", ""),
            "color": p.get("color", ""),
            "gated": bool(p.get("gated")),
            "plans": p.get("plans", []),
            "enabled": p["id"] in enabled,
            "views": p.get("views", []),
        }
        for p in PACKS
    ]
