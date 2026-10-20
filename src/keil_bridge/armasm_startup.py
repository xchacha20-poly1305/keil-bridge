"""Reading a CMSIS-style armasm startup file (startup_<device>.s).

Only the usual Keil device startup layout is accepted: stack and heap areas,
a vector table of DCD entries, a Reset_Handler that calls SystemInit and
__main, and weak handlers that loop forever. Anything else is reported as an
error with its line number, because a GCC startup generated from a partial
understanding would be silently wrong.
"""

from dataclasses import dataclass, field

from .diagnostics import Diagnostics

DIRECTIVES_IGNORED = {"PRESERVE8", "THUMB", "ALIGN", "END", "LTORG", "IMPORT"}
KNOWN_AREAS = {"STACK", "HEAP", "RESET", "|.text|"}
KNOWN_EQUS = {"Stack_Size", "Heap_Size", "__Vectors_Size"}
KNOWN_SPACES = {("Stack_Mem", "Stack_Size"), ("Heap_Mem", "Heap_Size")}

RESET_WITH_SYSTEMINIT = [
    ("LDR", "R0,=SystemInit"),
    ("BLX", "R0"),
    ("LDR", "R0,=__main"),
    ("BX", "R0"),
]
RESET_WITHOUT_SYSTEMINIT = [("LDR", "R0,=__main"), ("BX", "R0")]
LOOP_FOREVER = [("B", ".")]


@dataclass
class StartupInfo:
    vectors: list[str | None]  # None for reserved (DCD 0) entries; [0] is the stack pointer
    stack_size: int | None = None
    heap_size: int | None = None
    calls_system_init: bool = True
    weak: set[str] = field(default_factory=set)


@dataclass
class _Line:
    number: int
    label: str
    op: str
    operands: str


def is_armasm(text: str) -> bool:
    """GNU assembler sources use '.' directives and '@' or '//' comments;
    armasm sources use AREA. Decide by the first AREA or '.section'."""
    for raw in text.splitlines():
        words = raw.split(";")[0].split()
        if not words:
            continue
        if "AREA" in words:
            return True
        if words[0].startswith("."):
            return False
    return False


def parse(text: str, path: str, diag: Diagnostics) -> StartupInfo | None:
    errors_before = diag.error_count()
    parser = _Parser(path, diag)
    for line in _logical_lines(text):
        parser.feed(line)
    info = parser.finish()
    if diag.error_count() > errors_before:
        diag.note(
            "the startup file is translated only when it follows the standard Keil layout; "
            'otherwise port it to GCC by hand, add it with sources_add and set startup = ""',
            path,
        )
        return None
    return info


def _logical_lines(text: str):
    """Strip ';' comments, join '\\' continuations and split label/op/operands."""
    pending = ""
    pending_number = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        code = raw.split(";")[0].rstrip()
        if not pending:
            pending_number = number
        if code.endswith("\\"):
            pending += code[:-1] + " "
            continue
        code = pending + code
        pending = ""
        if not code.strip():
            continue
        # A statement starting in column 0 begins with a label.
        label = ""
        if not code[0].isspace():
            label = code.split(None, 1)[0]
            code = code[len(label) :]
        words = code.split(None, 1)
        op = words[0].upper() if words else ""
        operands = words[1].strip() if len(words) > 1 else ""
        yield _Line(pending_number, label, op, operands)


