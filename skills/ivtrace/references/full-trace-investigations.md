# Answering execution questions from a Full Trace

A Full Trace is an instruction-level recording of the storage engine and query processor, replayed into every call
the query made with its count, arguments and return value, a per-thread timeline, every allocation with its C++
class and allocation site, and every write to the query's memory with the function that wrote it. Someone who
recorded one is not after a summary of the plan. They want to know how the engine executed the query, at the level
of its functions, objects and memory: "explain what the Sort operator is doing", "what is this batch using those
vectors for", "why is the hash join calling ReadRow twice per row", "what does this field of the iterator hold".

Answers to these are longer, come from several commands run in sequence, and are organised by phase. Everything
in them should be checkable: cite node ids, iterator addresses, block numbers, positions and counts, so a reader
who does have the run open in Internals Viewer can find the same thing. The answer itself never depends on that:
assume the export is all the user has in front of them, and mention the app's views only as an aside that says
"if you have the run open". Keep what the export records apart from what the names suggest, and say which is
which.

The depth is set by the question, not by the file. Internals Viewer is a learning tool and the trace is the exact
record of what happened, so a beginner's "what is the hash join doing" and an expert's "explain what is happening
with the scratch buffers in the RLE decompression" are answered from the same export. The first wants the shape:
build, then probe. The second wants the columnstore decode pipeline: the `XMRLE*`, `XMHybridRLE*` and `RbpDecode`
calls under the scan, the buffers they allocated by site in the memory map, and the writes to those buffers with
their writers in the memory history. Neither is a tuning exercise. What the optimizer should have chosen is outside
the tool; what the engine did with the plan it was given is all of it.

## What the export holds for this

| To find out | Look at | Command |
| --- | --- | --- |
| Which plan node, iterator object and call tree frames an operator is | `ExecutionOperatorEvent` (`Instances`, `EntryFrames`, `ExitFrames`), `plannodes.bin` | `operators --frames`, `plannodes` |
| What the operator's code did, phase by phase, with counts | the call tree under its entry frames | `calltree --operator N`, then `--root <id> --depth D` to open a subtree |
| Every method of its class, how often, with what arguments | the call log | `calls --grep "CQScanHash"`, `calls --function "CQScanHash::GetRow" --instance <this>` |
| Which call handled a given pointer, key or page | the call log registers and locators | `calls --function X --value 0x295C5810060`, `locators --page 1:384` |
| What it allocated and what those blocks are | the memory map (class, allocation site, bytes) | `memorymap --grep CQScanHash`, `memorymap --by site`, `memorymap --address <hex>` |
| How its state changed, field by field, and who changed it | memory history (writes with writers) | `memoryhistory --kind iterator`, `--block N`, `--block N --field 0x3B8` |
| Which blocks belong to an iterator, or were made by one function | memory history filters | `memoryhistory --iterator <this>`, `--allocated-in "CBpBatch::"`, `--address <hex>` |
| How much work a call or an operator was | the timeline in instructions | `timeline --node <entry node id>`, `timeline --top 20` |
| Which iterator produced each output column | `output.bin` owners, matched to `plannodes` | `output`, `plannodes` |
| The memory grant against what was used | the plan's `MemoryGrantInfo`, the workspace curve | `plan`, `memory` |

Positions are hex `Sequence:Steps` everywhere, and instructions are the only measure of cost. Nothing here is time.

## The method

Discovery runs in one order: the query, then its operators, then their calls, then the memory history where the
export has one. Each level is the big picture for the next, and the question decides how far down to go.

1. **Start with the query.** `summary` for what ran, where, with which options and whether it succeeded, then
   `plan` for the shape of the plan: the operators, how they nest, the rows each produced. Say this shape first.
2. **Locate the operator.** `operators --frames` gives the plan node, the iterator address (`this`), the entry frames
   where its execution starts and the exit frames where its children take over. `plannodes` lists the same
   iterator objects with the position each was constructed.
3. **Walk its calls by phase, shape first.** `calltree --operator N --depth 3` prints the operator's own work. Expect
   three kinds of entry frame: `Open` (setting up rowsets, partitions, memory), the first `GetRow` (which does the
   one-off work: the b-tree descent, the hash build, consuming the whole input for a blocking operator) and the
   repeated `GetRow` (one call per row returned). Call counts give the shape: a node called once per row, once per
   page, once per batch. State that shape before anything else, and only then open the subtree the question points
   at, with `--root`, `--find X --operator N` and `--path`.
