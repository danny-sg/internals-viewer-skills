"""Read an Internals Viewer query run export (.ivtrace) with nothing but the Python standard library.

Usage: python ivtrace.py <file.ivtrace | extracted folder> <command> [options]

Run with --help, or "<command> --help", for the options of each command.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import io
import json
import os
import re
import struct
import sys
import zipfile
import zlib
import xml.etree.ElementTree as ElementTree

MAGIC = 0x54465649

SHOWPLAN = "{http://schemas.microsoft.com/sqlserver/2004/07/showplan}"

REGISTERS = {0: "RCX", 1: "RDX", 2: "R8", 3: "R9", 4: "Arg5", 5: "Arg6", 6: "Arg7", 7: "Arg8", 16: "RAX"}

LOCATOR_TYPES = ["Page", "Row", "ColumnStoreObject", "Referenced", "Argument", "LogSequenceNumber", "Text", "WideText",
                 "TypeInfo", "Variant", "Bindings"]

COLUMNSTORE_OBJECT_TYPES = {0: "Undefined", 1: "ColumnSegment", 2: "PrimaryDictionary", 4: "SecondaryDictionary",
                            5: "BulkInsertDictionary", 6: "DeleteBitmap", 7: "ExtendedStringPageRange",
                            8: "GlobalStringDictionary", 9: "DeleteBitmapCollection", 10: "ExternalFileMetadata"}

MEMORY_MAP_OPERATIONS = ["Allocated", "Constructed", "Visited", "Declared"]

MEMORY_HISTORY_CHANGES = ["Initialised", "Changed"]

UNKNOWN = 0xFFFFFFFFFFFFFFFF


# ----------------------------------------------------------------------------------------------------------------------
# Container
# ----------------------------------------------------------------------------------------------------------------------


class Export:
    """The export, read from the zip or from a folder it was extracted into"""

    def __init__(self, path):
        self.path = path
        self.folder = None
        self.zip = None
        if os.path.isdir(path):
            self.folder = path
        else:
            self.zip = zipfile.ZipFile(path)
        self.manifest = json.loads(self.read_text("manifest.json"))
        self._frames = None
        self._nodes = None
        self._children = None
        self._events = None
        self._timeline = None
        self._symbols = None

    # -- entries --------------------------------------------------------------------------------------------------------

    def names(self):
        if self.zip:
            return [i.filename for i in self.zip.infolist()]
        names = []
        for root, _, files in os.walk(self.folder):
            for file in files:
                names.append(os.path.relpath(os.path.join(root, file), self.folder).replace("\\", "/"))
        return names

    def size(self, name):
        if self.zip:
            return self.zip.getinfo(name).file_size
        return os.path.getsize(os.path.join(self.folder, name))

    def has(self, name):
        try:
            if self.zip:
                self.zip.getinfo(name)
                return True
            return os.path.exists(os.path.join(self.folder, name))
        except KeyError:
            return False

    def read_bytes(self, name):
        if self.zip:
            return self.zip.read(name)
        with open(os.path.join(self.folder, name), "rb") as file:
            return file.read()

    def read_text(self, name):
        if not self.has(name):
            return ""
        return self.read_bytes(name).decode("utf-8-sig")

    def iter_jsonl(self, name):
        if not self.has(name):
            return
        if self.zip:
            stream = io.TextIOWrapper(self.zip.open(name), encoding="utf-8")
        else:
            stream = open(os.path.join(self.folder, name), encoding="utf-8")
        with stream:
            for line in stream:
                line = line.strip()
                if line:
                    yield json.loads(line)

    # -- call trees -----------------------------------------------------------------------------------------------------

    @property
    def frames(self):
        if self._frames is None:
            self._frames = {f["Id"]: f for f in self.iter_jsonl("frames.jsonl")}
        return self._frames

    @property
    def nodes(self):
        if self._nodes is None:
            self._nodes = {}
            self._children = collections.defaultdict(list)
            for tree in self.manifest.get("CallTrees", []):
                for node in self.iter_jsonl(tree["File"]):
                    node["_tree"] = tree["Name"]
                    self._nodes[node["Id"]] = node
                    if node.get("Parent") is not None and not node.get("Detached"):
                        self._children[node["Parent"]].append(node["Id"])
        return self._nodes

    @property
    def children(self):
        self.nodes
        return self._children

    def tree(self, name):
        for tree in self.manifest.get("CallTrees", []):
            if tree["Name"] == name:
                return tree
        return None

    def node_name(self, node_id):
        node = self.nodes.get(node_id)
        if node is None:
            return f"node {node_id}"
        if node.get("Function"):
            return node["Function"]
        if node.get("Key"):
            return node["Key"]
        return "(root)" if node.get("Parent") is None else f"node {node_id}"

    def ancestors(self, node_id):
        """The node and its ancestors, innermost first, stopping before the root"""
        path = []
        seen = set()
        node = self.nodes.get(node_id)
        while node is not None and node.get("Parent") is not None and node["Id"] not in seen:
            seen.add(node["Id"])
            path.append(node)
            node = self.nodes.get(node["Parent"])
        return path

    def depth(self, node_id):
        return len(self.ancestors(node_id))

    # -- events ---------------------------------------------------------------------------------------------------------

    @property
    def events(self):
        if self._events is None:
            self._events = {e["EventId"]: e for e in self.iter_jsonl("events.jsonl")}
        return self._events

    def top_level_events(self):
        return sorted((e for e in self.events.values() if e.get("TopLevel")), key=lambda e: (e.get("TimeUs", 0), e["EventId"]))

    def events_of_type(self, *types):
        return sorted((e for e in self.events.values() if e["$type"] in types), key=lambda e: (e.get("TimeUs", 0), e["EventId"]))

    # -- symbols for call store keys -------------------------------------------------------------------------------------

    @property
    def symbols(self):
        """(address, instance) and address to a function name, from the frames"""
        if self._symbols is None:
            exact = {}
            loose = {}
            for frame in self.frames.values():
                name = frame_name(frame)
                exact.setdefault((frame["Address"], frame.get("Instance", 0)), name)
                loose.setdefault(frame["Address"], name)
                loose.setdefault(frame.get("Rva"), name)
            self._symbols = (exact, loose)
        return self._symbols

    def function_name(self, address, instance):
        exact, loose = self.symbols
        return exact.get((address, instance)) or loose.get(address) or f"0x{address:X}"

    # -- binary ---------------------------------------------------------------------------------------------------------

    def open_binary(self, name):
        data = self.read_bytes(name)
        reader = Binary(data)
        magic = reader.u32()
        if magic != MAGIC:
            raise ValueError(f"{name} is not an Internals Viewer binary section")
        reader.format_version = reader.i32()
        return reader

    @property
    def timeline(self):
        if self._timeline is None:
            self._timeline = read_timeline(self) if self.has("fulltrace/timeline.bin") else False
        return self._timeline or None


def frame_name(frame):
    symbol = frame.get("Symbol")
    module = frame.get("SymbolModule") or frame.get("Module", "")
    if symbol:
        symbol = re.sub(r"\+0x[0-9A-Fa-f]+$", "", symbol)
        return f"{module}!{symbol}"
    return f"{module}!0x{frame.get('Rva', 0):X}"


# ----------------------------------------------------------------------------------------------------------------------
# Binary reading
# ----------------------------------------------------------------------------------------------------------------------


class Binary:
    """A little-endian reader over a byte string, matching .NET BinaryWriter"""

    def __init__(self, data):
        self.data = data
        self.view = memoryview(data)
        self.offset = 0
        self.format_version = None

    def _read(self, fmt):
        value = struct.unpack_from(fmt, self.data, self.offset)[0]
        self.offset += struct.calcsize(fmt)
        return value

    def u8(self):
        return self._read("<B")

    def boolean(self):
        return self._read("<?")

    def i32(self):
        return self._read("<i")

    def u32(self):
        return self._read("<I")

    def i64(self):
        return self._read("<q")

    def u64(self):
        return self._read("<Q")

    def f64(self):
        return self._read("<d")

    def raw(self, count):
        value = self.data[self.offset:self.offset + count]
        self.offset += count
        return value

    def string(self):
        length = 0
        shift = 0
        while True:
            byte = self.u8()
            length |= (byte & 0x7F) << shift
            if byte < 0x80:
                break
            shift += 7
        return self.raw(length).decode("utf-8")

    def array(self, fmt):
        """A .NET int32 count followed by that many raw elements, returned as a memoryview"""
        count = self.i32()
        size = struct.calcsize(fmt)
        view = self.view[self.offset:self.offset + count * size].cast(fmt)
        self.offset += count * size
        return view

    def section_version(self):
        """The negative int32 that versions a Full Trace section"""
        return -self.i32()

    def remaining(self):
        return len(self.data) - self.offset


def position(value):
    """Format a packed trace position the way the app does: hex Sequence:Steps"""
    if value == UNKNOWN:
        return "Unknown"
    return f"{value >> 32:X}:{value & 0xFFFFFFFF:X}"


def position_parts(value):
    if value == UNKNOWN:
        return None, None
    return value >> 32, value & 0xFFFFFFFF


class Scale:
    def __init__(self, steps_per_sequence):
        self.steps_per_sequence = steps_per_sequence

    def value(self, packed):
        sequence, steps = position_parts(packed)
        if sequence is None:
            return float("nan")
        return sequence + min(steps, max(self.steps_per_sequence - 1, 0)) / self.steps_per_sequence


class ThreadClock:
    def __init__(self, sequences, starts):
        self.sequences = list(sequences)
        self.starts = list(starts)

    def instructions(self, packed):
        sequence, steps = position_parts(packed)
        if sequence is None:
            return None
        index = bisect.bisect_left(self.sequences, sequence)
        if index < len(self.sequences) and self.sequences[index] == sequence:
            return self.starts[index] + steps
        previous = index - 1
        return self.starts[previous] if previous >= 0 else 0


class Timeline:
    def __init__(self):
        self.version = None
        self.scale = None
        self.start = None
        self.end = None
        self.parents = []
        self.node_ids = []
        self.threads = []

    def node_of(self, raw_index):
        if 0 <= raw_index < len(self.node_ids):
            return self.node_ids[raw_index]
        return -1


class TimelineThread:
    def __init__(self, thread_id, calls, clock, rows):
        self.thread_id = thread_id
        self.calls = calls
        self.clock = clock
        self.rows = rows


def read_point(reader):
    return reader.u64(), reader.u64(), reader.u64()


def read_timeline(export):
    reader = export.open_binary("fulltrace/timeline.bin")
    timeline = Timeline()
    timeline.version = reader.section_version()
    if timeline.version != 3:
        raise ValueError(f"fulltrace/timeline.bin is format {timeline.version}; this reader understands format 3")
    timeline.scale = Scale(reader.f64())
    timeline.start = read_point(reader)
    timeline.end = read_point(reader)
    timeline.parents = reader.array("i")
    timeline.node_ids = reader.array("i")
    for _ in range(reader.i32()):
        thread_id = reader.u32()
        calls = reader.i64()
        clock = ThreadClock(reader.array("I"), reader.array("Q"))
        rows = []
        for _ in range(reader.i32()):
            froms = reader.array("Q")
            tos = reader.array("Q")
            node_indexes = reader.array("i")
            call_indexes = reader.array("I")
            flags = reader.array("B")
            rows.append((froms, tos, node_indexes, call_indexes, flags))
        timeline.threads.append(TimelineThread(thread_id, calls, clock, rows))
    timeline.memory = read_memory_index(reader)
    return timeline


def read_memory_index(reader):
    threads = {}
    for _ in range(reader.i32()):
        thread_id = reader.u32()
        threads[thread_id] = {
            "allocation_starts": reader.array("Q"),
            "allocation_bytes": reader.array("Q"),
            "freed_at": reader.array("Q"),
            "free_starts": reader.array("Q"),
            "free_totals": reader.array("Q"),
        }
    in_use = (reader.array("Q"), reader.array("Q"))
    workspace = (reader.array("Q"), reader.array("Q"))
    return {"threads": threads, "in_use": in_use, "workspace": workspace}


class BitPacked:
    def __init__(self, reader):
        self.count = reader.i32()
        self.width = reader.i32()
        self.words = reader.array("Q")

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if self.width == 0:
            return 0
        bit = index * self.width
        word = bit // 64
        shift = bit % 64
        value = self.words[word] >> shift
        if shift + self.width > 64:
            value |= self.words[word + 1] << (64 - shift)
        return value & ((1 << self.width) - 1)


def read_locators(reader):
    locators = []
    for _ in range(reader.i32()):
        register = reader.i32()
        value_type = reader.u8()
        value = reader.u64()
        detail = reader.u64()
        text = reader.string() if value_type in (6, 7, 10) else None
        locators.append((register, value_type, value, detail, text))
    return locators


class CallStore:
    """The call log: every logged call per (function address, instance address)"""

    def __init__(self, export):
        reader = export.open_binary("fulltrace/calllog.bin")
        self.version = reader.section_version()
        if self.version != 5:
            raise ValueError(f"fulltrace/calllog.bin is format {self.version}; this reader understands format 5")
        self.functions = {}
        for _ in range(reader.i32()):
            key = (reader.u64(), reader.u64())
            chunks = []
            for _ in range(reader.i32()):
                calls = reader.i32()
                chunks.append((calls, bytes(reader.array("B"))))
            self.functions[key] = chunks
        self.unique_locators = read_locators(reader)
        self.locator_tables = {}
        for _ in range(reader.i32()):
            key = (reader.u64(), reader.u64())
            call_indexes = BitPacked(reader)
            palette = reader.array("i")
            palette_indexes = BitPacked(reader)
            self.locator_tables[key] = (call_indexes, palette, palette_indexes)

    def call_count(self, key):
        return sum(calls for calls, _ in self.functions[key])

    def locators_by_call(self, key):
        table = self.locator_tables.get(key)
        result = collections.defaultdict(list)
        if table is None:
            return result
        call_indexes, palette, palette_indexes = table
        for index in range(len(call_indexes)):
            result[call_indexes[index]].append(self.unique_locators[palette[palette_indexes[index]]])
        return result

    def calls(self, key):
        """Yield (call index, from, to, thread, returned, registers, return value, raw node index)"""
        index = 0
        for calls, compressed in self.functions[key]:
            length = calls * 11 * 8
            raw = zlib.decompress(compressed)
            if len(raw) < length:
                raw = raw + bytes(length - len(raw))
            values = list(memoryview(raw[:length]).cast("Q"))
            from_sequence = values[0:calls]
            from_steps = values[calls:2 * calls]
            flags = values[2 * calls:3 * calls]
            to_sequence = values[9 * calls:10 * calls]
            to_steps = values[10 * calls:11 * calls]
            running = 0
            for call in range(calls):
                running += from_sequence[call]
                from_sequence[call] = running & UNKNOWN
                if to_sequence[call] != UNKNOWN:
                    to_sequence[call] = (to_sequence[call] + running) & UNKNOWN
            for call in range(calls):
                registers = [values[(3 + slot) * calls + call] for slot in range(4)]
                yield (index,
                       pack(from_sequence[call], from_steps[call]),
                       pack(to_sequence[call], to_steps[call]),
                       flags[call] & 0xFFFFFFFF,
                       bool(flags[call] & (1 << 32)),
                       registers,
                       values[7 * calls + call],
                       values[8 * calls + call])
                index += 1


def pack(sequence, steps):
    if sequence == UNKNOWN:
        return UNKNOWN
    return ((sequence << 32) | (steps & 0xFFFFFFFF)) & UNKNOWN


def format_locator(locator):
    register, value_type, value, detail, text = locator
    kind = LOCATOR_TYPES[value_type] if value_type < len(LOCATOR_TYPES) else f"type {value_type}"
    where = REGISTERS.get(register, f"slot {register}")
    if value_type == 0:
        return f"{where} page ({value >> 32 & 0xFFFF}:{value & 0xFFFFFFFF})"
    if value_type == 1:
        return f"{where} row ({value >> 32 & 0xFFFF}:{value & 0xFFFFFFFF}:{value >> 48})"
    if value_type == 2:
        object_type = COLUMNSTORE_OBJECT_TYPES.get(value & 0xFFFF, str(value & 0xFFFF))
        return (f"{where} columnstore {object_type} hobt {value & ~0xFFFF} object {detail >> 32} "
                f"column {detail & 0xFFFFFFFF}")
    if value_type in (6, 7, 10):
        return f"{where} {kind.lower()} {text!r}"
    return f"{where} {kind.lower()} 0x{value:X}" + (f" detail 0x{detail:X}" if detail else "")


# ----------------------------------------------------------------------------------------------------------------------
# Symbol classification (the app's mapping rules, copied under references/mappings)
# ----------------------------------------------------------------------------------------------------------------------


class Pattern:
    """One mapping field: exact, a prefix/suffix/contains glob, a general glob, or anything"""

    EXACT = 1_000_000

    def __init__(self, text):
        text = (text or "").strip()
        self.text = text
        if text in ("", "*"):
            self.shape, self.literal, self.score = "any", "", 0
        elif "*" not in text:
            self.shape, self.literal, self.score = "exact", text.lower(), self.EXACT + len(text)
        else:
            self.score = sum(1 for c in text if c != "*")
            inner = text.strip("*")
            if "*" in inner:
                expression = "^" + ".*".join(re.escape(part) for part in text.split("*")) + "$"
                self.shape, self.literal = "glob", re.compile(expression, re.IGNORECASE)
            elif text.startswith("*") and text.endswith("*"):
                self.shape, self.literal = "contains", inner.lower()
            elif text.endswith("*"):
                self.shape, self.literal = "prefix", inner.lower()
            else:
                self.shape, self.literal = "suffix", inner.lower()

    def matches(self, value):
        shape = self.shape
        if shape == "any":
            return True
        if shape == "exact":
            return value == self.literal
        if shape == "prefix":
            return value.startswith(self.literal)
        if shape == "suffix":
            return value.endswith(self.literal)
        if shape == "contains":
            return self.literal in value
        return self.literal.match(value) is not None


class Mappings:
    """Classifies module!Class::Method names the way the app does"""

    def __init__(self, folder):
        self.main = self.load(os.path.join(folder, "SymbolCategories.txt"), 4)
        self.fallback = self.load(os.path.join(folder, "FallbackCategories.txt"), 4)
        self.operators = [r for r in self.load(os.path.join(folder, "Operators.txt"), 5) if r[3]]
        self.modules = {}
        for row in self.load(os.path.join(folder, "ModuleCategories.txt"), 2):
            self.modules[row[0].lower()] = row[1]
        self.cache = {}

    @staticmethod
    def load(path, columns):
        rows = []
        if not os.path.exists(path):
            return rows
        with open(path, encoding="utf-8-sig") as file:
            for order, line in enumerate(file):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                cells = [c.strip() for c in line.split("|")]
                if len(cells) < columns or cells[0] == "Module":
                    continue
                if columns == 2:
                    rows.append((cells[0], cells[1]))
                    continue
                label = cells[3]
                extra = cells[4] if columns == 5 and len(cells) > 4 else ""
                rows.append((Pattern(cells[0]), Pattern(cells[1]), Pattern(cells[2]), label, extra, order))
        return rows

    @staticmethod
    def best(rules, module, class_name, method):
        winner = None
        score = None
        for rule_module, rule_class, rule_function, label, extra, order in rules:
            if not rule_module.matches(module) or not rule_class.matches(class_name) or not rule_function.matches(method):
                continue
            candidate = (rule_module.score + rule_class.score + rule_function.score, rule_function.score, rule_class.score,
                         rule_module.score, order)
            if score is None or candidate > score:
                score = candidate
                winner = (label, extra)
        return winner

    def classify(self, name):
        """(category, operator badge) for a module!Class::Method name"""
        if name in self.cache:
            return self.cache[name]
        module, _, symbol = name.partition("!")
        class_name, method = split_symbol(symbol)
        module, class_name, method = module.lower(), class_name.lower(), method.lower()
        found = self.best(self.main, module, class_name, method) or self.best(self.fallback, module, class_name, method)
        category = found[0] if found else "Unknown"
        operator = self.best(self.operators, module, class_name, method)
        result = (category, operator[0] if operator else "")
        self.cache[name] = result
        return result


def split_symbol(symbol):
    """Class::Method split on the last :: outside template brackets"""
    depth = 0
    split = -1
    index = 0
    while index < len(symbol):
        char = symbol[index]
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        elif char == ":" and depth == 0 and symbol.startswith("::", index):
            split = index
            index += 1
        index += 1
    if split < 0:
        return "", symbol
    return symbol[:split], symbol[split + 2:]


def load_mappings():
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "references", "mappings")
    if not os.path.isdir(folder):
        return None
    return Mappings(folder)



# ----------------------------------------------------------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------------------------------------------------------


def ms(microseconds):
    if microseconds is None:
        return ""
    return f"{microseconds / 1000:.3f}"


def human_bytes(value):
    if value is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def table(rows, headers):
    rows = [[("" if cell is None else str(cell)) for cell in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line.rstrip())
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())


def crop_start(export):
    return export.manifest.get("CropStartUs") or 0


def relative(export, time_us):
    if time_us is None:
        return ""
    return f"{(time_us - crop_start(export)) / 1000:+.3f}"


def event_summary(event):
    description = event.get("Description") or ""
    name = event.get("Name") or ""
    if description and name and description != name:
        return f"{name}: {description}" if name not in description else description
    return description or name or event["$type"]


# ----------------------------------------------------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------------------------------------------------


def command_summary(export, args):
    manifest = export.manifest
    print(f"Server:        {manifest.get('ServerName')}")
    print(f"Database:      {manifest.get('DatabaseName')}")
    print(f"Exported:      {manifest.get('ExportedAt')} (format {manifest.get('FormatVersion')})")
    print(f"Session:       {manifest.get('SessionId')}")
    print(f"Success:       {manifest.get('IsSuccess')}  {manifest.get('Message') or ''}".rstrip())
    print(f"Rows returned: {manifest.get('RowCount')} (result rows are not exported, only the columns)")
    if manifest.get("CropStartUs") is not None:
        print(f"Query window:  {ms(manifest['CropStartUs'])} ms to {ms(manifest['CropEndUs'])} ms capture-relative "
              f"({ms(manifest['CropEndUs'] - manifest['CropStartUs'])} ms)")
    print()
    print("Query:")
    for line in export.read_text("query.sql").splitlines():
        print(f"  {line}")
    print()
    options = manifest.get("QueryOptions", {})
    event_options = manifest.get("EventOptions", {})
    print("Query options: " + ", ".join(f"{k}={v}" for k, v in options.items()))
    print("Event options: " + ", ".join(f"{k}={v}" for k, v in event_options.items() if not isinstance(v, list)))
    if event_options.get("IncludeLockModeCategories") is not None:
        print(f"Lock categories captured: {event_options['IncludeLockModeCategories'] or 'none'}")
    print(f"Full Trace:    {'yes' if export.has('fulltrace/calllog.bin') else 'no'}")
    print()
    for index, result_set in enumerate(manifest.get("ResultSets", [])):
        columns = ", ".join(f"{c['Name']} ({c.get('TypeName') or c.get('ClrType')})" for c in result_set.get("Columns", []))
        print(f"Result set {index}: {columns}")
    print()
    print("Plans:")
    for plan in manifest.get("Plans", []):
        print(f"  plan {plan['Index']} handle {plan['PlanHandleId']} internal={plan['IsInternalPlan']} {plan['File']}"
              + (f" + {plan['BatchFile']}" if plan.get("BatchFile") else ""))
    print()
    print("Call trees:")
    for tree in manifest.get("CallTrees", []):
        extra = ""
        if tree.get("ActivityBuckets"):
            extra = (f" activity {ms(tree['ActivityMinUs'])}-{ms(tree['ActivityMaxUs'])} ms in "
                     f"{tree['ActivityBuckets']} buckets")
        print(f"  {tree['Name']:<12} {tree['File']:<28} root {tree['RootId']}"
              f"{' result tree' if tree.get('IsResultTree') else ''}{' detached' if tree.get('IsDetached') else ''}{extra}")
    print(f"  {manifest.get('NodeCount')} nodes, {manifest.get('EventCount')} events, {manifest.get('LogRecordCount')} log records")
    print()
    counts = collections.Counter()
    top = collections.Counter()
    for event in export.iter_jsonl("events.jsonl"):
        counts[event["$type"]] += 1
        if event.get("TopLevel"):
            top[event["$type"]] += 1
    print("Events by type (top-level / all):")
    for event_type, count in counts.most_common():
        print(f"  {event_type:<26} {top[event_type]:>8} / {count}")
    print()
    print("Entries:")
    for name in export.names():
        line = f"  {name:<34} {export.size(name):>12,} bytes"
        if name.endswith(".bin"):
            try:
                reader = export.open_binary(name)
                version = reader.section_version() if name != "fulltrace/objectpool.bin" else None
                line += f"  format {reader.format_version}"
                if version is not None:
                    line += f", section version {version}" if 0 < version < 100 else ", older section layout (binary not readable)"
            except Exception as error:
                line += f"  ({error})"
        print(line)
    print()
    print("Messages:")
    for line in export.read_text("messages.txt").splitlines():
        print(f"  {line}")


def command_ls(export, args):
    for name in export.names():
        print(f"{export.size(name):>12,}  {name}")


def command_extract(export, args):
    data = export.read_bytes(args.entry)
    target = args.out or os.path.basename(args.entry)
    with open(target, "wb") as file:
        file.write(data)
    print(f"wrote {len(data):,} bytes to {target}")


def match_event(event, args):
    if args.type and event["$type"].lower() != args.type.lower():
        return False
    if args.name and (event.get("Name") or "").lower() != args.name.lower():
        return False
    if args.object and args.object.lower() not in (event.get("ObjectName") or "").lower():
        return False
    if args.page:
        pages = {event.get("PageAddress")} | set(event.get("Pages") or [])
        if args.page not in pages:
            return False
    if args.thread is not None and event.get("ThreadId") != args.thread:
        return False
    if args.node is not None and (event.get("PlanNodeIdentifier") or {}).get("NodeId") != args.node:
        return False
    if args.grep and not re.search(args.grep, json.dumps(event), re.IGNORECASE):
        return False
    return True


def command_events(export, args):
    if args.id is not None:
        event = export.events.get(args.id)
        if event is None:
            print(f"no event {args.id}")
            return
        print(json.dumps(event, indent=2))
        for key in ("Events", "FoldedFrom", "PoolLookup", "CallStack", "LogRecord", "LogRecords"):
            if key in event:
                print(f"\n{key}:")
                ids = event[key] if isinstance(event[key], list) else [event[key]]
                for child_id in ids:
                    if key == "CallStack":
                        path = " < ".join(export.node_name(n["Id"]) for n in export.ancestors(child_id))
                        print(f"  node {child_id}: {path}")
                    elif key.startswith("LogRecord"):
                        print(f"  log record {child_id}")
                    else:
                        child = export.events.get(child_id)
                        if child:
                            print(f"  [{child_id}] {relative(export, child.get('TimeUs'))} {child['$type']} {event_summary(child)}")
        return
    events = export.events.values() if args.all else [e for e in export.events.values() if e.get("TopLevel")]
    events = sorted((e for e in events if match_event(e, args)), key=lambda e: (e.get("TimeUs", 0), e["EventId"]))
    if args.counts:
        counts = collections.Counter((e["$type"], e.get("Name") or "") for e in events)
        table([[t, n, c] for (t, n), c in counts.most_common()], ["Type", "Name", "Count"])
        return
    if args.sort == "duration":
        events.sort(key=lambda e: -(e.get("DurationUs") or 0))
    print(f"{len(events)} events (times are ms from the query start; --all includes folded children)")
    rows = []
    for event in events[args.offset:args.offset + args.limit]:
        rows.append([event["EventId"], relative(export, event.get("TimeUs")), ms(event.get("DurationUs")), event.get("ThreadId"),
                     event["$type"], event_summary(event)[:args.width], (event.get("ObjectName") or "")[:40]])
    table(rows, ["Id", "Time", "Dur ms", "Thr", "Type", "Description", "Object"])


def compact_pages(pages):
    """1:1048-1055 (8) for a run of consecutive pages in one file"""
    parsed = []
    for page in pages:
        file_id, _, page_id = page.partition(":")
        parsed.append((int(file_id), int(page_id)))
    parsed.sort()
    runs = []
    for file_id, page_id in parsed:
        if runs and runs[-1][0] == file_id and runs[-1][2] == page_id - 1:
            runs[-1][2] = page_id
        else:
            runs.append([file_id, page_id, page_id])
    parts = []
    for file_id, first, last in runs:
        parts.append(f"{file_id}:{first}" if first == last else f"{file_id}:{first}-{last} ({last - first + 1})")
    return ", ".join(parts)


def command_reads(export, args):
    groups = export.events_of_type("ReadEventGroup")
    by_object = collections.defaultdict(lambda: collections.Counter())
    distinct = collections.defaultdict(lambda: {"disk": set(), "buffer": set()})
    for group in groups:
        key = group.get("ObjectName") or "(unknown object)"
        kind = "disk" if group.get("ReadType") == "NonCached" else "buffer"
        counter = by_object[key]
        pages = group.get("Pages") or []
        counter[f"{kind} reads"] += 1
        counter[f"{kind} pages"] += len(pages)
        distinct[key][kind].update(pages)
        if group.get("IsReadAhead"):
            counter["read ahead"] += 1
        if group.get("IsAllocationPage"):
            counter["allocation pages"] += 1
        counter["duration us"] += group.get("DurationUs") or 0
    rows = []
    for key, counter in sorted(by_object.items(), key=lambda i: -(i[1]["disk pages"] + i[1]["buffer pages"])):
        sets = distinct[key]
        rows.append([key, counter["disk reads"], counter["disk pages"], len(sets["disk"]), counter["buffer reads"],
                     len(sets["buffer"]), len(sets["disk"] | sets["buffer"]), len(sets["buffer"] - sets["disk"]),
                     counter["read ahead"], counter["allocation pages"], ms(counter["duration us"])])
    table(rows, ["Object", "Disk reads", "Disk pages", "Disk distinct", "Buffer reads", "Buffer distinct", "All distinct",
                 "Warm", "ReadAhead", "AllocPages", "Total ms"])
    print()
    print("A disk read covers every page in Pages (an extent is 8), a buffer pool read is one page, so Buffer reads is also its")
    print("page count. Distinct counts each page once. Warm = buffer pool pages no disk read of this run brought in, so they")
    print("were already in memory before the query. Buffer distinct for an operator is normally its logical reads in the plan.")
    io_events = export.events_of_type("IoEvent")
    file_events = [e for e in export.events_of_type("FileEvent") if e.get("Name") == "file_read_completed"]
    print(f"\n{len(groups)} page reads, {len(io_events)} physical page reads, {len(file_events)} file reads totalling "
          f"{human_bytes(sum(e.get('Size') or 0 for e in file_events))}")
    if args.list:
        print()
        rows = []
        for group in groups[args.offset:args.offset + args.limit]:
            rows.append([group["EventId"], relative(export, group.get("TimeUs")), ms(group.get("DurationUs")),
                         "disk" if group.get("ReadType") == "NonCached" else "buffer",
                         "RA" if group.get("IsReadAhead") else "", compact_pages(group.get("Pages") or [])[:args.width],
                         (group.get("ObjectName") or "")[:40], (group.get("PlanNodeIdentifier") or {}).get("NodeId", "")])
        table(rows, ["Id", "Time", "Dur ms", "Type", "RA", "Pages", "Object", "Node"])
        if len(groups) > args.offset + args.limit:
            print(f"({len(groups) - args.offset - args.limit} more; use --offset and --limit)")


def command_waits(export, args):
    waits = export.events_of_type("WaitEvent")
    counts = collections.defaultdict(lambda: [0, 0, 0])
    for wait in waits:
        entry = counts[wait.get("WaitType")]
        entry[0] += 1
        duration = wait.get("DurationUs") or 0
        entry[1] += duration
        entry[2] = max(entry[2], duration)
    rows = [[k, v[0], ms(v[1]), ms(v[2])] for k, v in sorted(counts.items(), key=lambda i: -i[1][1])]
    table(rows, ["Wait type", "Count", "Total ms", "Max ms"])
    if args.list:
        print()
        rows = []
        for wait in sorted(waits, key=lambda w: -(w.get("DurationUs") or 0))[:args.limit]:
            rows.append([wait["EventId"], relative(export, wait.get("TimeUs")), ms(wait.get("DurationUs")), wait.get("ThreadId"),
                         wait.get("WaitType"), wait.get("PageAddress") or "", (wait.get("ObjectName") or "")[:40]])
        table(rows, ["Id", "Time", "Dur ms", "Thr", "Wait type", "Page", "Object"])


def command_locks(export, args):
    locks = export.events_of_type("LockEvent")
    counts = collections.Counter()
    for lock in locks:
        resource = lock.get("Resource") or {}
        counts[(resource.get("ResourceType"), lock.get("LockMode"), lock.get("Name"), lock.get("ObjectName") or "")] += 1
    rows = [[t, m, n, o[:50], c] for (t, m, n, o), c in sorted(counts.items(), key=lambda i: -i[1])]
    table(rows, ["Resource", "Mode", "Event", "Object", "Count"])
    groups = export.events_of_type("LockGroup")
    if groups:
        print("\nLock groups:")
        for group in groups:
            print(f"  [{group['EventId']}] {relative(export, group.get('TimeUs'))} held {ms(group.get('DurationUs'))} ms: "
                  f"{group.get('Description')}")
    escalations = export.events_of_type("LockEscalationEvent")
    if escalations:
        print("\nLock escalations:")
        for escalation in escalations:
            print(f"  [{escalation['EventId']}] {relative(export, escalation.get('TimeUs'))} {escalation.get('Description')}")
    if args.list:
        print()
        rows = []
        for lock in locks[:args.limit]:
            resource = lock.get("Resource") or {}
            rows.append([lock["EventId"], relative(export, lock.get("TimeUs")), lock.get("Name"), resource.get("ResourceType"),
                         lock.get("LockMode"), resource.get("PageAddress") or resource.get("KeyHash") or "",
                         (lock.get("LockOwnerContext") or {}).get("TransactionId"), (lock.get("ObjectName") or "")[:40]])
        table(rows, ["Id", "Time", "Event", "Resource", "Mode", "Page/Key", "Xact", "Object"])


def command_latches(export, args):
    latches = export.events_of_type("LatchEvent")
    counts = collections.Counter((l.get("LatchClass"), l.get("LatchMode"), l.get("Name")) for l in latches)
    table([[c, m, n, k] for (c, m, n), k in sorted(counts.items(), key=lambda i: -i[1])], ["Class", "Mode", "Event", "Count"])
    pages = collections.Counter(l.get("PageAddress") for l in latches if l.get("PageAddress"))
    print(f"\n{len(pages)} distinct pages latched; most latched:")
    for page, count in pages.most_common(args.limit):
        print(f"  {page:<14} {count}")


def command_operators(export, args):
    operators = export.events_of_type("ExecutionOperatorEvent")
    rows = []
    for operator in sorted(operators, key=lambda o: (o.get("PlanNodeIdentifier") or {}).get("NodeId", -1)):
        node_id = (operator.get("PlanNodeIdentifier") or {}).get("NodeId")
        target = operator.get("OperatorTableName") or ""
        if operator.get("OperatorIndexName"):
            target += "." + operator["OperatorIndexName"]
        threads = operator.get("Threads") or []
        rows.append([node_id, operator.get("ParentNodeId"), operator.get("OperatorDescription"), operator.get("LogicalOperator"),
                     target, relative(export, operator.get("TimeUs")), ms(operator.get("DurationUs")),
                     relative(export, operator.get("EmitStartUs")), operator.get("RowsProcessed"), len(threads),
                     len(operator.get("EntryFrames") or []), len(operator.get("Instances") or [])])
    table(rows, ["Node", "Parent", "Operator", "Logical", "Target", "Start", "Dur ms", "First row", "Rows", "Thr", "Entry", "Inst"])
    if args.frames:
        for operator in operators:
            node_id = (operator.get("PlanNodeIdentifier") or {}).get("NodeId")
            print(f"\nNode {node_id} {operator.get('OperatorDescription')}:")
            for frame in operator.get("EntryFrames") or []:
                print(f"  entry node {frame}: {export.node_name(frame)}")
            for frame in operator.get("ExitFrames") or []:
                print(f"  exit  node {frame}: {export.node_name(frame)}")
            for instance in operator.get("Instances") or []:
                print(f"  iterator instance 0x{instance:X}")
            memory = operator.get("Memory")
            if memory:
                print(f"  memory: allocated {human_bytes(memory.get('Allocated'))} in {memory.get('Allocations')} allocations, "
                      f"freed {human_bytes(memory.get('Freed'))}, peak in use {human_bytes(memory.get('PeakInUse'))}")
                for use in memory.get("Uses") or []:
                    print(f"    {use.get('Caller')} via {use.get('Allocator')}: {human_bytes(use.get('Bytes'))} x{use.get('Allocations')}")
            for thread in operator.get("Threads") or []:
                print(f"  thread {thread.get('ThreadId')}: start {relative(export, thread.get('StartUs'))} ms, "
                      f"{ms(thread.get('DurationUs'))} ms, {thread.get('RowsProcessed')} rows")


def relops_under(element):
    for child in element:
        if child.tag == SHOWPLAN + "RelOp":
            yield child
        else:
            yield from relops_under(child)


def own_elements(element, tag):
    """Descendants with this tag that belong to this RelOp, not to a RelOp nested beneath it"""
    for child in element:
        if child.tag == SHOWPLAN + "RelOp":
            continue
        if child.tag == SHOWPLAN + tag:
            yield child
        yield from own_elements(child, tag)


def plan_object(relop):
    for element in own_elements(relop, "Object"):
        parts = [element.get(a) for a in ("Schema", "Table", "Index") if element.get(a)]
        return ".".join(parts).replace("[", "").replace("]", "")
    return ""


def command_plan(export, args):
    plans = export.manifest.get("Plans", [])
    if not plans:
        print("no plans in the export")
        return
    plan = plans[args.index]
    xml = export.read_text(plan["File"])
    if args.xml:
        print(xml)
        return
    batch = json.loads(export.read_text(plan["BatchFile"])) if plan.get("BatchFile") else {}
    root = ElementTree.fromstring(xml)
    if root.tag == "event":
        for data in root.findall("data"):
            if data.get("name") in ("cpu_time", "duration", "dop", "estimated_rows", "granted_memory_kb", "used_memory_kb"):
                print(f"{data.get('name')}: {data.findtext('value')}")
    for statement in root.iter(SHOWPLAN + "StmtSimple"):
        print(f"Statement: {statement.get('StatementType')} est rows {statement.get('StatementEstRows')} "
              f"cost {statement.get('StatementSubTreeCost')} hash {statement.get('QueryHash')} plan hash {statement.get('QueryPlanHash')}")
        if statement.get("StatementText"):
            print(f"  {statement.get('StatementText')}")
    for query_plan in root.iter(SHOWPLAN + "QueryPlan"):
        print(f"QueryPlan: DOP {query_plan.get('DegreeOfParallelism')} compile {query_plan.get('CompileTime')} ms "
              f"cached size {query_plan.get('CachedPlanSize')} KB")
        grant = query_plan.find(SHOWPLAN + "MemoryGrantInfo")
        if grant is not None:
            print("  memory grant: " + ", ".join(f"{k}={v}" for k, v in grant.attrib.items()))
        stats = query_plan.find(SHOWPLAN + "QueryTimeStats")
        if stats is not None:
            print(f"  elapsed {stats.get('ElapsedTime')} ms, cpu {stats.get('CpuTime')} ms")
        waits = query_plan.find(SHOWPLAN + "WaitStats")
        if waits is not None:
            print("  waits: " + ", ".join(f"{w.get('WaitType')} {w.get('WaitTimeMs')} ms x{w.get('WaitCount')}" for w in waits))
        for warning in query_plan.iter(SHOWPLAN + "Warnings"):
            print("  warnings: " + ", ".join(f"{w.tag.replace(SHOWPLAN, '')} {w.attrib}" for w in warning))
        print()
        for relop in relops_under(query_plan):
            print_relop(relop, 0, batch, args)
    if args.batch and batch:
        print("\nBatch info:")
        print(json.dumps(batch, indent=2))


def print_relop(relop, depth, batch, args):
    node_id = relop.get("NodeId")
    actual_rows = 0
    actual_reads = 0
    executions = 0
    threads = 0
    for counters in own_elements(relop, "RunTimeCountersPerThread"):
        actual_rows += int(counters.get("ActualRows") or 0)
        actual_reads += int(counters.get("ActualLogicalReads") or 0)
        executions += int(counters.get("ActualExecutions") or 0)
        threads += 1
    mode = relop.get("ActualExecutionMode") or relop.get("EstimatedExecutionMode") or ""
    line = (f"{'  ' * depth}[{node_id}] {relop.get('PhysicalOp')} ({relop.get('LogicalOp')}) {plan_object(relop)} "
            f"est {relop.get('EstimateRows')} actual {actual_rows} rows, {executions} exec, {actual_reads} logical reads, "
            f"{mode} {'parallel' if relop.get('Parallel') == '1' else ''}").rstrip()
    print(line)
    if args.detail:
        for predicate in own_elements(relop, "Predicate"):
            print(f"{'  ' * depth}    predicate: {text_of(predicate)}")
        for seek in own_elements(relop, "SeekPredicates"):
            print(f"{'  ' * depth}    seek: {text_of(seek)}")
        for hash_keys in own_elements(relop, "HashKeysBuild"):
            print(f"{'  ' * depth}    hash build keys: {', '.join(c.get('Column', '') for c in hash_keys.iter(SHOWPLAN + 'ColumnReference'))}")
        for hash_keys in own_elements(relop, "HashKeysProbe"):
            print(f"{'  ' * depth}    hash probe keys: {', '.join(c.get('Column', '') for c in hash_keys.iter(SHOWPLAN + 'ColumnReference'))}")
        for warnings in own_elements(relop, "Warnings"):
            print(f"{'  ' * depth}    warnings: {[w.tag.replace(SHOWPLAN, '') for w in warnings]}")
        info = batch.get(node_id)
        if info and (info.get("BatchCount") or info.get("SegmentReads") or info.get("SegmentSkips") or info.get("SegmentScans")):
            scans = info.get("SegmentScans") or []
            print(f"{'  ' * depth}    batch: {info.get('BatchCount')} batches, {info.get('SegmentReads')} segment reads, "
                  f"{info.get('SegmentSkips')} skips, {len(scans)} segment scans")
    for child in relops_under(relop):
        print_relop(child, depth + 1, batch, args)


def text_of(element):
    for scalar in element.iter(SHOWPLAN + "ScalarOperator"):
        if scalar.get("ScalarString"):
            return scalar.get("ScalarString")
    return ElementTree.tostring(element, encoding="unicode")[:200]


def node_line(export, node):
    parts = [f"[{node['Id']}] {export.node_name(node['Id'])}"]
    if node.get("Calls"):
        parts.append(f"calls={node['Calls']:,}")
    if node.get("AllocatedBytes"):
        parts.append(f"alloc={human_bytes(node['AllocatedBytes'])}/{node.get('Allocations', 0):,}")
    locators = node.get("Locators") or {}
    if locators.get("Pages"):
        parts.append(f"pages={len(locators['Pages'])}")
    if locators.get("Rows"):
        parts.append(f"rows={len(locators['Rows'])}")
    if locators.get("Objects"):
        parts.append(f"objects={len(locators['Objects'])}")
    if node.get("Events"):
        parts.append(f"events={len(node['Events'])}")
    return "  ".join(parts)


def subtree_totals(export, node_id, cache):
    if node_id in cache:
        return cache[node_id]
    node = export.nodes[node_id]
    calls = node.get("Calls", 0)
    allocated = node.get("AllocatedBytes", 0)
    allocations = node.get("Allocations", 0)
    for child in export.children.get(node_id, []):
        c, a, n = subtree_totals(export, child, cache)
        calls += c
        allocated += a
        allocations += n
    cache[node_id] = (calls, allocated, allocations)
    return cache[node_id]


def select_roots(export, args):
    """The roots to walk and the nodes to stop at, from --operator, --root or the tree name"""
    if args.operator is not None:
        roots = []
        stop = set()
        for operator in export.events_of_type("ExecutionOperatorEvent"):
            if (operator.get("PlanNodeIdentifier") or {}).get("NodeId") == args.operator:
                roots.extend(operator.get("EntryFrames") or [])
                stop.update(operator.get("ExitFrames") or [])
                print(f"Node {args.operator} {operator.get('OperatorDescription')}: {len(roots)} entry frames, "
                      f"cut at {len(stop)} exit frames")
        if not roots:
            print(f"no operator event for plan node {args.operator}, or it has no entry frames")
        return roots, stop, True
    if args.root is not None:
        return [args.root], set(), True
    tree = export.tree(args.tree) or export.tree("fulltrace") or export.tree("events")
    if tree is None:
        print("no call tree in the export")
        return [], set(), False
    return [tree["RootId"]], set(), False


def subtree_ids(export, roots, stop):
    """Every node under the roots, not descending past a stop node"""
    ids = set()
    stack = list(roots)
    while stack:
        node_id = stack.pop()
        if node_id in ids:
            continue
        ids.add(node_id)
        if node_id in stop:
            continue
        stack.extend(export.children.get(node_id, []))
    return ids


def command_calltree(export, args):
    nodes = export.nodes
    if args.node_path is not None:
        for node in reversed(export.ancestors(args.node_path)):
            pages = (node.get("Locators") or {}).get("Pages") or []
            extra = f"  pages: {compact_pages(pages)[:args.width]}" if pages else ""
            print(f"{'  ' * (export.depth(node['Id']) - 1)}{node_line(export, node)}{extra}")
        return
    roots, stop, scoped = select_roots(export, args)
    if not roots:
        return
    within = subtree_ids(export, roots, stop) if scoped else None
    if args.find:
        pattern = re.compile(args.find, re.IGNORECASE)
        matches = [n for n in nodes.values() if pattern.search(export.node_name(n["Id"]))
                   and (within is None or n["Id"] in within)]
        matches.sort(key=lambda n: -(n.get("Calls") or 0))
        print(f"{len(matches)} matching nodes" + (" in the selected subtree" if scoped else "") + ", most calls first")
        for node in matches[:args.limit]:
            path = [export.node_name(a["Id"]) for a in export.ancestors(node["Id"])[1:]]
            shown = path[:args.path_depth]
            more = f" (+{len(path) - len(shown)} more)" if len(path) > len(shown) else ""
            print(f"{node_line(export, node)}\n    called from: {' < '.join(shown)}{more}")
        if len(matches) > args.limit:
            print(f"(+{len(matches) - args.limit} more; raise --limit or scope with --operator or --root)")
        return
    if args.hot:
        cache = {}
        key = {"calls": lambda n: n.get("Calls") or 0,
               "alloc": lambda n: n.get("AllocatedBytes") or 0,
               "inclusive-alloc": lambda n: subtree_totals(export, n["Id"], cache)[1]}[args.by]
        candidates = [n for n in nodes.values() if within is None or n["Id"] in within]
        ranked = sorted(candidates, key=lambda n: -key(n))[:args.hot]
        for node in ranked:
            inclusive = subtree_totals(export, node["Id"], cache)
            print(f"{node_line(export, node)}  inclusive alloc={human_bytes(inclusive[1])}/{inclusive[2]:,}")
        return
    cache = {}
    printed = 0
    cut = 0
    for root in roots:
        stack = [(root, 0)]
        while stack and printed < args.limit:
            node_id, depth = stack.pop()
            node = nodes.get(node_id)
            if node is None:
                continue
            inclusive = subtree_totals(export, node_id, cache)
            if args.min_calls and inclusive[0] < args.min_calls:
                continue
            children = export.children.get(node_id, [])
            marker = " [exit]" if node_id in stop else ""
            if depth >= args.depth and children and node_id not in stop:
                marker += f"  (+{len(children)} children below --depth {args.depth})"
                cut += 1
            print(f"{'  ' * depth}{node_line(export, node)}{marker}"
                  + (f"  inclusive calls={inclusive[0]:,}" if args.inclusive else ""))
            printed += 1
            if depth >= args.depth or node_id in stop:
                continue
            children = sorted(children, key=lambda c: nodes[c].get("Order", 0), reverse=True)
            for child in children:
                stack.append((child, depth + 1))
    if printed >= args.limit:
        print(f"(stopped after {args.limit} lines; raise --limit, lower --depth or use --min-calls)")
    if cut:
        print(f"({cut} subtrees continue below --depth {args.depth}; use --root <id> --depth N to open one)")


def function_totals(export, grep=None):
    totals = collections.defaultdict(lambda: [0, 0, 0, 0, set()])
    pattern = re.compile(grep, re.IGNORECASE) if grep else None
    for node in export.nodes.values():
        if node.get("Parent") is None:
            continue
        name = export.node_name(node["Id"])
        if pattern and not pattern.search(name):
            continue
        entry = totals[name]
        entry[0] += 1
        entry[1] += node.get("Calls") or 0
        entry[2] += node.get("AllocatedBytes") or 0
        entry[3] += node.get("Allocations") or 0
        locators = node.get("Locators") or {}
        entry[4].update(locators.get("Pages") or [])
    return totals


def command_functions(export, args):
    totals = function_totals(export, args.grep)
    mappings = load_mappings() if not args.no_categories else None
    if args.category and mappings:
        wanted = args.category.replace(" ", "").lower()
        totals = {n: v for n, v in totals.items() if mappings.classify(n)[0].lower() == wanted}
    key = {"calls": 1, "alloc": 2, "nodes": 0, "pages": 4}[args.by]
    ranked = sorted(totals.items(), key=lambda i: -(len(i[1][4]) if key == 4 else i[1][key]))[:args.top]
    rows = []
    for name, v in ranked:
        row = [name, v[0], v[1], human_bytes(v[2]), v[3], len(v[4])]
        if mappings:
            category, operator = mappings.classify(name)
            row += [category, operator]
        rows.append(row)
    headers = ["Function", "Nodes", "Calls", "Alloc", "Allocs", "Pages"] + (["Category", "Operator"] if mappings else [])
    table(rows, headers)


def command_categories(export, args):
    mappings = load_mappings()
    if mappings is None:
        print("the references/mappings folder is missing, so functions cannot be categorised")
        return
    totals = function_totals(export)
    by_category = collections.defaultdict(lambda: [0, 0, 0, 0, collections.Counter()])
    for name, (nodes, calls, allocated, allocations, pages) in totals.items():
        category, _ = mappings.classify(name)
        entry = by_category[category]
        entry[0] += nodes
        entry[1] += calls
        entry[2] += allocated
        entry[3] += allocations
        entry[4][name] += calls
    key = {"calls": 1, "alloc": 2, "nodes": 0}[args.by]
    rows = []
    for category, v in sorted(by_category.items(), key=lambda i: -i[1][key]):
        top = ", ".join(f"{n.split('!', 1)[-1]} x{c:,}" for n, c in v[4].most_common(args.examples))
        rows.append([category, v[0], v[1], human_bytes(v[2]), v[3], top[:args.width]])
    table(rows, ["Category", "Nodes", "Calls", "Alloc", "Allocs", "Most called"])
    print()
    print("Categories follow the app's mapping rules (references/mappings). Extended Events, Tracing, SQL OS Scheduling and")
    print("Synchronisation, I/O Infrastructure and System are infrastructure the app hides by default.")


def format_object_locator(value, detail):
    object_type = COLUMNSTORE_OBJECT_TYPES.get(value & 0xFFFF, str(value & 0xFFFF))
    return f"{object_type} hobt {value & ~0xFFFF} object {detail >> 32} column {detail & 0xFFFFFFFF}"


def command_locators(export, args):
    if args.node is not None:
        node = export.nodes.get(args.node)
        if node is None:
            print(f"no node {args.node}")
            return
        print(node_line(export, node))
        locators = node.get("Locators") or {}
        if locators.get("Pages"):
            print(f"  pages ({len(locators['Pages'])}): {compact_pages(locators['Pages'])}")
        if locators.get("Rows"):
            by_page = collections.defaultdict(list)
            for row in locators["Rows"]:
                file_id, page_id, slot = row.split(":")
                by_page[f"{file_id}:{page_id}"].append(int(slot))
            print(f"  rows ({len(locators['Rows'])}):")
            for page, slots in sorted(by_page.items(), key=lambda i: (int(i[0].split(':')[0]), int(i[0].split(':')[1]))):
                print(f"    {page} slots {sorted(slots)}")
        if locators.get("Objects"):
            print(f"  columnstore objects ({len(locators['Objects'])}):")
            for value, detail in locators["Objects"]:
                print(f"    {format_object_locator(value, detail)}")
        if not locators:
            print("  no locators on this node")
        return
    pages = collections.defaultdict(list)
    rows_by_page = collections.defaultdict(set)
    for node in export.nodes.values():
        locators = node.get("Locators") or {}
        for page in locators.get("Pages") or []:
            pages[page].append(node["Id"])
        for row in locators.get("Rows") or []:
            file_id, page_id, slot = row.split(":")
            rows_by_page[f"{file_id}:{page_id}"].add(int(slot))
    if args.page:
        print(f"page {args.page}: {len(rows_by_page.get(args.page, []))} row slots touched "
              f"{sorted(rows_by_page.get(args.page, []))[:50]}")
        for node_id in pages.get(args.page, []):
            print(f"  {node_line(export, export.nodes[node_id])}")
            print(f"    called from: {' < '.join(export.node_name(a['Id']) for a in export.ancestors(node_id)[1:1 + args.path_depth])}")
        return
    print(f"{len(pages)} distinct pages appear as call arguments, {len(rows_by_page)} with row slots")
    rows = []
    for page, node_ids in sorted(pages.items(), key=lambda i: -len(i[1]))[:args.limit]:
        functions = collections.Counter(export.node_name(n) for n in node_ids)
        rows.append([page, len(node_ids), len(rows_by_page.get(page, [])), ", ".join(f"{f} x{c}" for f, c in functions.most_common(3))[:args.width]])
    table(rows, ["Page", "Nodes", "Slots", "Functions"])


def command_frames(export, args):
    frames = export.frames
    if args.id is not None:
        print(json.dumps(frames.get(args.id), indent=2))
        return
    pattern = re.compile(args.grep, re.IGNORECASE) if args.grep else None
    count = 0
    for frame in frames.values():
        name = frame_name(frame)
        if pattern and not pattern.search(name):
            continue
        print(f"[{frame['Id']}] {name}  address 0x{frame['Address']:X} rva 0x{frame.get('Rva', 0):X}"
              + (f" instance 0x{frame['Instance']:X}" if frame.get("Instance") else "")
              + (f"  {frame['Signature']}" if frame.get("Signature") and args.signatures else ""))
        count += 1
        if count >= args.limit:
            print("(more; use --grep or raise --limit)")
            break


def command_calls(export, args):
    store = CallStore(export)
    timeline = export.timeline
    if args.functions or not args.function:
        rows = []
        pattern = re.compile(args.grep, re.IGNORECASE) if args.grep else None
        for key in store.functions:
            name = export.function_name(*key)
            if pattern and not pattern.search(name):
                continue
            rows.append([name, f"0x{key[0]:X}", f"0x{key[1]:X}" if key[1] else "", store.call_count(key),
                         len(store.locator_tables[key][0]) if key in store.locator_tables else 0])
        rows.sort(key=lambda r: -r[3])
        print(f"{len(store.functions)} (function, instance) call lists in the call log, "
              f"{sum(r[3] for r in rows):,} calls in the {len(rows)} listed")
        table(rows[:args.limit], ["Function", "Address", "Instance", "Calls", "Locators"])
        return
    pattern = re.compile(args.function, re.IGNORECASE)
    keys = [k for k in store.functions if pattern.search(export.function_name(*k))]
    if args.instance:
        wanted = int(args.instance, 16)
        keys = [k for k in keys if k[1] == wanted]
    if not keys:
        print("no function in the call log matches")
        return
    for key in keys:
        name = export.function_name(*key)
        locators = store.locators_by_call(key)
        print(f"\n{name} address 0x{key[0]:X} instance 0x{key[1]:X}: {store.call_count(key):,} calls")
        shown = 0
        for call in store.calls(key):
            index, from_position, to_position, thread, returned, registers, return_value, raw_node = call
            if args.thread is not None and thread != args.thread:
                continue
            if args.value is not None and args.value not in registers and args.value != return_value:
                continue
            if index < args.offset:
                continue
            node_id = timeline.node_of(raw_node) if timeline else -1
            where = export.node_name(node_id) if node_id >= 0 else f"raw node {raw_node}"
            print(f"  #{index} thread {thread} {position(from_position)} to {position(to_position)} "
                  f"RCX=0x{registers[0]:X} RDX=0x{registers[1]:X} R8=0x{registers[2]:X} R9=0x{registers[3]:X}"
                  + (f" RAX=0x{return_value:X}" if returned else " (no return)") + f"  in {where}")
            for locator in locators.get(index, []):
                print(f"      {format_locator(locator)}")
            shown += 1
            if shown >= args.limit:
                print(f"  (stopped after {args.limit} calls; use --offset or raise --limit)")
                break


def command_timeline(export, args):
    timeline = export.timeline
    if timeline is None:
        print("no fulltrace/timeline.bin in the export")
        return
    print(f"timeline format {timeline.version}, {timeline.scale.steps_per_sequence:.0f} steps per sequence, "
          f"start {position(pack(timeline.start[0], timeline.start[1]))} end {position(pack(timeline.end[0], timeline.end[1]))}, "
          f"{len(timeline.node_ids)} replay nodes mapped to the call tree")
    rows = []
    for thread in timeline.threads:
        spans = sum(len(row[0]) for row in thread.rows)
        first = min((row[0][0] for row in thread.rows if len(row[0])), default=UNKNOWN)
        last = max((row[1][len(row[1]) - 1] for row in thread.rows if len(row[1]) and row[1][len(row[1]) - 1] != UNKNOWN), default=UNKNOWN)
        rows.append([thread.thread_id, thread.calls, spans, len(thread.rows), position(first), position(last),
                     thread.clock.instructions(last) if last != UNKNOWN else ""])
    table(rows, ["Thread", "Calls", "Spans", "Depth", "First", "Last", "Instructions"])
    if args.top:
        wanted = None
        if args.node is not None:
            wanted = {i for i, n in enumerate(timeline.node_ids) if n == args.node}
        candidates = []
        for thread in timeline.threads:
            if args.thread is not None and thread.thread_id != args.thread:
                continue
            for depth, (froms, tos, node_indexes, call_indexes, flags) in enumerate(thread.rows):
                if args.depth is not None and depth != args.depth:
                    continue
                for index in range(len(froms)):
                    raw = node_indexes[index]
                    if wanted is not None and raw not in wanted:
                        continue
                    if tos[index] == UNKNOWN:
                        continue
                    if args.axis == "instructions":
                        length = (thread.clock.instructions(tos[index]) or 0) - (thread.clock.instructions(froms[index]) or 0)
                    else:
                        length = timeline.scale.value(tos[index]) - timeline.scale.value(froms[index])
                    candidates.append((length, thread.thread_id, depth, froms[index], tos[index], raw, call_indexes[index], flags[index]))
        candidates.sort(key=lambda c: -c[0])
        print(f"\nlongest {min(args.top, len(candidates))} of {len(candidates):,} spans by {args.axis}:")
        rows = []
        for length, thread_id, depth, start, end, raw, call_index, flag in candidates[:args.top]:
            node_id = timeline.node_of(raw)
            rows.append([f"{length:,.0f}" if args.axis == "instructions" else f"{length:,.3f}", thread_id, depth, position(start),
                         position(end), "" if call_index == 0xFFFFFFFF else call_index, "ret" if flag & 1 else "", node_id,
                         export.node_name(node_id) if node_id >= 0 else f"raw {raw}"])
        table(rows, ["Length", "Thread", "Depth", "From", "To", "Call", "Flags", "Node", "Function"])
    if args.node is not None and not args.top:
        count = 0
        total = 0
        for thread in timeline.threads:
            for froms, tos, node_indexes, call_indexes, flags in thread.rows:
                for index in range(len(froms)):
                    if timeline.node_of(node_indexes[index]) == args.node and tos[index] != UNKNOWN:
                        count += 1
                        total += (thread.clock.instructions(tos[index]) or 0) - (thread.clock.instructions(froms[index]) or 0)
        print(f"\nnode {args.node} {export.node_name(args.node)}: {count:,} spans, {total:,} instructions including callees")


def command_memory(export, args):
    timeline = export.timeline
    if timeline is None:
        print("no fulltrace/timeline.bin in the export (the memory index is stored with it)")
        return
    memory = timeline.memory
    rows = []
    for thread_id, data in memory["threads"].items():
        allocated = sum(data["allocation_bytes"])
        freed = data["free_totals"][len(data["free_totals"]) - 1] if len(data["free_totals"]) else 0
        rows.append([thread_id, len(data["allocation_starts"]), human_bytes(allocated), len(data["free_starts"]), human_bytes(freed)])
    table(rows, ["Thread", "Allocations", "Allocated", "Frees", "Freed"])
    positions, values = memory["in_use"]
    if len(values):
        peak = max(values)
        at = positions[list(values).index(peak)]
        print(f"\nin use: {len(values):,} points, peak {human_bytes(peak)} at {position(at)}")
    positions, values = memory["workspace"]
    if len(values):
        peak = max(values)
        at = positions[list(values).index(peak)]
        print(f"workspace (grant) memory: {len(values):,} points, peak {human_bytes(peak)} at {position(at)}")
    if export.has("fulltrace/memorymap.bin"):
        print()
        command_memorymap(export, argparse.Namespace(grep=args.grep, limit=args.limit, by="class", address=None, width=90))


def command_memorymap(export, args):
    if not export.has("fulltrace/memorymap.bin"):
        print("no fulltrace/memorymap.bin in the export")
        return
    reader = export.open_binary("fulltrace/memorymap.bin")
    version = reader.section_version()
    if version not in (1, 2):
        print(f"memory map format {version} is not understood (expected 1 or 2)")
        return
    names = [reader.string() for _ in range(reader.i32())]
    count = reader.i32()
    groups = collections.defaultdict(lambda: [0, 0, set()])
    by_operation = collections.Counter()
    entries = []
    wanted = int(args.address, 16) if args.address else None
    for _ in range(count):
        address = reader.u64()
        call_index = reader.i32()
        owner = reader.u64()
        from_sequence, from_steps, to_sequence, to_steps = reader.u64(), reader.u64(), reader.u64(), reader.u64()
        call_node_index = reader.i32()
        thread = reader.u32()
        operation = reader.u8()
        class_index = reader.i32()
        size = reader.u64()
        site_index = reader.i32() if version >= 2 else -1
        class_name = names[class_index] if class_index >= 0 else None
        site = names[site_index] if site_index >= 0 else None
        operation_name = MEMORY_MAP_OPERATIONS[operation] if operation < len(MEMORY_MAP_OPERATIONS) else str(operation)
        by_operation[operation_name] += 1
        if args.by == "site":
            key = site or "(no site)"
        elif args.by == "operation":
            key = operation_name
        else:
            key = class_name or site or "(unlabelled)"
        group = groups[key]
        group[0] += 1
        group[1] += size
        group[2].add(address)
        matched = (args.grep and re.search(args.grep, f"{class_name} {site}", re.IGNORECASE)) or (wanted is not None and address == wanted)
        if matched:
            entries.append((address, size, operation_name, class_name, site, thread, call_node_index,
                            pack(from_sequence, from_steps), pack(to_sequence, to_steps), owner))
    print(f"memory map format {version}: {count:,} entries, {len(names)} names; "
          + ", ".join(f"{k} {v:,}" for k, v in by_operation.most_common()))
    print("An entry is one labelling of an address: Allocated at its allocation, Constructed when a constructor ran on it,")
    print("Declared when a call declared its type, Visited when a method ran on it. Blocks counts distinct addresses.")
    rows = [[name[:args.width], v[0], len(v[2]), human_bytes(v[1])]
            for name, v in sorted(groups.items(), key=lambda i: -i[1][1])[:args.limit]]
    table(rows, [{"site": "Allocation site", "operation": "Operation"}.get(args.by, "Class or site"), "Entries", "Blocks", "Bytes"])
    if entries:
        print(f"\n{len(entries)} entries matching:")
        timeline = export.timeline
        entries.sort(key=lambda e: (e[0], e[7]))
        for address, size, operation, class_name, site, thread, raw, start, end, owner in entries[:args.limit]:
            node_id = timeline.node_of(raw) if timeline else -1
            print(f"  0x{address:X} {human_bytes(size)} {operation} {(class_name or '')[:args.width]} {site or ''} thread {thread} "
                  f"{position(start)} to {position(end)} owner 0x{owner:X} in {export.node_name(node_id) if node_id >= 0 else raw}")
        if len(entries) > args.limit:
            print(f"  (+{len(entries) - args.limit} more; raise --limit or narrow --grep)")


def command_objectpool(export, args):
    if not export.has("fulltrace/objectpool.bin"):
        print("no fulltrace/objectpool.bin in the export")
        return
    reader = export.open_binary("fulltrace/objectpool.bin")
    rows = []
    for _ in range(reader.i32()):
        entry_address, data_address, memory_object = reader.u64(), reader.u64(), reader.u64()
        pages_kb = reader.i64()
        object_type = reader.i32()
        database_id = reader.i32()
        hobt_id = reader.u64()
        column_id = reader.i32()
        object_id = reader.i32()
        object_size = reader.i64()
        rows.append([COLUMNSTORE_OBJECT_TYPES.get(object_type, object_type), database_id, hobt_id, column_id, object_id,
                     human_bytes(object_size), pages_kb, f"0x{data_address:X}"])
    print(f"{len(rows)} columnstore object pool entries at the end of the run")
    table(rows[:args.limit], ["Type", "Db", "Hobt", "Column", "Object", "Size", "Pages KB", "Data"])


def command_plannodes(export, args):
    if not export.has("fulltrace/plannodes.bin"):
        print("no fulltrace/plannodes.bin in the export")
        return
    reader = export.open_binary("fulltrace/plannodes.bin")
    version = reader.section_version()
    rows = []
    for _ in range(reader.i32()):
        iterator, plan_object, batch_plan_object, query = reader.u64(), reader.u64(), reader.u64(), reader.u64()
        node_id = reader.i32()
        constructed = read_point(reader) if reader.boolean() else None
        is_batch_helper = reader.boolean() if version >= 3 else False
        rows.append([node_id, f"0x{iterator:X}", f"0x{plan_object:X}", f"0x{batch_plan_object:X}" if batch_plan_object else "",
                     f"0x{query:X}", position(pack(constructed[0], constructed[1])) if constructed else "", is_batch_helper])
    print(f"plan nodes format {version}: {len(rows)} iterator objects linked to plan nodes")
    table(rows, ["Node", "Iterator", "Plan object", "Batch plan object", "Query", "Constructed", "Batch helper"])


def command_teardowns(export, args):
    if not export.has("fulltrace/teardowns.bin"):
        print("no fulltrace/teardowns.bin in the export")
        return
    reader = export.open_binary("fulltrace/teardowns.bin")
    version = reader.section_version()
    rows = []
    for _ in range(reader.i32()):
        thread = reader.u32()
        end = read_point(reader)
        rows.append([thread, position(pack(end[0], end[1])), end[2]])
    print(f"teardowns format {version}: where the query's execution ended on each thread")
    table(rows, ["Thread", "Position", "Instructions"])


def command_output(export, args):
    if not export.has("fulltrace/output.bin"):
        print("no fulltrace/output.bin in the export")
        return
    reader = export.open_binary("fulltrace/output.bin")
    version = reader.section_version()
    rows = []
    for _ in range(reader.i32()):
        output_row = reader.u64()
        direct = reader.u64()
        owners = [reader.u64() for _ in range(reader.i32())]
        rows.append([f"0x{output_row:X}", f"0x{direct:X}", ", ".join(f"0x{o:X}" for o in owners)])
    print(f"output structures format {version}: {len(rows)} output rows, each column's owning iterator object")
    table(rows[:args.limit], ["Output row", "Direct select owner", "Column owners"])


def command_memoryhistory(export, args):
    if not export.has("fulltrace/memoryhistory.bin"):
        print("no fulltrace/memoryhistory.bin in the export")
        return
    reader = export.open_binary("fulltrace/memoryhistory.bin")
    version = reader.section_version()
    if version != 8:
        print(f"memory history format {version} is not understood (expected 8)")
        return
    blocks = []
    for _ in range(reader.i32()):
        iterator, address = reader.u64(), reader.u64()
        size = reader.i32()
        is_iterator, is_inline = reader.boolean(), reader.boolean()
        thread = reader.u32()
        call_node_index = reader.i32()
        start = pack(reader.u64(), reader.u64())
        end = pack(reader.u64(), reader.u64())
        from_instructions, to_instructions = reader.u64(), reader.u64()
        first_write, write_count = reader.i32(), reader.i32()
        blocks.append((iterator, address, size, is_iterator, is_inline, thread, call_node_index, start, end, first_write, write_count))
    writes = []
    for _ in range(reader.i32()):
        at = pack(reader.u64(), reader.u64())
        instructions = reader.u64()
        call_node_index, call_index, offset = reader.i32(), reader.i32(), reader.i32()
        change = reader.u8()
        data = reader.raw(reader.i32())
        writes.append((at, instructions, call_node_index, call_index, offset, change, data))
    iterators = {reader.i32(): reader.u64() for _ in range(reader.i32())}
    timeline = export.timeline

    def allocated_in(block):
        node_id = timeline.node_of(block[6]) if timeline else -1
        return export.node_name(node_id) if node_id >= 0 else str(block[6])

    print(f"memory history format {version}: {len(blocks):,} watched blocks, {len(writes):,} writes, {len(iterators)} iterator nodes")
    print("A block is memory the query allocated and wrote to. Kind iterator = an operator object, inline = carved from a larger")
    print("block. Iterator = the operator object the block belongs to (0 when none). Writes are byte changes, each with its writer.")
    selected = list(enumerate(blocks))
    if args.iterator:
        wanted = int(args.iterator, 16)
        selected = [(i, b) for i, b in selected if b[0] == wanted or b[1] == wanted]
    if args.address:
        wanted = int(args.address, 16)
        selected = [(i, b) for i, b in selected if b[1] <= wanted < b[1] + b[2]]
    if args.allocated_in:
        pattern = re.compile(args.allocated_in, re.IGNORECASE)
        selected = [(i, b) for i, b in selected if pattern.search(allocated_in(b))]
    if args.kind:
        selected = [(i, b) for i, b in selected if (args.kind == "iterator" and b[3]) or (args.kind == "inline" and b[4])]
    if args.sort == "writes":
        selected.sort(key=lambda i: -i[1][10])
    rows = []
    for index, block in selected[:args.limit]:
        iterator, address, size, is_iterator, is_inline, thread, raw, start, end, first_write, write_count = block
        rows.append([index, f"0x{address:X}", size, "iterator" if is_iterator else ("inline" if is_inline else ""), f"0x{iterator:X}",
                     thread, position(start), position(end), write_count, allocated_in(block)[:args.width]])
    print(f"{len(selected)} blocks selected")
    table(rows, ["#", "Address", "Size", "Kind", "Iterator", "Thread", "From", "To", "Writes", "Allocated in"])
    if len(selected) > args.limit:
        print(f"(+{len(selected) - args.limit} more; raise --limit or filter with --iterator, --address, --allocated-in, --kind)")
    if args.block is not None and args.block < len(blocks):
        block = blocks[args.block]
        block_writes = writes[block[9]:block[9] + block[10]]
        writers = collections.Counter()
        offsets = collections.Counter()
        for at, instructions, raw, call_index, offset, change, data in block_writes:
            node_id = timeline.node_of(raw) if timeline else -1
            writers[export.node_name(node_id) if node_id >= 0 else str(raw)] += 1
            offsets[(offset, len(data))] += 1
        print(f"\nblock {args.block} 0x{block[1]:X} size {block[2]}: {len(block_writes):,} writes by {len(writers)} functions")
        print("writers: " + ", ".join(f"{w} x{c:,}" for w, c in writers.most_common(args.writers)))
        print("most written fields (offset/length): " + ", ".join(f"+0x{o:X}/{l} x{c:,}" for (o, l), c in offsets.most_common(args.writers)))
        print(f"\nwrites (use --offset and --limit to page, --field to follow one offset):")
        shown = 0
        for write in block_writes[args.offset:]:
            at, instructions, raw, call_index, offset, change, data = write
            if args.field is not None and offset != args.field:
                continue
            node_id = timeline.node_of(raw) if timeline else -1
            value = int.from_bytes(data, "little") if len(data) in (1, 2, 4, 8) else None
            print(f"  {position(at)} +0x{offset:X} {MEMORY_HISTORY_CHANGES[change] if change < 2 else change} {data.hex()}"
                  + (f" (= 0x{value:X})" if value is not None else "")
                  + f" by {export.node_name(node_id) if node_id >= 0 else raw} call {call_index}")
            shown += 1
            if shown >= args.limit:
                print(f"  (stopped after {args.limit} writes)")
                break


def command_columnstore(export, args):
    scans = export.events_of_type("SegmentScanEvent")
    rows = []
    for scan in scans:
        rows.append([scan["EventId"], scan.get("RowGroupId"), scan.get("ColumnId"), scan.get("EncodingType"), scan.get("CompressedDataType"),
                     scan.get("FilterType"), scan.get("FilterOnCompressedDataType") if scan.get("IsFilterOnCompressedDataUsed") else "",
                     scan.get("InputRows"), scan.get("OutputRows"), scan.get("PureRowBuckets"), scan.get("ImpureRowBuckets"),
                     scan.get("PrimaryDictionaryValueCount"), scan.get("BitPacking")])
    if rows:
        print("Segment scans:")
        table(rows, ["Id", "Rowgroup", "Col", "Encoding", "Type", "Filter", "Compressed filter", "In", "Out", "Pure", "Impure", "Dict", "Bits"])
    eliminated = export.events_of_type("SegmentEliminateEvent")
    if eliminated:
        print("\nRowgroups eliminated: " + ", ".join(
            f"{e.get('RowGroupId')}{' (unique value filter)' if e.get('IsEliminatedByUniqueValueFilter') else ''}" for e in eliminated))
    pool = export.events_of_type("ObjectPoolEvent")
    if pool:
        counts = collections.Counter((p.get("ObjectType"), "hit" if p.get("IsHit") else "miss") for p in pool)
        print("\nObject pool lookups: " + ", ".join(f"{t} {h} x{c}" for (t, h), c in counts.most_common()))
    for group in export.events_of_type("RowGroupScanEvent"):
        print(f"\n[{group['EventId']}] {group.get('Description')} {relative(export, group.get('TimeUs'))} ms for {ms(group.get('DurationUs'))} ms")
    for event in export.events_of_type("ColumnStoreScanEvent", "ColumnstoreFilterEvent", "BatchModeEvent"):
        print(f"[{event['EventId']}] {relative(export, event.get('TimeUs'))} {event['$type']} {event_summary(event)}")


def command_logrecords(export, args):
    records = list(export.iter_jsonl("log-records.jsonl"))
    print(f"{len(records)} log records")
    counts = collections.Counter((r.get("Operation"), r.get("Context")) for r in records)
    table([[o, c, n] for (o, c), n in counts.most_common()], ["Operation", "Context", "Count"])
    if args.list:
        for record in records[:args.limit]:
            print(f"[{record.get('LogRecordId')}] {record.get('Lsn')} {record.get('Operation')}/{record.get('Context')} "
                  f"{record.get('PageAddress') or ''} slot {record.get('SlotId', '')} xact {record.get('LogTransactionId')} "
                  f"{record.get('Description') or ''}")


# ----------------------------------------------------------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", help="the .ivtrace file, or a folder it was extracted into")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("summary", help="what the run was: server, query, options, plans, counts, entries, messages")
    commands.add_parser("ls", help="list the entries in the file with their sizes")

    extract = commands.add_parser("extract", help="write one entry out to a file")
    extract.add_argument("entry")
    extract.add_argument("--out")

    events = commands.add_parser("events", help="list or count events, or show one event in full")
    events.add_argument("--id", type=int, help="print one event as JSON with its children and call stack")
    events.add_argument("--type", help="event $type, e.g. ReadEventGroup, WaitEvent, LockEvent")
    events.add_argument("--name", help="event Name, e.g. latch_acquired, lock_released")
    events.add_argument("--object", help="substring of the object name")
    events.add_argument("--page", help="page address f:p the event is about")
    events.add_argument("--thread", type=int)
    events.add_argument("--node", type=int, help="plan node id the event is attributed to")
    events.add_argument("--grep", help="regex over the event JSON")
    events.add_argument("--all", action="store_true", help="include folded children, not just top-level events")
    events.add_argument("--counts", action="store_true", help="count by type and name instead of listing")
    events.add_argument("--sort", choices=["time", "duration"], default="time")
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--offset", type=int, default=0)
    events.add_argument("--width", type=int, default=90)

    reads = commands.add_parser("reads", help="page reads per object: disk vs buffer pool, pages, read-ahead")
    reads.add_argument("--list", action="store_true", help="also list every read in time order")
    reads.add_argument("--limit", type=int, default=200, help="reads to list")
    reads.add_argument("--offset", type=int, default=0, help="first read to list")
    reads.add_argument("--width", type=int, default=80, help="width of the Pages column")

    waits = commands.add_parser("waits", help="waits by type with counts and durations")
    waits.add_argument("--list", action="store_true", help="also list the longest waits")
    waits.add_argument("--limit", type=int, default=50, help="waits to list")

    locks = commands.add_parser("locks", help="locks by resource and mode, lock groups and escalations")
    locks.add_argument("--list", action="store_true", help="also list every lock event in time order")
    locks.add_argument("--limit", type=int, default=200, help="lock events to list")

    latches = commands.add_parser("latches", help="latches by class and mode, most-latched pages")
    latches.add_argument("--limit", type=int, default=20, help="pages to list")

    operators = commands.add_parser("operators", help="plan operators with timing, rows, threads and call tree entry frames")
    operators.add_argument("--frames", action="store_true", help="also list entry and exit frames, instances and memory")

    plan = commands.add_parser("plan", help="the execution plan as an indented operator tree with actual rows")
    plan.add_argument("--index", type=int, default=0)
    plan.add_argument("--detail", action="store_true", help="predicates, seek predicates, warnings and batch info per operator")
    plan.add_argument("--batch", action="store_true", help="dump the batch (columnstore) info JSON")
    plan.add_argument("--xml", action="store_true", help="print the raw plan XML")

    calltree = commands.add_parser("calltree", help="print, search or rank the call tree")
    calltree.add_argument("--tree", default="fulltrace", help="call tree name from the manifest (fulltrace, events)")
    calltree.add_argument("--root", type=int, help="start at this node id")
    calltree.add_argument("--operator", type=int, help="start at the entry frames of this plan node id and stop at its exit frames")
    calltree.add_argument("--depth", type=int, default=6, help="levels to print below the root (default 6); deeper subtrees are marked")
    calltree.add_argument("--min-calls", type=int, default=0, help="hide subtrees with fewer calls including children")
    calltree.add_argument("--inclusive", action="store_true", help="show calls including children on each line")
    calltree.add_argument("--limit", type=int, default=300, help="maximum lines")
    calltree.add_argument("--find", help="regex over function names; lists matches with their call path, scoped by --operator or --root")
    calltree.add_argument("--width", type=int, default=100, help="width of the pages shown by --path")
    calltree.add_argument("--path", dest="node_path", type=int, help="print the call path from the root to this node id")
    calltree.add_argument("--path-depth", type=int, default=8)
    calltree.add_argument("--hot", type=int, help="list the top N nodes, scoped by --operator or --root")
    calltree.add_argument("--by", choices=["calls", "alloc", "inclusive-alloc"], default="calls")

    functions = commands.add_parser("functions", help="totals per function across the whole call tree, with the app's category")
    functions.add_argument("--top", type=int, default=40)
    functions.add_argument("--by", choices=["calls", "alloc", "nodes", "pages"], default="calls")
    functions.add_argument("--grep", help="regex over function names")
    functions.add_argument("--category", help="only functions in this category, e.g. BTreeNavigation, Latching")
    functions.add_argument("--no-categories", action="store_true", help="skip the classification (faster on huge trees)")

    categories = commands.add_parser("categories", help="calls and allocations per engine category (B-Tree Navigation, Latching, etc.)")
    categories.add_argument("--by", choices=["calls", "alloc", "nodes"], default="calls")
    categories.add_argument("--examples", type=int, default=3)
    categories.add_argument("--width", type=int, default=110)

    locators = commands.add_parser("locators", help="pages and rows seen as call arguments, and which functions touched them")
    locators.add_argument("--page", help="page address f:p to drill into")
    locators.add_argument("--node", type=int, help="list the pages, rows and columnstore objects on one call tree node")
    locators.add_argument("--limit", type=int, default=50)
    locators.add_argument("--width", type=int, default=100)
    locators.add_argument("--path-depth", type=int, default=6)

    frames = commands.add_parser("frames", help="look up resolved frames (symbols)")
    frames.add_argument("--id", type=int)
    frames.add_argument("--grep")
    frames.add_argument("--signatures", action="store_true")
    frames.add_argument("--limit", type=int, default=50)

    calls = commands.add_parser("calls", help="Full Trace call log: functions logged, or the calls of one function with arguments")
    calls.add_argument("--functions", action="store_true", help="list logged functions with call counts (the default)")
    calls.add_argument("--grep", help="regex to filter the function list")
    calls.add_argument("--function", help="regex naming the function whose calls to dump")
    calls.add_argument("--instance", help="hex instance (this) address to narrow to one object")
    calls.add_argument("--thread", type=int)
    calls.add_argument("--value", type=lambda v: int(v, 0), help="only calls with this value in a register or the return")
    calls.add_argument("--offset", type=int, default=0)
    calls.add_argument("--limit", type=int, default=50)

    timeline = commands.add_parser("timeline", help="Full Trace timeline: threads, span counts, longest spans")
    timeline.add_argument("--top", type=int, help="list the N longest spans")
    timeline.add_argument("--axis", choices=["instructions", "position"], default="instructions")
    timeline.add_argument("--thread", type=int)
    timeline.add_argument("--depth", type=int)
    timeline.add_argument("--node", type=int, help="restrict to spans of this call tree node id")

    memory = commands.add_parser("memory", help="Full Trace memory: allocations per thread, peak in use, memory map by class")
    memory.add_argument("--grep")
    memory.add_argument("--limit", type=int, default=40)

    memorymap = commands.add_parser("memorymap", help="Full Trace memory map: allocations labelled by class and allocation site")
    memorymap.add_argument("--grep", help="regex over class name or site to list matching entries")
    memorymap.add_argument("--address", help="hex address to list the entries of one block")
    memorymap.add_argument("--by", choices=["class", "site", "operation"], default="class", help="how to group the totals")
    memorymap.add_argument("--width", type=int, default=90, help="truncate long template class names")
    memorymap.add_argument("--limit", type=int, default=40)

    objectpool = commands.add_parser("objectpool", help="columnstore object pool snapshot")
    objectpool.add_argument("--limit", type=int, default=100)

    commands.add_parser("plannodes", help="iterator objects linked to plan node ids")
    commands.add_parser("teardowns", help="where the query ended on each thread")

    output = commands.add_parser("output", help="output row structures and the iterator owning each column")
    output.add_argument("--limit", type=int, default=50)

    history = commands.add_parser("memoryhistory", help="memory history: the blocks the query wrote to, and every write with its writer")
    history.add_argument("--block", type=int, help="block number (#) whose writes to show")
    history.add_argument("--iterator", help="hex address: only blocks belonging to this iterator object")
    history.add_argument("--address", help="hex address: only the block containing it")
    history.add_argument("--allocated-in", help="regex over the allocating function, e.g. CBpBatch::")
    history.add_argument("--kind", choices=["iterator", "inline"])
    history.add_argument("--sort", choices=["index", "writes"], default="index")
    history.add_argument("--field", type=lambda v: int(v, 0), help="with --block, only writes at this offset (0x18)")
    history.add_argument("--offset", type=int, default=0, help="with --block, first write to show")
    history.add_argument("--writers", type=int, default=8, help="with --block, how many writers and fields to summarise")
    history.add_argument("--width", type=int, default=70)
    history.add_argument("--limit", type=int, default=50)

    commands.add_parser("columnstore", help="segment scans, eliminations, object pool lookups and batch events")

    logrecords = commands.add_parser("logrecords", help="transaction log records captured with the run")
    logrecords.add_argument("--list", action="store_true")
    logrecords.add_argument("--limit", type=int, default=200)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    export = Export(args.path)
    handler = globals()[f"command_{args.command}"]
    try:
        handler(export, args)
    except ValueError as error:
        print(f"cannot read this section: {error}", file=sys.stderr)
        print("The JSON and XML entries are still readable with the other commands.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
        sys.stdout.flush()
    except (BrokenPipeError, OSError) as error:
        if isinstance(error, BrokenPipeError) or getattr(error, "errno", None) in (22, 32):
            os._exit(0)
        raise
