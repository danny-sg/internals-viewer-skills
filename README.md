# internals-viewer-skills
LLM skills for Internals Viewer

A skill is a folder with a `SKILL.md` that tells an LLM agent what the skill does, when to use it and how, together
with the scripts and reference material it needs. Each skill here is self-contained under `skills/`.

## ivtrace

Reads a query run exported from Internals Viewer (**Query > Export to file**, an `.ivtrace` file) and answers
questions about the captured query: the SQL and actual plan, which pages were read from disk or the buffer pool,
waits, locks and latches, columnstore segment scans, the call tree of SQL Server engine functions behind each
operator and, for a Full Trace run, every logged call with its arguments, the trace timeline and the query's
memory. It includes a reader, `scripts/ivtrace.py` (Python 3.10 or later, standard library only), that the agent
runs against the file, and references on the file format, the event types, SQL Server symbol names and how to
investigate a Full Trace.

Once installed, ask about an export in plain words, for example "what did SQL Server read to run the query in
`C:\exports\orders.ivtrace`?" or "explain what the hash join in this trace is doing". The agent runs the reader
and answers from the file. The reader needs Python, so the agent must be able to run code.

### Installing in Claude Code

This repository is a Claude Code plugin marketplace, and the plugin `internals-viewer` holds the skills. Add the
marketplace once, then install the plugin:

```bash
claude plugin marketplace add danny-sg/internals-viewer-skills
claude plugin install internals-viewer@internals-viewer-skills
```

Inside Claude Code, `/plugin marketplace add danny-sg/internals-viewer-skills` adds the marketplace and
`/plugin install internals-viewer@internals-viewer-skills` opens the plugin's page to choose a scope and install.
A running session picks the plugin up after `/reload-plugins`; otherwise it is there in the next session. The
plugin's skills are namespaced, so the skill is `/internals-viewer:ivtrace`. It also triggers on its own when an
`.ivtrace` file or an Internals Viewer export comes up.

To get a new release later, refresh the marketplace and update the plugin:

```bash
claude plugin marketplace update internals-viewer-skills
claude plugin update internals-viewer@internals-viewer-skills
```

To use the skill without the plugin, copy its folder into a project's `.claude\skills` folder, or into
`~\.claude\skills` to have it in every project. It is then available in new sessions as `/ivtrace`:

```powershell
git clone https://github.com/danny-sg/internals-viewer-skills.git
Copy-Item -Recurse internals-viewer-skills\skills\ivtrace "$env:USERPROFILE\.claude\skills\ivtrace"
```

```bash
git clone https://github.com/danny-sg/internals-viewer-skills.git
cp -r internals-viewer-skills/skills/ivtrace ~/.claude/skills/ivtrace
```

### Installing in Claude.ai and the Claude desktop app

Zip the `skills\ivtrace` folder so that the zip holds `ivtrace\SKILL.md`, then upload it under
**Settings > Capabilities > Skills**. Attach the `.ivtrace` file to a conversation and ask about it.

### Using the reader on its own

```bash
python skills/ivtrace/scripts/ivtrace.py "C:\exports\orders.ivtrace" summary
python skills/ivtrace/scripts/ivtrace.py "C:\exports\orders.ivtrace" --help
```
