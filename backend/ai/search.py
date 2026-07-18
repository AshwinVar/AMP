"""Global entity search — type a thing, jump to it (ADR-0007-adjacent).

Answers "where is CO-5001 / SMT-Reflow-01 / the reflow SOP?": one query across
the tenant's operational entities — machines, work orders, customer orders,
inventory items, maintenance tasks, escalations, compliance documents — each hit
carrying the dashboard view that opens it. A read-only projection; auto-scoped
to the tenant (ADR-0002); adds no storage.
"""
import models

name = "search"

LIMIT_PER_TYPE = 4
LIMIT_TOTAL = 10


def build_search(db, tenant: str, q: str) -> dict:
    """Case-insensitive contains-search across the core entities. Returns typed
    hits (label, sublabel, and the view that opens them), a few per type."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"query": q, "results": []}
    like = f"%{q}%"
    results = []

    def take(rows, fmt):
        for r in rows[:LIMIT_PER_TYPE]:
            results.append(fmt(r))

    take(db.query(models.Machine).filter(models.Machine.name.ilike(like)).all(),
         lambda m: {"type": "machine", "id": m.id, "label": m.name,
                    "sublabel": f"{m.status} · {m.line or 'no line'}", "view": "machines"})

    take(db.query(models.WorkOrder).filter(
            models.WorkOrder.work_order_no.ilike(like) | models.WorkOrder.part_number.ilike(like)).all(),
         lambda w: {"type": "work order", "id": w.id, "label": w.work_order_no,
                    "sublabel": f"{w.part_number} · {w.status}", "view": "workorders"})

    take(db.query(models.CustomerOrder).filter(
            models.CustomerOrder.order_no.ilike(like) | models.CustomerOrder.customer_name.ilike(like)
            | models.CustomerOrder.product_name.ilike(like)).all(),
         lambda o: {"type": "customer order", "id": o.id, "label": o.order_no,
                    "sublabel": f"{o.customer_name} · {o.product_name}", "view": "orders"})

    take(db.query(models.InventoryItem).filter(
            models.InventoryItem.item_code.ilike(like) | models.InventoryItem.item_name.ilike(like)).all(),
         lambda i: {"type": "inventory", "id": i.id, "label": i.item_name,
                    "sublabel": f"{i.item_code} · stock {i.current_stock or 0}", "view": "inventory"})

    take(db.query(models.MaintenanceTask).filter(models.MaintenanceTask.task_no.ilike(like)).all(),
         lambda t: {"type": "maintenance", "id": t.id, "label": t.task_no,
                    "sublabel": f"{t.task_type} · {t.status}", "view": "cmms"})

    take(db.query(models.Escalation).filter(models.Escalation.title.ilike(like)).all(),
         lambda e: {"type": "escalation", "id": e.id, "label": e.title,
                    "sublabel": f"{e.severity} · {e.status}", "view": "escalations"})

    take(db.query(models.ComplianceDocument).filter(
            models.ComplianceDocument.document_no.ilike(like) | models.ComplianceDocument.title.ilike(like)).all(),
         lambda d: {"type": "document", "id": d.id, "label": d.title,
                    "sublabel": f"{d.document_no} · {d.approval_status}", "view": "documents"})

    return {"query": q, "results": results[:LIMIT_TOTAL]}
