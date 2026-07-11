# AMP — Pilot Proposal for GMATS Machineries India Pvt Ltd

**Prepared for:** GMATS Machineries India Private Limited, Bengaluru
**Prepared by:** AMP
**Date:** _<fill in>_

---

## 1. The problem we're solving

GMATS currently tracks inventory in Tally, where the same part is entered under
different names and free spares shipped with machines are never deducted — so the
system stock and the physical stock on the rack drift apart. The result is
missing-stock surprises, double-selling, and manual stock-taking.

## 2. What the pilot delivers

A live, GMATS-only inventory system that mirrors how your store actually works:

| Capability | What it does for GMATS |
|---|---|
| **4-bucket stock** | Physical · Reserved · Available · Reorder — always know what's truly sellable |
| **Item aliases** | "1″ Collar" = "1″ Coupler" = "GI Coupler 1″" all update one stock item |
| **Proforma → reserve** | Raising a proforma blocks stock so it can't be double-sold |
| **Tax invoice → deduct** | Final invoice deducts physical stock and prints a GST tax invoice (PDF) |
| **Free spares (MIN)** | Spares shipped free with a compressor are deducted — no more missing stock |
| **Reorder alerts** | Items below minimum flagged "Purchase Required" before you run out |
| **Tally import** | Your existing item master comes in via CSV — no re-typing |
| **Admin controls** | Admin can correct/void any operator mistake; full audit trail |

Tally stays your system of record for accounting. AMP owns the shop-floor truth.

## 3. Pilot scope & timeline

- **Duration:** 2 months (extendable)
- **Scope:** Inventory module for one plant (Bengaluru), up to 5 users
- **Week 1:** Import GMATS item master, set up users and roles, train store + sales
- **Weeks 2–8:** Daily live use; weekly check-ins; we tune to your workflow

## 4. Success criteria (how we both know it worked)

- Item master fully imported and in daily use
- Zero stock mismatch at the end-of-month physical count for tracked items
- Free spares for at least one compressor sale tracked end-to-end
- Store + sales team using it without our help by week 4

## 5. Commercials (suggested — adjust to taste)

| Item | Amount |
|---|---|
| One-time setup & data import | ₹ _<e.g. 15,000>_ (waived if you commit to an annual plan after the pilot) |
| Pilot subscription | ₹ _<e.g. 15,000>_ / month × 2 months |
| **Total pilot** | **₹ _<e.g. 30,000>_** |

After a successful pilot, an annual plan at ₹ _<e.g. 15,000–25,000>_/month per plant
(based on users and modules), with the setup fee credited back.

## 6. Data security (your earlier questions, answered)

- Your data is **isolated to GMATS** — enforced on the server, not just hidden in the UI
- Passwords stored with **bcrypt**; all traffic over **HTTPS**
- Hosted on managed cloud (Railway/AWS) with **daily database backups**
- **Role-based access:** Admin, Supervisor, Operator — operators can't delete or change settings
- Data export available any time as CSV; on-premise deployment available for an annual contract

## 7. What we need from GMATS to start

1. Tally export of your item master (CSV/Excel)
2. List of users and their roles (Admin / Supervisor / Operator)
3. A point of contact in the store and in sales
4. Sign-off on this one-page pilot agreement

---

**Agreed for GMATS Machineries India Pvt Ltd**            **Agreed for AMP**

_____________________________                              _____________________________
Name / Designation / Date                                  Name / Date
