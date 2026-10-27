---
name: ivtrace
description: Explain how SQL Server executed a query from an Internals Viewer run export (an .ivtrace file) - the actual plan and what each operator did, the pages it read from disk or the buffer pool, waits, locks and latches, columnstore segment scans, the call tree of engine functions behind each operator, and for a Full Trace run every logged call with its arguments, the trace timeline and the query's memory. Internals Viewer is a learning tool for storage engine and query execution internals, so the export is the exact record of what happened and the answer teaches the mechanism at whatever depth the question asks, from "what is the hash join doing here" to "what are the scratch buffers in the RLE decompression for". Use this whenever a user mentions an .ivtrace file, an Internals Viewer export, run, Full Trace or call tree, or asks what SQL Server did or how something worked while running a query they captured, even if they only say "look at this trace".
---

# Reading an Internals Viewer export

## What this is for

Internals Viewer is a tool for understanding how the SQL Server storage engine and query processor work, by
watching them execute a real query in detail. It is not a tool for tuning a query. Deciding whether the plan was a
good one, why the estimates differed from the actual rows, or which index would have been better is the query
optimizer's domain and outside what the tool records. What the export holds is exactly what the engine did with
the plan it was given, down to each function call, and the job here is to explain that: "this used a hash join,
which reads one input into a hash table and probes it with the other"; "here is how it works in this run: the build
phase is `CQScanHash::Open` calling `GetRow` into the underlying seek until it is exhausted"; "yes, it filters
there: the predicate is on the seek in the plan, and the evaluation calls recorded under that operator show each
comparison being made". Understanding the mechanism this well is what makes troubleshooting possible, but the
understanding is the product.

The user may be new to SQL Server or an expert in its internals. The complexity of the question sets the level of
the answer, and the trace can answer at any level because it records everything that happened.

## What the file is

An `.ivtrace` file is a zip written by Internals Viewer's **Query > Export to file**. It holds one query run: the
SQL, the Messages pane, the actual execution plan, every captured event, any transaction log records, and the call
tree of SQL Server engine functions. A run recorded with **Full Trace** (Time Travel Debugging) also holds what the
replay produced: a call tree with a count on every path, the call log with each call's register arguments and return
value, the per-thread timeline of calls, and memory allocations. Not in the file: the result rows (only the columns
and row count), the recording itself, and the database.

The files are large. A call tree entry can be 40 MB with 60,000 lines and the events entry tens of thousands of
lines, so never print a whole entry into the conversation. `scripts/ivtrace.py` reads the zip directly with the
standard library and answers the common questions with filtered, aggregated output. Reach for raw entries only
through its `extract` command or by importing its `Export` class in your own Python.

## Workflow

1. Run `summary` first, every time. It gives the query, server and database, whether the run succeeded, the run
   options that decide how to read everything else, the plans and call trees present, event counts by type, and
   which entries exist and whether the binary sections are in a layout the script reads.
