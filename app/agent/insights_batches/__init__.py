"""MARSOUD-INSIGHTS-AGENT-PROFESSIONAL (2026-08-06) — batch modules
for the analyst tool catalog.

Every module in this package imports the shared @register decorator
from `app.agent.insights_catalog` and stacks its own tool
definitions. `app.agent.insights_tools` imports every batch module
by side effect at boot — no manual list maintenance.
"""