class _Parser:
    def __init__(self, path: str, diag: Diagnostics):
        self.path = path
        self.diag = diag
        self.equs: dict[str, int] = {}
        self.vectors: list[str | None] = []
        self.in_vectors = False
        self.weak: set[str] = set()
        self.proc: str | None = None
        self.proc_body: list[tuple[str, str]] = []
        self.proc_line = 0
        self.calls_system_init = True
        self.saw_reset = False
        self.conditional_depth = 0

    def error(self, line: _Line, message: str):
        self.diag.error(message, self.path, line.number)

    def feed(self, line: _Line):
        if self.conditional_depth:
            # Only the MicroLIB/__user_initial_stackheap block is conditional
            # in a standard startup; GCC uses its own heap setup instead.
            if line.op == "IF":
                self.conditional_depth += 1
            elif line.op == "ENDIF":
                self.conditional_depth -= 1
            return
        if line.op == "IF":
            if line.operands.replace(" ", "") != ":DEF:__MICROLIB":
                self.error(line, f"unsupported conditional 'IF {line.operands}'")
            self.conditional_depth = 1
            return

        if line.label:
            self.on_label(line)
        if not line.op:
            return
        handler = getattr(self, f"op_{line.op}", None)
        if handler is not None:
            handler(line)
        elif line.op in DIRECTIVES_IGNORED:
            pass
        elif self.proc is not None:
            self.proc_body.append((line.op, line.operands.replace(" ", "")))
        else:
            self.error(line, f"unsupported statement '{line.op} {line.operands}'".rstrip())

    def on_label(self, line: _Line):
        if line.label == "__Vectors":
            self.in_vectors = True
        elif line.label == "__Vectors_End":
            self.in_vectors = False

    def op_EQU(self, line: _Line):
        if line.label not in KNOWN_EQUS:
            self.error(line, f"unsupported constant '{line.label} EQU {line.operands}'")
            return
        if line.label == "__Vectors_Size":
            return
        try:
            self.equs[line.label] = int(line.operands, 0)
        except ValueError:
            self.error(line, f"cannot evaluate '{line.operands}'")

    def op_AREA(self, line: _Line):
        name = line.operands.split(",")[0].strip()
        if name not in KNOWN_AREAS:
            self.error(line, f"unsupported AREA {name}")

    def op_SPACE(self, line: _Line):
        if (line.label, line.operands) not in KNOWN_SPACES:
            self.error(line, f"unsupported SPACE '{line.label} SPACE {line.operands}'")

    def op_EXPORT(self, line: _Line):
        words = line.operands.split()
        if len(words) == 2 and words[1] == "[WEAK]":
            self.weak.add(words[0])
        elif len(words) != 1:
            self.error(line, f"unsupported EXPORT {line.operands}")

    def op_DCD(self, line: _Line):
        if not self.in_vectors:
            self.error(line, "DCD outside the vector table")
            return
        for item in line.operands.split(","):
            item = item.strip()
            if item == "0":
                self.vectors.append(None)
            elif item.isidentifier():
                self.vectors.append(item)
            else:
                self.error(line, f"unsupported vector table entry {item!r}")

    def op_PROC(self, line: _Line):
        if self.proc is not None:
            self.error(line, "nested PROC")
        self.proc = line.label
        self.proc_body = []
        self.proc_line = line.number

    def op_ENDP(self, line: _Line):
        if self.proc is None:
            self.error(line, "ENDP without PROC")
            return
        body = self.proc_body
        if self.proc == "Reset_Handler":
            self.saw_reset = True
            if body == RESET_WITHOUT_SYSTEMINIT:
                self.calls_system_init = False
            elif body != RESET_WITH_SYSTEMINIT:
                self.diag.error("Reset_Handler does more than call SystemInit and __main", self.path, self.proc_line)
        elif body != LOOP_FOREVER:
            self.diag.error(f"handler {self.proc} contains code other than 'B .'", self.path, self.proc_line)
        self.proc = None

    def finish(self) -> StartupInfo:
        if not self.vectors:
            self.diag.error("no vector table (__Vectors) found", self.path)
        elif self.vectors[0] != "__initial_sp" or len(self.vectors) < 2 or self.vectors[1] != "Reset_Handler":
            self.diag.error("vector table must start with __initial_sp, Reset_Handler", self.path)
        if not self.saw_reset:
            self.diag.error("no Reset_Handler procedure found", self.path)
        if self.conditional_depth:
            self.diag.error("unterminated IF block", self.path)
        return StartupInfo(
            vectors=self.vectors,
            stack_size=self.equs.get("Stack_Size"),
            heap_size=self.equs.get("Heap_Size"),
            calls_system_init=self.calls_system_init,
            weak=self.weak,
        )