2. Discover in this order, and stop at the level the question needs: the **query** (`summary` and `plan`: what
   ran and the shape of the plan), then the **operators** (`operators`: which operators ran, their phases, rows and
   threads), then the **calls** (`calltree --operator N` for what an operator's code did by phase, `calls` for the
   values a function was passed), then the **memory history**, when the export has it (`memoryhistory` for an
   object's state over the run, `memorymap` for what the memory was). Each level is the big picture for the next.
   The table below maps questions to commands within those levels.
3. Read the references when you need them: [references/events.md](references/events.md) for what each event type
   and field means, [references/format.md](references/format.md) for the exact layout of every entry,
   [references/engine-symbols.md](references/engine-symbols.md) to read SQL Server function names, and
   [references/full-trace-investigations.md](references/full-trace-investigations.md) for how to explain what an
   operator, a batch or a block of memory is doing from a Full Trace.
4. Answer with the evidence: event ids, node ids, page addresses and counts straight from the commands, so every
   claim can be checked against the file, and found in the app by a user who happens to have the run open.

```bash
python <skill-dir>/scripts/ivtrace.py "<file.ivtrace>" summary
python <skill-dir>/scripts/ivtrace.py "<file.ivtrace>" <command> --help
```

`<skill-dir>` is this skill's own folder: the base directory reported when the skill loads, or
`${CLAUDE_PLUGIN_ROOT}/skills/ivtrace` when it is installed as part of the Internals Viewer plugin. The script also
accepts a folder the zip was extracted into in place of the file. Use its `--limit`, `--offset` and `--grep`
options to cut output down rather than piping into `head`.

For a question none of the commands answer, import the reader and work on the parsed data:

```python
import sys; sys.path.insert(0, r"<skill-dir>/scripts")
from ivtrace import Export, CallStore, frame_name, compact_pages
export = Export(r"<file.ivtrace>")
export.manifest                      # manifest.json as a dict
export.read_text("query.sql")        # any text entry; iter_jsonl(name) streams a .jsonl entry as dicts
export.events                        # {EventId: event}; top_level_events(); events_of_type("ReadEventGroup", ...)
export.nodes, export.children        # {Id: node} over every call tree; {Id: [child ids]}
export.node_name(node_id)            # "module!Class::Method"; ancestors(node_id) innermost first; tree("fulltrace")
export.frames                        # {Id: frame}; function_name(address, instance) for call log keys
export.timeline                      # parsed timeline.bin (threads, rows, node_of(raw), memory) or None
CallStore(export)                    # calllog.bin: functions, calls(key), locators_by_call(key)
```

## Which command answers what

| Question | Command |
| --- | --- |
| What ran, where, with what options, how long, did it succeed | `summary` |
| What did the plan look like, estimates vs actual rows, warnings, seek predicates | `plan --detail`, `plan --xml` for the raw showplan |
| Which operators ran, when each started and emitted its first row, rows, threads, memory | `operators`, `operators --frames` |
| How many pages were read, from disk or the buffer pool, for which object, read-ahead | `reads`, `reads --list` |
| What did it wait on | `waits`, `waits --list` |
| What locks did it take, were they escalated, how long were they held | `locks`, `locks --list` |
| Which pages were latched, in what mode | `latches` |
| Columnstore: segments scanned, rowgroups eliminated, encodings, object pool hits | `columnstore`, `plan --batch` |
| Any event in detail, its children and call stack | `events --id N`, `events --type T`, `events --page 1:1048`, `events --grep` |
| Transaction log records captured with a modification | `logrecords --list` |
| What code ran under an operator | `calltree --operator <plan node id>` (6 levels by default; deeper subtrees are marked, open one with `--root <id> --depth N`) |
| The call path to a function, or every place a function was called from | `calltree --find "BPool::Get$" --operator <plan node id>`, `calltree --path <node id>` |
| The most called paths, the biggest allocators | `calltree --hot 20`, `calltree --hot 20 --by inclusive-alloc`, both scoped with `--operator` or `--root` |
| Totals per function across the whole tree, by engine category | `functions`, `functions --by alloc`, `categories` |
| Which functions touched a page or row, which pages a node touched | `locators`, `locators --page 1:1055`, `locators --node <id>` |
| Full Trace: the calls of one function with their arguments and return values | `calls --function "BTreeMgr::Seek"` |
| Full Trace: which functions were logged, how often | `calls` |
| Full Trace: threads, the longest calls by instructions, a node's total instructions | `timeline --top 20`, `timeline --node <id>` |
| Full Trace: memory allocated per thread, peak in use, what the memory was (by C++ class or allocation site) | `memory`, `memorymap --grep CQScanHash`, `memorymap --by site` |
| Full Trace: an operator object's state over the run, field by field, and who wrote it | `memoryhistory --kind iterator`, `memoryhistory --block N`, `--block N --field 0x3B8` |
| Full Trace: a batch's parts and what each vector holds | `memoryhistory --allocated-in "CBpBatch::"`, `--address <hex>`, then `--block N` |
| Full Trace: which iterator object ran each plan node, which produced each output column, where each thread ended | `plannodes`, `output`, `teardowns` |
| Explain what an operator, a sort, a hash join or a batch is doing | [full-trace-investigations.md](references/full-trace-investigations.md) |

## The depth follows the question

"This used a hash join, what does that mean?" wants the idea: the build input is read into a hash table, then each
probe row is hashed to find its matches. "How does it work here?" wants the mechanism from the trace:
`CQScanHash::Open` runs `ConsumeBuild`, which calls `GetRow` on the underlying seek until it is exhausted, and then
each `GetRow` on the join probes with one row from the other side. "Does it filter there?" wants the evidence: the
plan's predicate on the seek, and the predicate evaluations recorded on that operator with their results. "What are
the scratch buffers in the RLE decompression for?" wants the columnstore decode pipeline, the buffers it allocated
by site and the functions that wrote them. Each is answered from the same export at a different depth, and each
answer can offer the next level down.

Do not assume the user has the run open in Internals Viewer. They may have exported it precisely because they are
away from the app, or are asking on behalf of someone else, so every answer has to stand on the export alone, with
the evidence quoted from the commands. Where the app would show the same thing more directly, add it as an aside
that names the assumption: "if you have the run open in Internals Viewer, the Trace Timeline's Evaluations track
on the seek shows each comparison's result". The app's views, for those asides: the Call Tree for the merged paths
and counts, Arguments on a frame for the values it was passed, the Trace Timeline's operator rows with their
GetRow, Reads and Evaluations tracks (toggled under View > Operators or in the Operators pane's right-click menu),
and Memory History for an object's writes over the run.

A Full Trace is where the deepest questions land: "explain what the Sort operator is doing", "what is the batch
using those vectors for", "what does this field of the hash join hold". Answer them from several commands in turn
(entry frames, the call tree by phase with counts, methods and arguments from the call log, allocations by class
and site, the writes to the object with their writers, cost in instructions), organised by phase and cited by node
id, address, block number and position. [full-trace-investigations.md](references/full-trace-investigations.md)
has the method, a worked hash join and the batch memory layout.

## How to read what comes back

**Time.** `TimeUs` and `DurationUs` on events are microseconds from the start of the capture. The query's own window
is `CropStartUs` to `CropEndUs` in the manifest, and the script shows event times as milliseconds from that start
(`+188.000`). The plan's `cpu_time` and `duration` are microseconds, its `QueryTimeStats` milliseconds.

**Full Trace has no clock.** TTD records instructions, not time, and the recorded query runs about 25 times slower
than normal. Positions are shown as hex `Sequence:Steps`: the sequence number is shared across threads (so lanes line
up) and advances wherever a thread synchronises, and steps count instructions since. "Longest" on the timeline means
most instructions, which is the closest thing to cost, and it only counts the recorded modules (`sqlmin`, `sqllang`).
Time in SQLOS, waiting or reading from disk adds almost nothing. Never report a Full Trace figure as milliseconds.

**Pages and rows.** `1:1048` is file 1, page 1048, the same notation as the app and as `%%physloc%%`. A row is
`1:1048:3`, slot 3 on that page. A read of 8 consecutive pages is one extent.

**Reads.** A `ReadEventGroup` is one read as the app shows it. `ReadType` `NonCached` is a disk read (a latch suspend,
a file read, then `physical_page_read`), `Cached` is a buffer pool read (a `BUF SH` latch on a page already in
memory). `Pages` lists every page the read covered. With **Clear Buffer Pool** on, disk reads are the point of the
run; with it off a warm-up ran first and the reads are mostly buffer pool reads. The same page can appear in several
reads.

**Events are folded.** `TopLevel` events are the list the app shows. The others are children: the pages, latches
and file reads inside a read group (`Events`), the acquire half of an acquire/release pair (`FoldedFrom`), the
object pool lookup behind a columnstore read (`PoolLookup`). `events --all` includes them. A `latch_acquired` and
`latch_released` with the same page are one latch hold; `Lock` is the acquire and `lock_released` the release.

**The call tree.** One node per distinct call path, not per function: `BPool::Get` reached from a seek and from a
lookup is two nodes. `Calls` is how many times that path was taken (Full Trace only). `AllocatedBytes` and
`Allocations` are the node's own allocations; the app shows the subtree total, which `calltree --hot --by
inclusive-alloc` and `--inclusive` give. `functions` adds every path of a function together. The root has an empty
key. In a Full Trace the Extended Events and tracing machinery was cut out of the tree, so what is left is the
query's own work, and SQLOS (`sqldk`) was not recorded, so only the calls into it appear, not what it did. Nodes in
`calltree-detached.jsonl` are such removed subtrees, kept because the timeline or call log still point at them.

**Operators and the tree.** Each plan node has an `ExecutionOperatorEvent`; node -1 is the statement itself
(`SELECT`). `EntryFrames` are the call tree nodes where that operator's execution begins and `ExitFrames` where a
child operator takes over, so `calltree --operator N` prints exactly the operator's own work. `Instances` are the
iterator object addresses (`this`), which is also RCX in the call log for that operator's methods. An operator with
no entry frames was inlined or could not be matched; say so rather than guessing.

**Locators.** The pages, rows and columnstore objects on a node are the ones passed to or returned by that function
in that path, read from the recording. A page on `BPool::Get` is the page it fetched. `locators --page` turns that
round to list the functions that worked on a page.

**Arguments.** The call log holds RCX, RDX, R8 and R9 at the call and RAX at the return. For a C++ method RCX is
`this`, the object, and the declared parameters follow in RDX, R8, R9. A class returned by value makes RDX the
hidden return buffer and shifts the parameters along. Floating point and most stack arguments were not captured.
Values are raw 64-bit numbers: pointers look like `0x1F508206060`, a page id argument is the pointer to the id, and
the page itself is the locator printed under the call.

**Older exports.** The binary sections changed layout as the app developed, and the script refuses a layout it does
not know with a clear message. The JSON entries, the plan and the call tree always read, so answer from those and say
the Full Trace detail (arguments, timeline, memory) is not readable from this file.

## Left out on purpose

Several things are absent from a Full Trace export by design. None of them is a gap in the capture or a sign that
something went wrong:

- **Extended Events and tracing calls** are cut from the call tree. Publishing an event is code that runs on the
  query's own thread, so the Extended Events machinery would otherwise dominate the tree. The removed subtrees are
  kept in `calltree-detached.jsonl` only because the timeline and call log still point at them. The actual-plan
  profiling wrappers (`CQScanProfileNew`, `CQScanLightProfileNew`) stay, because the operators run inside them.
- **The event session is minimal.** A Full Trace run captures only the batch start and end, the per-operator
  profile, the actual plan and the parallelism waits that identify worker threads. There are no lock, latch, page
  read or I/O events in a Full Trace export; that detail comes from the calls instead.
- **SQLOS (`sqldk`) and Windows are not recorded.** Only `sqlmin` and `sqllang` are. Calls into the scheduler,
  memory objects, synchronisation and the operating system appear as the entry call only, so waiting, memory
  allocation inside SQLOS and disk I/O add almost no instructions. Instruction counts measure recorded engine code.
- **There is no clock.** Positions and instructions are the only measures. Nothing in a Full Trace is a duration.
- **Crop to query** trims the call tree and events to the statement's execution, so the batch handling above it
  and anything before the query started are not there.
- **Result rows, the TTD recording itself and the database** are never exported.

## Answering well

- Lead with the answer, then the evidence. "It read 240 pages from disk in 30 extent reads of
  `dbo.ClusteredTable.pk_ClusteredTable`, then made 531 buffer pool reads over 225 distinct pages" beats a tour of
  the commands.
- Keep what the export records apart from what you infer. A function's name suggests what it does;
  [engine-symbols.md](references/engine-symbols.md) explains the naming and marks what is inferred. Say "this looks
  like" for a function you are reading by name alone.
- Explain the mechanism rather than diagnose the query. Internals Viewer shows what the engine did, not what the
  optimizer should have chosen. A question about estimates against actuals, a missing index or a better plan is the
  optimizer's responsibility and outside what the export records: say so in a sentence, then explain what the chosen
  plan did, because that understanding is where troubleshooting comes from. Estimates, warnings and the grant are
  context for reading the plan, not the subject.
- Do not suggest changes to the query, its indexes or its hints unless the user asks for them. The export shows
  what ran. Advice on what to run instead is a different conversation, and offered unasked it turns an explanation
  into a review.
- Teach progressively. Answer the question at its own level, then offer the next step down ("the probe is 99
  `GetRow` calls on the join; want to see what each one does?"). Mention where the same thing appears in the app
  only as an option, prefixed with "if you have the run open", never as the way to see the evidence.
- Big picture first. Open with the shape of what happened (the plan, the operators, their phases and row counts)
  and go into function-level detail only in the direction the question points. Do not start with instruction
  counts, hot functions or oddities.
- Mechanism, not performance. Do not rank hot spots, hunt anomalies or call a number "suspicious" unless the user
  asks about cost. Much of any run is routine housekeeping: yields and abort checks, scheduler accounting, stack
  and security cookie checks, profiling timers, `memcpy`. These are scenery, listed under "Routine work in every
  run" in [engine-symbols.md](references/engine-symbols.md), and a large share under one of them is normal. Take the
  recorded numbers as recorded rather than auditing how the tool counted them.
- Stay on the question. A trace is full of true, interesting things that have nothing to do with what was asked. A
  side finding earns at most one closing sentence, and only when it bears on the question.
- Know what was left out on purpose, listed in the next section, so an absence is reported as "not captured in a
  Full Trace" rather than as something missing or odd.
- For "what did operator X do" questions use `calltree --operator`, then `calls --function` on the interesting
  function with `--instance` set to the operator's iterator address when it is a method of the iterator.
- Count what the user asked for in the unit they asked for: reads (events) are not pages (an extent read is 8 pages,
  a buffer pool read is one), and a call tree node's calls are for one path, not the function. Two cross-checks:
  an operator's distinct buffer pool pages should match its logical reads in the plan, and with Clear Buffer Pool
  on every buffer pool page should appear in an earlier disk read of the run (the `Warm` column of `reads` is the
  ones that do not).
- The file can hold data from the database: values in log records and in call arguments. Treat it as the user's
  data and do not copy it anywhere.
