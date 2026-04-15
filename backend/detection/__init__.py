"""
Launch-detection package — modules that feed `launch_signals` and the
unified launch_scorer that aggregates them into tiered alerts.

Each detection module is independent. A signal row is the ONLY way a
module communicates with the rest of the pipeline. The launch_scorer
owns all gating and alerting logic.
"""