4. **Read its methods and arguments.** `calls --grep "<Class>"` is the list of the class's logged methods with
   counts. `calls --function "<Class>::<Method>" --instance <this>` dumps the calls on that object: RCX is `this`,
   RDX, R8 and R9 the next arguments, RAX the return, with page, row and object locators decoded under each call.
5. **Map its memory.** `memorymap --grep <Class>` finds the object itself and anything else labelled with that
   class. `memorymap --by site` groups blocks by the source file and line that allocated them, which is how raw
   buffers with no class are told apart (`bpbatch.cpp:265` is a vector buffer, `qshashwf.cpp:2437` a hash workfile
   buffer). `memoryhistory --kind iterator` lists the operator objects that were written to, and `--iterator <this>`
   every block belonging to one.
6. **Follow its state, when the export has a memory history.** `memoryhistory --block N` summarises the writers of a block and its most written fields,
   then lists each write with position, offset, bytes, decoded value and writer. `--field 0x3B8` follows one field
   through the run, which is how a counter, a pointer or a flag is understood: who sets it, when, to what.
7. **Weigh it, roughly.** `timeline --node <entry node id>` totals the instructions under that call tree node, and
   `timeline --top 20 --node <id>` ranks its calls. Use this to say which phase is the big one, not to profile:
   the recorded query ran many times slower than normal, the profiling wrappers add work of their own, and a large
   count under routine housekeeping is not a finding (see the next section).
8. **Tie it to the plan and the output.** `plan --detail` for predicates, seek keys, hash keys and the grant, with
   the estimates as context for reading the plan rather than a subject; `output` and `plannodes` for which iterator
   produced which output column.

## Big picture before detail

Start every answer with the shape of what happened: the plan, the operators in the order they ran, each one's
phases with their call counts, and how the rows moved. Only then go down, and only in the direction the question
points. Opening with instruction counts, hot functions or oddities is the wrong order: the user asked how
something works, and detail is the second step of teaching it, not the first.

A Full Trace is about mechanism, not performance. Do not rank hot spots, hunt for anomalies or call a number
"suspicious" unless the user asks about cost. Instruction shares are there to orient (the build is bigger than the
probe), and a routine function with a large share is normal, because the engine spends much of its time on
housekeeping. The housekeeping present in every run is listed under "Routine work in every run" in
[engine-symbols.md](engine-symbols.md); treat it as scenery. The yield is the usual trap: in a 1,999-row hash
build `YieldAndCheckForAbort` was called 250 times, once every few rows, and 77 of those calls switched the task
out through `SOS_Task::PreWait`, `SOS_Scheduler::Suspend` and `SwitchContext`, under which the scheduler checked
for aborted tasks, polled I/O completions and chose the next worker. Those calls carried two thirds of the build's
instructions. That is the server cooperatively yielding and checking for a cancel, as it does in every query. Say
"the build yielded 77 times" if it bears on the question, and move on. Do not set off to audit how the timeline
counts instructions across a yield: take the recorded numbers as recorded, and interpret them with the big picture.

Stay on the question. A trace is full of true, interesting things that have nothing to do with what was asked,
and listing them turns an explanation into a tour. A side finding earns at most one closing sentence, and only
when it bears on the question.

## Worked example: a row mode Hash Match

From a real run of `SELECT c.* FROM dbo.ClusteredTable AS c INNER HASH JOIN dbo.HeapTable AS h ON c.Id = h.Id AND
FORMAT(h.Id % 2, '0') = '0' WHERE c.Id < 2000`: node 0 Hash Match (Inner Join), build input a Clustered Index Seek
(node 1, 1,999 rows), probe input a Filter over an Index Seek (nodes 3 and 4, 99 rows), 99 rows out.

**What it is.** `operators --frames`: iterator instance `0x295C5810060`, entry frames `CQScanHash::Open` and two
`CQScanNew::GetRowOrReQualifyHelper` nodes. `memorymap --grep CQScanHash`: a 1,040-byte `CQScanHash` object
allocated at `qshash.cpp:70` and constructed by `CQScanHash::CQScanHash`.

**Open.** `calltree --operator 0 --depth 3`: `CQScanHash::Open` calls `CHashWorkfilePartitionGroup::Open` four
times and `CHashWorktablePartitionGroup::Open` twice (the partition groups a spill would use, prepared up front),
then `CQScanHash::Iterate`, which runs `PickInputAndPrepareOutput`, `ConsumeBuild` once and `ConsumeProbe` once.
So the build side is consumed inside `Open`, before the first row is asked for, which is why the operator's first
row (`EmitStartUs`) is later than its start.

