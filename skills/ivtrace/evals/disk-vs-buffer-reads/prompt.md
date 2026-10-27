---
tags: [ivtrace]
max_turns: 30
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Bash, Skill]
---

I exported a query run from Internals Viewer to clustered-index-seek.ivtrace in this folder. What query was it, and how many pages did SQL Server read from disk versus the buffer pool to run it? Which table or index were they from?

Input files: clustered-index-seek.ivtrace
