import tomllib

import pytest

from keil_bridge import armasm_startup, options, scan, textcodec
from keil_bridge.diagnostics import Diagnostics, Level

GBK_CRLF = "/* 中文注释 */\r\nint x;\r\n".encode("gbk")


def test_gbk_crlf_round_trip():
    fmt = textcodec.detect(GBK_CRLF)
    assert (fmt.encoding, fmt.eol, fmt.mixed_eol) == ("gbk", "crlf", False)
    converted = textcodec.to_converted(GBK_CRLF, fmt)
    assert converted == "/* 中文注释 */\nint x;\n".encode()
    assert textcodec.to_original(converted, fmt, "crlf") == (GBK_CRLF, "gbk")


def test_detects_utf8_ascii_and_lf():
    assert textcodec.detect("é\n".encode()).encoding == "utf-8"
    assert textcodec.detect(b"\xef\xbb\xbfx\n").encoding == "utf-8-sig"
    fmt = textcodec.detect(b"int x;\n")
    assert (fmt.encoding, fmt.eol) == ("ascii", "lf")
    assert textcodec.detect(b"a\r\nb\n").mixed_eol


def test_ascii_file_gaining_chinese_is_written_as_gbk():
    fmt = textcodec.TextFormat("ascii", "crlf")
    data, encoding = textcodec.to_original("// 注释\n".encode(), fmt, "crlf")
    assert encoding == "gbk"
    assert data == "// 注释\r\n".encode("gbk")


def test_unencodable_character_is_an_error():
    with pytest.raises(textcodec.EncodingError, match="line 2"):
        textcodec.to_original("a\n😀\n".encode(), textcodec.TextFormat("gbk", "crlf"), "crlf")


def test_binary_is_rejected():
    with pytest.raises(textcodec.EncodingError):
        textcodec.detect(b"\x00\x01\x02")


STARTUP = """\
Stack_Size      EQU     0x00000400
                AREA    STACK, NOINIT, READWRITE, ALIGN=3
Stack_Mem       SPACE   Stack_Size
__initial_sp
Heap_Size       EQU     0x00000200
                AREA    HEAP, NOINIT, READWRITE, ALIGN=3
__heap_base
Heap_Mem        SPACE   Heap_Size
__heap_limit
                PRESERVE8
                THUMB
                AREA    RESET, DATA, READONLY
                EXPORT  __Vectors
__Vectors       DCD     __initial_sp               ; Top of Stack
                DCD     Reset_Handler              ; Reset Handler
                DCD     NMI_Handler
                DCD     0                          ; Reserved
                DCD     USART1_IRQHandler
__Vectors_End
__Vectors_Size  EQU  __Vectors_End - __Vectors
                AREA    |.text|, CODE, READONLY
Reset_Handler    PROC
                 EXPORT  Reset_Handler             [WEAK]
                 IMPORT  __main
                 IMPORT  SystemInit
                 LDR     R0, =SystemInit
                 BLX     R0
                 LDR     R0, =__main
                 BX      R0
                 ENDP
NMI_Handler\\
                PROC
                EXPORT  NMI_Handler                [WEAK]
                B       .
                ENDP
Default_Handler PROC
                EXPORT  USART1_IRQHandler          [WEAK]
USART1_IRQHandler
                B       .
                ENDP
                ALIGN
                 IF      :DEF:__MICROLIB
                 EXPORT  __initial_sp
                 ELSE
__user_initial_stackheap
                 LDR     R0, =  Heap_Mem
                 BX      LR
                 ENDIF
                 END
"""


def test_parses_standard_startup():
    diag = Diagnostics()
    info = armasm_startup.parse(STARTUP, "startup.s", diag)
    assert not diag.items
    assert info is not None
    assert info.vectors == ["__initial_sp", "Reset_Handler", "NMI_Handler", None, "USART1_IRQHandler"]
    assert (info.stack_size, info.heap_size, info.calls_system_init) == (0x400, 0x200, True)
    assert armasm_startup.is_armasm(STARTUP)
    assert not armasm_startup.is_armasm("  .syntax unified\n  .section .text\n")


def test_custom_reset_code_is_an_error_with_line():
    text = STARTUP.replace(
        "                 BLX     R0\n", "                 MOVS    R1, #0\n                 BLX     R0\n"
    )
    diag = Diagnostics()
    assert armasm_startup.parse(text, "startup.s", diag) is None
    errors = [d for d in diag.items if d.level is Level.ERROR]
    assert errors[0].line == text.splitlines().index("Reset_Handler    PROC") + 1


def test_unknown_directive_is_an_error():
    diag = Diagnostics()
    armasm_startup.parse(STARTUP.replace("                PRESERVE8", "                MACRO"), "s.s", diag)
    assert any("MACRO" in d.message for d in diag.items if d.level is Level.ERROR)


def test_parse_set_types_and_unknown_keys():
    diag = Diagnostics()
    parsed = options.parse_set(["c_std=gnu11", "stack_size=0x800", "signed_char=true", "defines_add=A,B", "x=1"], diag)
    assert parsed == {"c_std": "gnu11", "stack_size": 0x800, "signed_char": True, "defines_add": ["A", "B"]}
    assert diag.error_count() == 1


def test_validate_rejects_bad_values():
    diag = Diagnostics()
    valid = options.validate({"c_std": "c++17", "optimize": "-Os", "stack_size": "big"}, diag, "t")
    assert valid == {"optimize": "-Os"}
    assert diag.error_count() == 2


def test_apply_and_toml_round_trip():
    defaults = options.BuildOptions(defines=["A", "B"], c_std="gnu90")
    overrides = {"defines_remove": ["A"], "defines_add": ["C"], "c_std": "gnu99", "heap_size": 0}
    result = options.apply(defaults, overrides)
    assert (result.defines, result.c_std, result.heap_size) == (["B", "C"], "gnu99", 0)
    assert tomllib.loads(options.dump_toml(overrides)) == overrides
    assert options.overridden_options(overrides) == {"defines", "c_std", "heap_size"}


def test_includes_ignore_comments():
    text = '#include "a.h"\n// #include "b.h"\n/* #include "c.h" */\n  #  include <d.h>\n'
    assert [(i.line, i.name, i.angle) for i in scan.includes(text)] == [(1, "a.h", False), (4, "d.h", True)]


def test_armcc_constructs_and_fputc():
    source = '# 1 "src/a.c"\nint fputc(int c, FILE *f) { return c; }\n__asm void f(void) {}\n__asm volatile ("nop");\n'
    tokens = scan._tokens(source)
    assert scan._defines_fputc(tokens)
    assert not scan._defines_fputc(scan._tokens("int fputc(int c, FILE *f);\nfputc(1, f);\n"))
    found = [(t.line, message) for t, _, message in scan._armcc_constructs(tokens, Diagnostics())]
    assert [line for line, _ in found] == [2]
