# The .ivtrace format

An `.ivtrace` file is a zip archive written by Internals Viewer's **Query > Export to file**. Every entry is listed
below with its layout. The text and JSON entries are the stable part (manifest `FormatVersion` 1). The binary
`fulltrace/` sections each carry their own section version, and an export made by an older build may hold sections
in a layout the current app and `scripts/ivtrace.py` no longer read. The JSON entries of such a file still read.

## Entries

| Entry | Content |
| --- | --- |
| `manifest.json` | Server, database, session, success, row count, query window, options, result columns, plans, call trees, counts |
| `query.sql` | The SQL text that was run |
| `messages.txt` | The Messages pane: run steps with timings, "(n rows affected)", errors, Full Trace load steps |
| `plans/plan-N.xml` | The actual execution plan, as the raw `query_post_execution_showplan` XEvent XML with `<ShowPlanXML>` inside |
| `plans/plan-N.batch.json` | Batch mode and columnstore counters per plan node id (only when the plan has any) |
| `frames.jsonl` | Resolved call stack frames (symbols), one per line |
| `calltree-<name>.jsonl` | One call tree per line-file: `fulltrace` for a Full Trace, `events` for a sampled run, `detached` for removed subtrees |
| `events.jsonl` | Every captured event, one per line, dependencies written before the events that reference them |
| `log-records.jsonl` | Transaction log records captured with the run (empty for a Full Trace run) |
| `fulltrace/*.bin` | Full Trace binary sections, listed under [Binary sections](#binary-sections) |

Result rows are never exported. The TTD recording is not exported either, only what the replay produced.

## manifest.json

```json
{
  "FormatVersion": 1,
  "ExportedAt": "2026-10-14T10:59:39+01:00",
  "ServerName": "...", "DatabaseName": "...",
  "SessionId": "QueryReplay_<guid>",            // the Extended Events session name, unique per run
  "IsSuccess": true, "Message": "",             // Message holds the error for a failed run
  "RowCount": 1996,                             // rows the query returned
  "CropStartUs": 15507000, "CropEndUs": 24342000,  // the query's own window, capture-relative microseconds (null when not cropped)
  "QueryOptions": { "ClearBufferPool", "DisableReadAhead", "IncludeResults", "Trace", "RecordMemoryHistory" },
  "EventOptions": { "IncludeLockModeCategories": [...], "IncludeWait", "IncludeMemory", "IncludeCallStack", "RecordTimeTravel",
                    "RecordTraceEvents", "IncludeLatch", "IncludeBatchMode", "IncludeColumnstore", "IncludeSystemObjects",
                    "CropToQuery", "MaxTraceSizeMb", "TraceDirectory", "AutoDeleteTrace" },
  "ResultSets": [ { "Columns": [ { "Ordinal", "Name", "ClrType", "IsNullable", "TypeName", "Detail", "Width", "Alignment" } ] } ],
  "Plans": [ { "Index", "PlanHandleId", "IsInternalPlan", "File", "BatchFile" } ],
  "CallTrees": [ { "Name", "File", "RootId", "IsResultTree", "IsDetached", "ActivityFromTrace",
                   "ActivityMinUs", "ActivityMaxUs", "ActivityBuckets", "ActivityBusiest" } ],
  "ResultLogRecords": [ log record ids ],
  "EventCount": 5117, "LogRecordCount": 0, "NodeCount": 60669
}
```

Option meanings that change how to read the rest:

- `ClearBufferPool` true: the buffer pool was emptied before the run, so disk reads are expected and are the point.
  False: a warm-up ran first, so the run's reads are mostly buffer pool reads.
- `DisableReadAhead` true: read-ahead was turned off, so reads show the access pattern itself.
- `RecordTimeTravel` true: a Full Trace run. The event list is the minimal set (batch, operator profiles, plan, a
  few parallelism waits), and the call tree comes from the recording rather than from event stacks.
- `CropToQuery` true: events and the call tree are trimmed to the statement's execution. `CropStartUs` and
  `CropEndUs` give that window.
- `IncludeLockModeCategories` empty: no lock events were captured.

The activity window on a call tree (`ActivityMinUs`, `ActivityMaxUs`, `ActivityBuckets`) is what each node's
`ActivityCounts` and `CallActivity` arrays index: bucket `i` covers `[Min + i * (Max - Min) / Buckets, ...)`.

## frames.jsonl

One resolved frame per line. A frame is a code address, and the call tree refers to frames by `Id`.

| Field | Meaning |
| --- | --- |
| `Id` | Frame id, referenced by `Frame` on call tree nodes |
| `Module` | Module the address is in: `sqlmin`, `sqllang`, `sqldk`, `SqlTsEs`, `KERNEL32`, etc. |
| `Address` | Absolute address in the recorded process. Call log entries are keyed by this address |
| `Pdb`, `Guid`, `Age` | The symbol file that resolved it |
| `Rva` | Offset of the address from the module base |
| `Instance` | For a Full Trace, the `this` object the call ran on when the frame is kept per object (0 otherwise) |
| `Signature` | The function's parameter list from the symbols, when known |
| `Symbol` | Raw symbol, `Class::Method+0x<offset into the function>` |
| `SymbolModule` | Present when the symbol resolved from a different module than `Module` (identical code folding) |

## calltree-*.jsonl

One node per line in pre-order (parents before children). A node is one distinct call path, so the same function
reached by two different paths is two nodes.

| Field | Meaning |
| --- | --- |
| `Id` | Node id. Events, operator entry frames, the timeline node map and `--root` options all use it |
| `Parent` | Parent node id. Absent on the root |
| `Detached` | Present (true) on a subtree that was removed from the tree, such as Extended Events publishing cut out of a Full Trace, but kept because the timeline or call log still point at it. Its `Parent` is set but the parent does not list it as a child |
| `Key` | The child key under its parent: `module!Class::Method` once resolved, or an RVA before. Empty on the root |
| `Function` | `module!Class::Method` when the frame resolved to a symbol |
| `Order` | Creation order. Sorting siblings by `Order` gives the order the calls were first seen |
| `Frame` | Frame id in `frames.jsonl` |
| `Rvas` | Call-site offsets merged into this node |
| `Calls` | Full Trace: how many times this exact path was called. Absent when zero |
| `CallActivity` | Full Trace: calls per activity bucket |
| `ActivityCounts` | Events in this node's subtree per activity bucket |
| `AllocatedBytes`, `Allocations` | Full Trace: memory allocated by this node's own calls (not its children). The app shows the total including children |
| `Locators` | Full Trace: `Pages` ["file:page"], `Rows` ["file:page:slot"], `Objects` [[value, detail]] that this function was passed or returned. A page here means the function worked on that page |
| `Events` | Ids of events whose innermost frame is this node (sampled runs) |

A columnstore object locator is `[value, detail]` with `value = hobt_id | object_type` (the low 16 bits of a hobt id
are always zero) and `detail = object_id << 32 | column_id`. Object types: 1 ColumnSegment, 2 PrimaryDictionary,
4 SecondaryDictionary, 5 BulkInsertDictionary, 6 DeleteBitmap, 7 ExtendedStringPageRange, 8 GlobalStringDictionary,
9 DeleteBitmapCollection, 10 ExternalFileMetadata. `object_id` is the rowgroup for a segment or delete bitmap and the
dictionary id for a secondary dictionary.

## events.jsonl

One event per line. `$type` names the event class, `EventId` is the id other events and nodes refer to, and
`TopLevel: true` marks the events the app lists (the rest are children folded into a group or into another event).
Fields common to every event:

| Field | Meaning |
| --- | --- |
| `Name` | The Extended Event name (`latch_acquired`, `lock_released`, `physical_page_read`), or a display name for a built event (`Page Read`, `Lock`, `Wait`, `Object Locks`) |
| `Description` | The one-line description the app shows |
| `TimeUs`, `DurationUs` | Capture-relative microseconds. Subtract `CropStartUs` for time since the query started |
| `Timestamp` | Wall clock of the event |
| `SequenceId` | Event order from the trace file |
| `ThreadId` | Query thread. 0 is the coordinator or the only thread, 1..n are parallel workers |
| `SystemThreadId`, `TaskAddress`, `WorkerAddress` | The SQLOS task, worker and OS thread that fired it |
| `DatabaseId`, `AllocationUnit` `{Id, Name}`, `ObjectName` | The object the event is about, when known |
| `PageAddress` | `"file:page"` for page events |
| `Category` | `Io`, `Cpu`, `Concurrency` or `Parallelism`, a coarse bucket for waits and locks |
| `PlanNodeIdentifier` `{PlanHandleId, NodeId}` | The plan operator the event was attributed to |
| `CallStack` | Call tree node id of the event's innermost frame (sampled runs) |
| `FoldedFrom` | Event id this one was folded from (for example the start event of an acquire/release pair) |

References between events are ids: `Events` (children of a group), `FoldedFrom`, `PoolLookup`, `LogRecord(s)` (log
record ids), `CallStack`, `EntryFrames` and `ExitFrames` (call tree node ids). The per-type fields are in
[events.md](events.md).

## log-records.jsonl

One transaction log record per line, `$type` naming the record class (`InsertRowsLogRecord`, `ModifyRowLogRecord`,
`FormatPageLogRecord`, `SetBitsLogRecord`, etc.) with `LogRecordId`, `Lsn` ("vlf:offset:sequence"), `PreviousLsn`,
`LogTransactionId`, `Operation` (`LOP_*`), `Context` (`LCX_*`), `TransactionId`, `LogRecordSize`, and for page
records `PageAddress`, `SlotId` and the row or column bytes the record carries. Only sampled runs capture them.

## plans/plan-N.xml and plan-N.batch.json

The XML is the raw `query_post_execution_showplan` event: `<data>` elements for `cpu_time` and `duration`
(microseconds), `dop`, `estimated_rows`, memory grant figures, then `showplan_xml` holding a standard SQL Server
`<ShowPlanXML>` document with the actual plan: `RelOp` nodes with `NodeId`, `PhysicalOp`, `LogicalOp`, estimates,
`RunTimeInformation/RunTimeCountersPerThread` (actual rows, executions, reads, per thread), predicates, seek
predicates and warnings. `plan-N.batch.json` maps a plan `NodeId` to its batch mode counters: `BatchCount`,
`SegmentReads`, `SegmentSkips`, `LocallyAggregatedRows`, pushdown counts, and `SegmentScans` with one entry per
segment read (rowgroup, column, encoding, filter, bit packing, dictionary sizes, pure and impure row buckets).

## Binary sections

Every binary entry starts with a 4-byte magic `IVFT` (`0x54465649` little-endian) and an int32 export format version
(1). Most sections then write their own version as a negative int32. Integers are little-endian. An array is an
int32 count followed by that many raw elements. A .NET string is a 7-bit varint byte length followed by UTF-8.
A `TracePosition` is a u64 packed as `sequence << 32 | steps`, all ones when unknown. A `TracePoint` is three u64:
sequence, steps, instructions.

Trace positions are TTD's clock: the sequence number is shared by every thread and advances where threads
synchronise, and steps count instructions on one thread since that point. There is no wall-clock time in a Full
Trace. The app prints a position as hex `Sequence:Steps`, and so does the script.

### fulltrace/calllog.bin (section version 5)

The call log: every call the replay logged, per `(function address, instance address)`.

```
int32 -5
int32 functionCount
  per function: u64 functionAddress, u64 instanceAddress, int32 chunkCount
    per chunk: int32 calls, byte[] zlibColumns   (array: int32 length + bytes)
int32 uniqueLocatorCount
  per locator: int32 register, u8 valueType, u64 value, u64 detail, [string text when valueType is Text, WideText or Bindings]
int32 locatorTableCount
  per table: u64 functionAddress, u64 instanceAddress,
             BitPacked callIndexes, int32[] palette, BitPacked paletteIndexes
```

A chunk's columns inflate (zlib) to `calls * 11` u64 values stored column-major, `values[column * calls + call]`,
zero-filled when the inflated data is short. Columns: 0 from sequence (delta-coded against the previous call),
1 from steps, 2 flags (low 32 bits = thread id, bit 32 = the call returned), 3-6 RCX, RDX, R8, R9 at the call,
7 return value (RAX), 8 raw replay node index, 9 to sequence (stored as an offset from the from sequence unless all
ones), 10 to steps. Call indexes run across the function's chunks in order.

A `BitPacked` array is int32 count, int32 width, then u64[] words with values packed `width` bits each from bit 0 of
word 0 upwards. A locator table lists, in call index order, the locators of a call: entry `i` belongs to call
`callIndexes[i]` and is `uniqueLocators[palette[paletteIndexes[i]]]`. Locator value types: 0 Page, 1 Row,
2 ColumnStoreObject, 3 Referenced (the scalar behind a pointer argument), 4 Argument (a stack argument), 5
LogSequenceNumber, 6 Text, 7 WideText, 8 TypeInfo, 9 Variant, 10 Bindings. `register` is the argument slot: 0-3 are
RCX, RDX, R8, R9, 4-7 the fifth to eighth (stack) arguments, 16 the return register. A page value packs
`slot << 48 | file << 32 | page`.

### fulltrace/timeline.bin (section version 3), followed by the memory index

```
int32 -3
f64 stepsPerSequence
TracePoint start, TracePoint end
int32[] parents          # raw replay node -> parent raw node
int32[] nodeIds          # raw replay node -> calltree-fulltrace node id, -1 when none
int32 threadCount
  per thread: u32 threadId, i64 calls,
              u32[] clockSequences, u64[] clockStarts,      # instructions at the start of each sequence seen on the thread
              int32 rowCount
              per row (depth): u64[] froms, u64[] tos, int32[] callNodeIndexes, u32[] callIndexes, u8[] flags
# memory index
int32 threadCount
  per thread: u32 threadId, u64[] allocationStarts, u64[] allocationBytes, u64[] freedAt, u64[] freeStarts, u64[] freeTotals
u64[] inUsePositions, u64[] inUseValues           # bytes in use over the run
u64[] workspacePositions, u64[] workspaceValues   # memory grant (workspace) bytes over the run
```

A row is one depth in a thread's lane, its spans sorted by start. `callNodeIndexes` are raw replay nodes (map through
`nodeIds` to the call tree), `callIndexes` is the call's index in its function's call log list (all ones when it
has none), and flags bit 0 means the call returned, bit 1 that its start is unknown (it began before the recording).
The instructions a span covers on its thread is `clock(to) - clock(from)`, where `clock(position)` is
`clockStarts[i] + steps` when `clockSequences[i]` equals the position's sequence and otherwise the start of the
previous sequence seen.

