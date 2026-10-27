---
name: ivtrace
description: Read an Internals Viewer query run export (an .ivtrace file) and answer questions about the SQL Server query it captured - the SQL and actual plan, which pages were read from disk or the buffer pool, waits, locks and latches, columnstore segment scans and eliminations, the call tree of SQL Server engine functions behind each plan operator, and for a Full Trace run every logged call with its arguments, the trace timeline and memory. Use this whenever a user mentions an .ivtrace file, an Internals Viewer export, run, Full Trace or call tree, or asks what SQL Server did while running a query they captured, even if they only say "look at this trace" or "why did this query read so many pages".
---

# Reading an Internals Viewer export

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
2. Pick the commands that answer the question (table below). Combine them: a "why is this slow" question needs the
   plan, the reads, the waits, the operators and the hot paths of the call tree together.
3. Read the references when you need them: [references/events.md](references/events.md) for what each event type
   and field means, [references/format.md](references/format.md) for the exact layout of every entry,
   [references/engine-symbols.md](references/engine-symbols.md) to read SQL Server function names, and
   [references/full-trace-investigations.md](references/full-trace-investigations.md) for how to explain what an
   operator, a batch or a block of memory is doing from a Full Trace.
4. Answer with the evidence: event ids, node ids, page addresses and counts straight from the commands, so the
   user can find the same thing in the app.

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

## Full Trace questions go deep

A Full Trace is recorded for one reason: to see how the engine executed the query, at the level of its functions,
objects and memory. Expect questions like "explain what the Sort operator is doing", "what is the batch using
those vectors for" or "what does this field of the hash join hold", and expect to answer them from several
commands run in turn: the operator's entry frames, its call tree by phase with counts, its methods and arguments
from the call log, its allocations by class and site, the writes to its object with their writers, and its cost in
instructions. Organise the answer by phase, cite node ids, addresses, block numbers and positions, and mark what
is read from a name rather than recorded. [full-trace-investigations.md](references/full-trace-investigations.md)
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

## Answering well

- Lead with the answer, then the evidence. "It read 240 pages from disk in 30 extent reads of
  `dbo.ClusteredTable.pk_ClusteredTable`, then made 531 buffer pool reads over 225 distinct pages" beats a tour of
  the commands.
- Keep what the export records apart from what you infer. A function's name suggests what it does;
  [engine-symbols.md](references/engine-symbols.md) explains the naming and marks what is inferred. Say "this looks
  like" for a function you are reading by name alone.
- For "why" questions, line up the plan (estimated against actual rows, warnings, the memory grant), the reads (disk
  against buffer, read-ahead, extents), the waits, the operators (start, first row, duration, threads), then the hot
  call paths and the biggest allocators. Each is one command.
- For "what did operator X do" questions use `calltree --operator`, then `calls --function` on the interesting
  function with `--instance` set to the operator's iterator address when it is a method of the iterator.
- Count what the user asked for in the unit they asked for: reads (events) are not pages (an extent read is 8 pages,
  a buffer pool read is one), and a call tree node's calls are for one path, not the function. Two cross-checks:
  an operator's distinct buffer pool pages should match its logical reads in the plan, and with Clear Buffer Pool
  on every buffer pool page should appear in an earlier disk read of the run (the `Warm` column of `reads` is the
  ones that do not).
- The file can hold data from the database: values in log records and in call arguments. Treat it as the user's
  data and do not copy it anywhere.
