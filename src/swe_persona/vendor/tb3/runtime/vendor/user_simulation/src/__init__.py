"""Lightweight, proxy-free SWE-bench harness for comparing CLI coding agents.

Runs the latest Claude Code CLI (direct to api.anthropic.com on real Sonnet)
and the latest GitHub Copilot CLI (its own native backend) on SWE-bench Verified,
producing result.json output byte-compatible with the reference ADK-Arena pipeline.

No token proxy: each agent talks to its own backend directly. Token/cost metrics
are parsed from the CLI's own structured output.
"""
