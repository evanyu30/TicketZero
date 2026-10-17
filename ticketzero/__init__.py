"""TicketZero: a recursive autonomous support engineer for Crusoe Managed Inference tickets.

Flowchart defect fixes are referenced as F1..F7 in comments across the package:
  F1 depth/budget gate at the entry of every resolve call, plus per-node caps   (resolver.py, budget.py)
  F2 join barrier before the parent aggregates and re-verifies                  (resolver.py)
  F3 one git worktree per node; only parents merge child patches                (sandbox.py, resolver.py)
  F4 escalation bubbles up to the parent; only depth 0 escalates to a human     (resolver.py, reports.py)
  F5 every node writes findings on close, including negative findings           (resolver.py, store.py)
  F6 acceptance tests frozen at depth 0 (hashed, read-only, patches rejected)   (sandbox.py, patches.py, verify.py)
  F7 Nemotron model routing per role                                            (models.py)
"""
