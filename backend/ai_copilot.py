"""
AI Factory Copilot — a natural-language assistant over live factory data.

It answers plant-floor questions ("why is my OEE low?", "what should I
reorder?"), does AI root-cause analysis, and generates management reports —
grounded in the company's real machines, downtime, OEE, shifts and inventory.

  OFF BY DEFAULT. To turn it on later (when a client signs up):
    1. Get an API key at console.anthropic.com
    2. In Railway -> FlowMES -> Variables, add  ANTHROPIC_API_KEY = <the key>
    3. (optional) AI_MODEL  — defaults to claude-haiku-4-5 (cheapest/fastest)
    4. Redeploy. That's it — the copilot detects the key and switches on.

No code change is needed to connect; the key lives only in the environment.
"""
import os

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

import models
from auth import get_current_user
from database import SessionLocal

# Cheap + fast model by default; override with AI_MODEL (e.g. claude-sonnet-4-6
# for deeper analysis). The key itself is never in code — only in the env.
AI_MODEL = os.environ.get("AI_MODEL", "claude-haiku-4-5")


def _ai_enabled() -> bool:
    """The copilot is on only when an API key is present in the environment."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _build_factory_context(db: Session, tenant: str) -> str:
    """Compact, token-efficient snapshot of the factory for the model to reason over."""
    lines = []

    machines = db.query(models.Machine).all()
    if machines:
        lines.append("MACHINES:")
        for m in machines:
            lines.append(f"- {m.name}: {m.status}, utilization {m.utilization}%, downtime {m.downtime}")

    recs = db.query(models.ProductionRecord).order_by(models.ProductionRecord.id.desc()).limit(10).all()
    if recs:
        from analytics_engine import calculate_oee_from_record
        oees = [calculate_oee_from_record(r)["oee"] for r in recs]
        lines.append(f"AVERAGE OEE (last {len(recs)} runs): {round(sum(oees) / len(oees))}%")

    downs = db.query(models.DowntimeLog).order_by(models.DowntimeLog.id.desc()).limit(8).all()
    if downs:
        lines.append("RECENT DOWNTIME:")
        for d in downs:
            lines.append(f"- {d.reason}: {d.duration}")

    shifts = db.query(models.ShiftData).order_by(models.ShiftData.id.desc()).limit(5).all()
    if shifts:
        lines.append("RECENT SHIFTS (actual/target):")
        for s in shifts:
            lines.append(f"- {s.shift_name}: {s.actual_output}/{s.target_output}")

    # Low stock — tenant aware (GMATS uses its own 4-bucket inventory).
    if tenant == "GMATS":
        items = db.query(models.GmatsItem).filter(models.GmatsItem.tenant_code == "GMATS").all()
        low = [i for i in items if (i.physical_stock - i.reserved_stock) <= i.reorder_level]
        if low:
            lines.append("LOW STOCK:")
            for i in low:
                lines.append(f"- {i.item_name}: available {i.physical_stock - i.reserved_stock} {i.unit} (reorder {i.reorder_level})")
    else:
        items = db.query(models.InventoryItem).all()
        low = [i for i in items if i.current_stock <= i.reorder_level]
        if low:
            lines.append("LOW STOCK:")
            for i in low[:15]:
                lines.append(f"- {i.item_name}: {i.current_stock} {i.unit} (reorder {i.reorder_level})")

    return "\n".join(lines) if lines else "No factory data available yet."


def _ask_claude(system: str, user: str) -> str:
    """Single call to Claude via the official Anthropic SDK (imported lazily so the
    app runs fine even when the package/key are absent)."""
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    resp = client.messages.create(
        model=AI_MODEL,
        max_tokens=1500,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(block.text for block in resp.content if block.type == "text").strip()


def register(app):
    def get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    @app.get("/ai/status")
    def ai_status(current_user: dict = Depends(get_current_user)):
        """Lets the UI show 'connect to enable' vs the live copilot."""
        return {"enabled": _ai_enabled(), "model": AI_MODEL if _ai_enabled() else None}

    @app.post("/ai/ask")
    def ai_ask(payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
        if not _ai_enabled():
            raise HTTPException(status_code=503, detail="AI copilot not connected. Set ANTHROPIC_API_KEY to enable.")
        question = (payload.get("question") or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="Ask a question.")
        tenant = current_user.get("tenant", "DEFAULT")
        context = _build_factory_context(db, tenant)
        system = (
            "You are FlowMES Copilot, a no-nonsense assistant for a factory manager at an Indian SME "
            "manufacturer. Answer using ONLY the factory data provided. If the data doesn't contain the "
            "answer, say so plainly. Be concise and practical — give shop-floor advice a supervisor can act on. "
            "When asked 'why', do a short root-cause analysis from the data."
        )
        try:
            answer = _ask_claude(system, f"Factory data:\n{context}\n\nQuestion: {question}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"AI request failed: {e}")
        return {"answer": answer, "model": AI_MODEL}

    @app.post("/ai/report")
    def ai_report(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
        if not _ai_enabled():
            raise HTTPException(status_code=503, detail="AI copilot not connected. Set ANTHROPIC_API_KEY to enable.")
        tenant = current_user.get("tenant", "DEFAULT")
        context = _build_factory_context(db, tenant)
        system = (
            "You are FlowMES Copilot. Write a brief daily management report for a factory manager from the data. "
            "Use short sections with these headings: Summary, Machine status, Key issues, Recommended actions. "
            "Be specific and concise — no fluff."
        )
        try:
            report = _ask_claude(system, f"Factory data:\n{context}\n\nWrite today's report.")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"AI request failed: {e}")
        return {"report": report, "model": AI_MODEL}
