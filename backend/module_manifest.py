"""Loads modules.json — the single source of truth for AMP's plug-and-play
modules. Both the plan-gate (plan_gate.py) and the GET /modules endpoint read
the manifest through here, so the pack / route / view definitions live in ONE
editable file instead of being duplicated across the backend gate and the
frontend nav. Adding or removing a module is a one-file change (modules.json).
"""
import json
import os

_PATH = os.path.join(os.path.dirname(__file__), "modules.json")


def _load():
    with open(_PATH, encoding="utf-8") as f:
        return json.load(f)


try:
    MANIFEST = _load()
    PACKS = MANIFEST.get("packs", [])
except Exception as e:  # pragma: no cover - the manifest ships with the code
    print(f"[MODULES] manifest load failed, using empty manifest: {e}")
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


def pack_labels():
    """pack id -> human label (used in the plan-gate's 403 message)."""
    return {p["id"]: p["label"] for p in PACKS}


def always_open_packs():
    """Pack ids the API never gates — core basics + account admin (gated=false)."""
    return {p["id"] for p in PACKS if not p.get("gated")}


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