**Rows.** `GetRowOrReQualifyHelper` is called 99 times, each calling `CQScanHash::GetRow` and `ConsumeProbe`, one
probe step per output row. `AppendSpilled` runs once at the end (nothing spilled: no workfile I/O appears under it)
and a final `PickInputAndPrepareOutput` closes the loop. `calls --grep "CQScanHash"`: `ReadRow` 2,100 calls, one per
build row and per probe row plus two end-of-input calls, `DbgTrackMemoryUsage` 2,000, `GetRow` 100 on instance
`0x295C5810060`, `ConsumeProbe` 101, `SetMaxBufs` 14, `UpdateGrantedMemory` 1.

**Memory.** `memorymap --by site --grep qshash`: 64 blocks of 1 KB at `qshashwf.cpp:2437` (hash workfile buffers,
reading the file name), two 88-byte `CValHashCachedSwitch` objects at `qshash.cpp:430` (the hashing of the build
and probe keys, reading the name), and `CValExec` expression executors at `qshash.cpp:407` whose type was declared
when `CValRefJoin::PvalInvokeSpecific` ran them (the join's key and residual expressions). `memory`: workspace peak
704.0 KB, which is the plan's `MaxUsedMemory` of 704 KB.

**State.** `memoryhistory --kind iterator` lists the object as block 61 with 13,161 writes. `memoryhistory --block
61`: the writers are `CQScanHash::ReadRow` (8,396), `CValConstXVar::GetDataX` (2,101), `ConsumeBuild` (2,011),
`ConsumeProbe` (414), and the hottest fields are `+0x138` (4,000 single-byte writes, two per build row), `+0x2F0`
(2,100, one per `ReadRow`), `+0x118` and `+0x90` (about one per build row). `--field 0x3B8` shows the constructor
initialising it to 0, then `ConsumeBuild` writing 0xFC, 0x1F5, 0x2EF and on: a value growing by about 250 per
write during the build, so a running count updated in steps rather than per row. That reading is inferred from the
pattern; the export records the writes, not the field's name.

## Sort

Row mode sorts are `CQScanSort*` and `CQScanTopSort*` (Top N Sort), batch mode `CBpQScanSort*`. A sort is
blocking: its first `GetRow` consumes the whole input before returning anything, so in `operators` its first row
comes well after its start, and in the call tree the input operator's `GetRow` calls all sit under the sort's first
`GetRow` node. Report, from the counts, how many input rows it consumed, how many it returned (a Top N Sort returns
N), and what it allocated: `memorymap --grep Sort` and `memorymap --by site` for the sort's working memory, against
the grant in `plan` and the workspace peak in `memory`. A sort that ran out of grant spills to a workfile: look for
workfile classes and `workfile.cpp` allocation sites under its segment, and say that a Full Trace carries no I/O
events, so a spill shows as code paths and allocations rather than as file writes. Follow the sort object's fields
with `memoryhistory` to show the run count, row count or position moving as it consumes and then emits.

## Batch mode and batch memory

Batch operators are `CBpQScan*` (`CBpQScanColumnStoreScan`, `CBpQScanFilter`, `CBpQScanHashJoin`,
`CBpQScanHashAgg`, `CBpQScanSort`, `CBpQScanProject`), pulled with `BpGetNextBatch`, and `CQScanBatchHelper` is
where batches become rows for a row mode parent. A batch is a `CBpBatch` object with parts allocated around it,
and every part is in the memory history, named by the function and site that allocated it. Observed on columnstore
runs (the sizes are for 900-row batches):

| Part | Allocated by | Size |
| --- | --- | --- |
| Batch header, class `CBpBatch` | the producer's `BpGetNextBatch` (for example `CBpQScanColumnStoreScan::BpGetNextBatch`) | 96 |
| Vector array, one 0x58 entry per vector | `CBpBatch::Initialize` (`bpbatch.cpp:123`), grown by `CBpBatch::Resize` (`bpbatch.cpp:215`) | 176, 352, 704, 1408 for 2, 4, 8, 16 vectors |
| Selection vector, u16 row indexes | `CBpBatch::Initialize` (`bpbatch.cpp:129`) | 1,800 |
| Vector buffers, one per vector, 8 bytes per row | `CBpBatch::AllocateVectorBuffer` (`bpbatch.cpp:265`) | 7,848 |
| Allocation bitmap and pure bitmap | `CBpBatch::Initialize` or `Resize` (`bpbatch.inl:460`) | 8 |
| Deep data pages for values wider than a slot | `RbpNormalize::NormalizeVector<...>` or `CBpDeepDataStore<...>::PvAllocateDeepDataSlow` | 8,084 |

The header fields, read from the writers of each offset: `+0x00` allocation bitmap pointer, `+0x08` vector array
pointer, `+0x10` pure bitmap pointer, `+0x18` vector array capacity, `+0x1C` vector count, `+0x20` a version
counter bumped by `AllocateVector`, `AllocateVectorBuffer` and `CBatchManager::ReleaseBatch`, `+0x24` row capacity
(900), `+0x28` active rows, `+0x30` the batch's memory object, `+0x38` selection vector pointer, `+0x40` the deep
data allocator. In a vector array entry `+0x10` is the current deep data page and `+0x18` the first; the entry also
carries the buffer pointer and the pure flag. A pure vector holds one value for the whole batch in slot 0 and its
other slots are never written. A deep slot is an odd pointer into a page whose entries are `[u16][u16 length][bytes]`.

One `CBpBatch` per producer is reused for every batch it produces, so its state is a function of position: read
the header and the parts at the position of interest with `memoryhistory --block N`, and remember its last writes
are the teardown zeroing everything.

To answer "what is the batch using these vectors for":

1. Find the batch: `memoryhistory --allocated-in "BpGetNextBatch"` for the header, then `--allocated-in "CBpBatch::"`
   for its parts, or `--address <pointer>` for a part you were given. `memorymap --by site --grep bpbatch` groups the
   same parts by site.
2. Read the header at the position: vector count and capacity (`+0x1C`, `+0x18`), active rows (`+0x28`), and the
   array pointer, then the vector array to list the buffers.
3. Decide each vector's role from its writers. `memoryhistory --address <buffer>` then `--block N` names who wrote
   it: a scan's `Rbp*` decode means column data from the segment; `RbpExpr*`, `CBpQScanProject*` or an expression
   evaluator means a computed value; `CBpag*` an aggregate's working values; a filter means the selection vector
   or a bitmap being narrowed. Match the vectors that reach the output through `output` and `plannodes`; the
   rest are working vectors the operators use between themselves. "Scratch" is a role you establish from the
   writers, not a label in the export.
4. Show the narrowing: compare active rows and the selection vector's writes before and after `CBpQScanFilter`, and
   the `SegmentScanEvent` input and output rows per column with the pure and impure bucket counts (`columnstore`).

## Expression evaluation

`CEsExec::GeneralEval*`, `CEsRuntime::Eval`, `CValConstXVar::GetDataX` and `sqlTsEs!GetData` are the expression
service evaluating predicates, seek keys and computed columns, and their counts say how many times an expression
ran. `plan --detail` gives the expression text per operator. A `clr!` module in the call list means a CLR
function (`FORMAT`, `TRY_PARSE`, user CLR) was invoked from the expression. "Does it filter there?" is answered
from the export by the evaluation calls under the operator: `calltree --find "CEsExec::(FastCompare|GeneralEval)"
--operator N` shows which of its paths evaluate a predicate and how often, and `calls --function` on that function
lists each call with its position, arguments and return register. The outcome of a comparison (true, false or
NULL) is written into a value the caller passes in, which the call log holds only where that value was captured as
a referenced locator, so check what the calls show before stating outcomes. If the user has the run open in
Internals Viewer, the Trace Timeline's Evaluations track on the operator's row ticks each of these calls coloured by
its outcome (toggled under View > Operators or in the Operators pane's right-click menu); say so as an option, not
as the evidence.

## Shaping the answer

- **What it is.** The plan node, the iterator class and address, the table or index, the rows in and out.
- **Phases.** Open, first row, each row (or each batch), close, with the calls and counts that define each.
- **Memory.** What it allocated, by class and site, against the grant.
- **State.** The fields that changed, who changed them and what the pattern means, inferences marked.
- **Cost.** Instructions under its entry frames, and the heaviest calls.
- **Limits.** What the export cannot show: fields whose names are not recorded, SQLOS and Windows internals that
  were not recorded, values in floating point registers, time.
