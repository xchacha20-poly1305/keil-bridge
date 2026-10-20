"""Source text encoding and line-ending conversion, in both directions.

Converted files are always UTF-8 with LF. The original encoding and line
ending of each file are recorded so that edits can be written back exactly.
"""

from dataclasses import dataclass

# Chinese Windows (and therefore most Keil projects) uses code page 936.
LEGACY_ENCODING = "gbk"


class EncodingError(Exception):
    pass


@dataclass
class TextFormat:
    encoding: str  # "ascii", "utf-8", "utf-8-sig" or LEGACY_ENCODING
    eol: str  # "crlf", "lf" or "none" (no line break in the file)
    mixed_eol: bool = False


def detect(data: bytes) -> TextFormat:
    return TextFormat(_detect_encoding(data), *_detect_eol(data))


def _detect_encoding(data: bytes) -> str:
    if b"\0" in data:
        raise EncodingError("contains NUL bytes, looks like a binary file")
    if data.isascii():
        return "ascii"
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode(LEGACY_ENCODING)
    except UnicodeDecodeError as e:
        raise EncodingError(f"neither UTF-8 nor {LEGACY_ENCODING.upper()} (byte offset {e.start})") from None
    if text.encode(LEGACY_ENCODING) != data:
        raise EncodingError(f"{LEGACY_ENCODING.upper()} decoding does not round-trip")
    return LEGACY_ENCODING


def _detect_eol(data: bytes) -> tuple[str, bool]:
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    if crlf == 0 and lf == 0:
        return "none", False
    return ("crlf" if crlf >= lf else "lf"), (crlf > 0 and lf > 0)


def to_converted(data: bytes, fmt: TextFormat) -> bytes:
    """Original bytes -> UTF-8 + LF."""
    text = data.decode(fmt.encoding)
    return text.replace("\r\n", "\n").encode("utf-8")


def to_original(data: bytes, fmt: TextFormat, default_eol: str) -> tuple[bytes, str]:
    """UTF-8 + LF bytes -> bytes in the original format.

    Returns the bytes and the encoding actually used, which differs from
    fmt.encoding when an ASCII file gained non-ASCII characters.
    """
    text = data.decode("utf-8")
    eol = fmt.eol if fmt.eol != "none" else default_eol
    if eol == "crlf":
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    encoding = fmt.encoding
    if encoding == "ascii" and not text.isascii():
        encoding = LEGACY_ENCODING
    try:
        return text.encode(encoding), encoding
    except UnicodeEncodeError as e:
        line = text.count("\n", 0, e.start) + 1
        raise EncodingError(f"line {line}: {text[e.start : e.end]!r} cannot be encoded as {encoding.upper()}") from None
