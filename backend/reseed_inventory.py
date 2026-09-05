"""Run this once to clear and reseed inventory data."""
from database import SessionLocal
import models
from factory_simulator import _inventory, _inventory_transactions

db = SessionLocal()

db.query(models.InventoryTransaction).delete()
db.query(models.PurchaseOrder).delete()
db.query(models.InventoryItem).delete()
db.commit()
print("Cleared inventory tables")

_inventory(db)
_inventory_transactions(db)
print("Done — inventory reseeded with 25 items and 42 transactions")
db.close()