### fulltrace/memorymap.bin (section version 2, version 1 has no site)

```
int32 -2
int32 nameCount, string[] names
int32 entryCount
  per entry: u64 address, int32 callIndex, u64 owner, u64 fromSequence, u64 fromSteps, u64 toSequence, u64 toSteps,
             int32 callNodeIndex, u32 thread, u8 operation, int32 classNameIndex, u64 bytes, int32 siteIndex
```

Operation: 0 Allocated, 1 Constructed (a constructor ran on it), 2 Visited, 3 Declared (its type came from a declared
parameter type). Name indexes are -1 when unknown. This is what names a block of memory with a C++ class.

### fulltrace/objectpool.bin (no section version)

`int32 count`, then per entry `u64 entryAddress, u64 dataAddress, u64 memoryObjectAddress, i64 pagesKb,
int32 objectType, int32 databaseId, u64 hobtId, int32 columnId, int32 objectId, i64 objectSize`: the columnstore
object pool at the end of the run.

### fulltrace/output.bin (section version 3)

`int32 -3, int32 count`, then per output row structure `u64 outputRow, u64 directSelectOwner, int32 ownerCount,
u64[] owners`: the row buffer the results were produced into and the iterator object that owns each column.

### fulltrace/plannodes.bin (section version 3)

`int32 -3, int32 count`, then per node `u64 iterator, u64 planObject, u64 batchPlanObject, u64 query, int32 nodeId,
bool hasConstructed, [TracePoint constructed], bool isBatchHelper`: the iterator object (`this` address) that ran
each plan node, which is how operators are matched to calls.

### fulltrace/teardowns.bin (section version 1)

`int32 -1, int32 count`, then per thread `u32 thread, TracePoint end`: where the query's execution ended on each
thread.

### fulltrace/memoryhistory.bin (section version 8)

```
int32 -8
int32 blockCount
  per block: u64 iterator, u64 address, int32 size, bool isIterator, bool isInline, u32 thread, int32 callNodeIndex,
             TracePosition from (2 x u64), TracePosition to (2 x u64), u64 fromInstructions, u64 toInstructions,
             int32 firstWrite, int32 writeCount
int32 writeCount
  per write: TracePosition at (2 x u64), u64 instructions, int32 callNodeIndex, int32 callIndex, int32 offset,
             u8 changeType (0 Initialised, 1 Changed), byte[] bytes (int32 length + bytes)
int32 iteratorCount
  per iterator: int32 callNodeIndex, u64 iterator
```

The memory the query wrote to, block by block, with every write: the second replay pass with watchpoints.
