---
tags: [ivtrace]
max_turns: 30
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Bash, Skill]
---

Can you look at columnstore-point-lookup.ivtrace in this folder and tell me which rowgroups of the columnstore index were scanned or skipped, and how the TradeId predicate was applied?

Input files: columnstore-point-lookup.ivtrace
