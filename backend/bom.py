"""Bill of Materials — the recipe book.

part_number -> {raw material code, units consumed per finished unit, finished good code}

This is domain knowledge, kept out of the HTTP layer so any module (e.g. the
inventory event subscriber) can consume it without importing `main`.
"""

PART_BOM = {
    "SHAFT-001": {"raw": "RM-STEEL-001",     "consume_per_unit": 2,  "fg": "FG-SHAFT-001"},
    "PLATE-002": {"raw": "RM-SHEET-002",     "consume_per_unit": 1,  "fg": "FG-PLATE-002"},
    "BEAR-003":  {"raw": "RM-STEEL-001",     "consume_per_unit": 3,  "fg": None},
    "GEAR-004":  {"raw": "RM-ALUM-003",      "consume_per_unit": 2,  "fg": "FG-GEAR-003"},
    "ASSY-005":  {"raw": None,               "consume_per_unit": 0,  "fg": "FG-BRACKET-004"},
    "PKG-006":   {"raw": "PKG-MAT-001",      "consume_per_unit": 1,  "fg": None},
}
