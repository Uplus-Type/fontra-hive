"""Pure-Python port of openstep_plist's parser.pyx (MIT). See __init__.py."""

from __future__ import annotations

import re


class ParseError(Exception):
    pass


UNQUOTED = re.compile(r"[A-Za-z0-9_$/:.\-]+")
_SPACE = "\t\n\x0b\x0c\r   "
# Whitespace and comments, as advance_to_non_space skips them.
SKIP = re.compile(
    r"(?:[\t-\r   ]+|//[^\n\r  ]*|/\*.*?(?:\*/|\Z))*", re.DOTALL
)
QUOTED_RUN = {
    '"': re.compile(r'[^"\\]*'),
    "'": re.compile(r"[^'\\]*"),
}

# NextStep encoding 0x80-0xFF (octal escapes in quoted strings).
NEXT_STEP_DECODING_TABLE = [
    0xA0, 0xC0, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC7, 0xC8, 0xC9,
    0xCA, 0xCB, 0xCC, 0xCD, 0xCE, 0xCF, 0xD0, 0xD1, 0xD2, 0xD3,
    0xD4, 0xD5, 0xD6, 0xD9, 0xDA, 0xDB, 0xDC, 0xDD, 0xDE, 0xB5,
    0xD7, 0xF7, 0xA9, 0xA1, 0xA2, 0xA3, 0x2044, 0xA5, 0x192, 0xA7,
    0xA4, 0x2019, 0x201C, 0xAB, 0x2039, 0x203A, 0xFB01, 0xFB02, 0xAE, 0x2013,
    0x2020, 0x2021, 0xB7, 0xA6, 0xB6, 0x2022, 0x201A, 0x201E, 0x201D, 0xBB,
    0x2026, 0x2030, 0xAC, 0xBF, 0xB9, 0x2CB, 0xB4, 0x2C6, 0x2DC, 0xAF,
    0x2D8, 0x2D9, 0xA8, 0xB2, 0x2DA, 0xB8, 0xB3, 0x2DD, 0x2DB, 0x2C7,
    0x2014, 0xB1, 0xBC, 0xBD, 0xBE, 0xE0, 0xE1, 0xE2, 0xE3, 0xE4,
    0xE5, 0xE7, 0xE8, 0xE9, 0xEA, 0xEB, 0xEC, 0xC6, 0xED, 0xAA,
    0xEE, 0xEF, 0xF0, 0xF1, 0x141, 0xD8, 0x152, 0xBA, 0xF2, 0xF3,
    0xF4, 0xF5, 0xF6, 0xE6, 0xF9, 0xFA, 0xFB, 0x131, 0xFC, 0xFD,
    0x142, 0xF8, 0x153, 0xDF, 0xFE, 0xFF, 0xFFFD, 0xFFFD,
]  # fmt: skip

SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    '"': '"',
    "\n": "\n",
}

UNQUOTED_STRING, UNQUOTED_INTEGER, UNQUOTED_FLOAT = 0, 1, 2


class ParseInfo:
    __slots__ = ("s", "curr", "end", "dict_type", "use_numbers")

    def __init__(self, s, curr=0, dict_type=dict, use_numbers=False):
        self.s = s
        self.curr = curr
        self.end = len(s)
        self.dict_type = dict_type
        self.use_numbers = use_numbers

    def char(self) -> str:
        """The character at curr, or "\\0" past the end (the C buffer's NUL)."""
        return self.s[self.curr] if self.curr < self.end else "\0"


def is_valid_unquoted_string_char(ch: str) -> bool:
    return (
        ("a" <= ch <= "z")
        or ("A" <= ch <= "Z")
        or ("0" <= ch <= "9")
        or ch in "_$/:.-"
    ) and ch != ""


def line_number_strings(pi: ParseInfo) -> int:
    count = 1
    p = 0
    s = pi.s
    while p < pi.curr:
        c = s[p]
        if c == "\r":
            count += 1
            if p + 1 < len(s) and s[p + 1] == "\n":
                p += 1
        elif c == "\n":
            count += 1
        p += 1
    return count


def advance_to_non_space(pi: ParseInfo) -> bool:
    """True if something that is not whitespace is found before the end."""
    pi.curr = SKIP.match(pi.s, pi.curr).end()
    return pi.curr < pi.end


def get_slashed_char(pi: ParseInfo) -> str:
    s = pi.s
    ch = pi.char()
    pi.curr += 1
    if "0" <= ch <= "7":
        num = ord(ch) - 48
        ch = pi.char()
        if "0" <= ch <= "7":
            pi.curr += 1
            num = (num << 3) + ord(ch) - 48
            if pi.curr < pi.end:
                ch = s[pi.curr]
                if "0" <= ch <= "7":
                    pi.curr += 1
                    num = (num << 3) + ord(ch) - 48
            num &= 0xFF  # uint8_t in the original
            return chr(num if num < 128 else NEXT_STEP_DECODING_TABLE[num - 128])
        # a single octal digit: the digit itself, as in the original
        return chr(ord("0") + num)
    elif ch == "U":
        unum = 0
        digits = 4
        while pi.curr < pi.end and digits > 0:
            c = s[pi.curr]
            if c in "0123456789abcdefABCDEF":
                pi.curr += 1
                unum = (unum << 4) + int(c, 16)
            digits -= 1
        return chr(unum)
    return SIMPLE_ESCAPES.get(ch, ch)


def _is_high_surrogate(c: str) -> bool:
    return 0xD800 <= ord(c) <= 0xDBFF


def _is_low_surrogate(c: str) -> bool:
    return 0xDC00 <= ord(c) <= 0xDFFF


def parse_quoted_plist_string(pi: ParseInfo, quote: str) -> str:
    s = pi.s
    run = QUOTED_RUN[quote]
    parts = []
    while True:
        m = run.match(s, pi.curr)
        parts.append(m.group())
        pi.curr = m.end()
        if pi.curr >= pi.end:
            raise ParseError(
                "Unterminated quoted string starting on line %d"
                % line_number_strings(pi)
            )
        if s[pi.curr] == quote:
            pi.curr += 1
            return "".join(parts)
        # a backslash
        pi.curr += 1
        ch = get_slashed_char(pi)
        if _is_high_surrogate(ch) and pi.curr < pi.end and s[pi.curr] == "\\":
            mark = pi.curr
            pi.curr += 1
            ch2 = get_slashed_char(pi)
            if _is_low_surrogate(ch2):
                ch = chr(
                    (ord(ch) - 0xD800) * 0x400 + ord(ch2) - 0xDC00 + 0x10000
                )
            else:
                pi.curr = mark
        parts.append(ch)


def get_unquoted_string_type(s: str) -> int:
    """A number starts with a digit, or "-" and a digit; a float has one ".".
    (1e-5 or .05 are strings, as in the original.)"""
    if not s:
        return UNQUOTED_STRING
    i = 0
    ch = s[0]
    if ch == "-":
        if len(s) > 1:
            i = 1
            ch = s[1]
            if not ("0" <= ch <= "9"):
                return UNQUOTED_STRING
        else:
            return UNQUOTED_STRING
    elif not ("0" <= ch <= "9"):
        return UNQUOTED_STRING
    is_float = False
    for ch in s[i:]:
        if ch > "9" or ch < "." or ch == "/":
            return UNQUOTED_STRING
        elif ch == ".":
            if is_float:
                return UNQUOTED_STRING
            is_float = True
    return UNQUOTED_FLOAT if is_float else UNQUOTED_INTEGER


def string_to_number(s: str, required: bool = True):
    kind = get_unquoted_string_type(s) if s else UNQUOTED_STRING
    if kind == UNQUOTED_FLOAT:
        return float(s)
    if kind == UNQUOTED_INTEGER:
        return int(s)
    if required:
        raise ValueError(f"Could not convert string to float or int: {s!r}")
    return s


def parse_unquoted_plist_string(pi: ParseInfo, ensure_string: bool = False):
    m = UNQUOTED.match(pi.s, pi.curr)
    if m is None or m.end() == pi.curr:
        raise ParseError("Unexpected EOF")
    pi.curr = m.end()
    s = m.group()
    if not ensure_string and pi.use_numbers:
        kind = get_unquoted_string_type(s)
        if kind == UNQUOTED_FLOAT:
            return float(s)
        if kind == UNQUOTED_INTEGER:
            return int(s)
    return s


def parse_plist_string(pi: ParseInfo, required: bool = True):
    if not advance_to_non_space(pi):
        if required:
            raise ParseError("Unexpected EOF while parsing string")
    ch = pi.char()
    if ch == "'" or ch == '"':
        pi.curr += 1
        return parse_quoted_plist_string(pi, ch)
    if is_valid_unquoted_string_char(ch):
        return parse_unquoted_plist_string(pi, ensure_string=True)
    if required:
        raise ParseError(
            "Invalid string character at line %d: %r" % (line_number_strings(pi), ch)
        )
    return None


def parse_plist_array(pi: ParseInfo) -> list:
    result = []
    tmp = parse_plist_object(pi, required=False)
    while tmp is not None:
        result.append(tmp)
        if not advance_to_non_space(pi):
            raise ParseError(
                "Missing ',' for array at line %d" % line_number_strings(pi)
            )
        if pi.s[pi.curr] != ",":
            tmp = None
        else:
            pi.curr += 1
            tmp = parse_plist_object(pi, required=False)
    if not advance_to_non_space(pi) or pi.s[pi.curr] != ")":
        raise ParseError(
            "Expected terminating ')' for array at line %d" % line_number_strings(pi)
        )
    pi.curr += 1
    return result


def parse_plist_dict_content(pi: ParseInfo):
    result = pi.dict_type()
    key = parse_plist_string(pi, required=False)
    while key is not None:
        if not advance_to_non_space(pi):
            raise ParseError("Missing ';' on line %d" % line_number_strings(pi))
        ch = pi.s[pi.curr]
        if ch == ";":
            value = key  # a "strings resource" shortcut
        elif ch == "=":
            pi.curr += 1
            value = parse_plist_object(pi, required=True)
        else:
            raise ParseError(
                "Unexpected character after key at line %d: %r"
                % (line_number_strings(pi), ch)
            )
        result[key] = value
        key = None
        if advance_to_non_space(pi) and pi.s[pi.curr] == ";":
            pi.curr += 1
            key = parse_plist_string(pi, required=False)
        else:
            raise ParseError("Missing ';' on line %d" % line_number_strings(pi))
    return result


def parse_plist_dict(pi: ParseInfo):
    result = parse_plist_dict_content(pi)
    if not advance_to_non_space(pi) or pi.s[pi.curr] != "}":
        raise ParseError(
            "Expected terminating '}' for dictionary at line %d"
            % line_number_strings(pi)
        )
    pi.curr += 1
    return result


def _hex(ch: str) -> int:
    if "0" <= ch <= "9":
        return ord(ch) - 48
    if "a" <= ch <= "f":
        return ord(ch) - 87
    if "A" <= ch <= "F":
        return ord(ch) - 55
    return 0xFF


def parse_plist_data(pi: ParseInfo) -> bytes:
    s = pi.s
    data = bytearray()
    while pi.curr < pi.end:
        ch1 = s[pi.curr]
        if ch1 == ">":
            break
        first = _hex(ch1)
        if first != 0xFF:
            pi.curr += 1
            if pi.curr >= pi.end or s[pi.curr] == ">":
                raise ParseError(
                    "Malformed data byte group at line %d: uneven length"
                    % line_number_strings(pi)
                )
            ch2 = s[pi.curr]
            second = _hex(ch2)
            if second == 0xFF:
                raise ParseError(
                    "Malformed data byte group at line %d: invalid hex digit: %r"
                    % (line_number_strings(pi), ch2)
                )
            data.append((first << 4) + second)
            pi.curr += 1
        elif ch1 in " \n\t\r\u2028\u2029":
            pi.curr += 1
        else:
            raise ParseError(
                "Malformed data byte group at line %d: invalid hex digit: %r"
                % (line_number_strings(pi), ch1)
            )
    if pi.char() == ">":
        pi.curr += 1
        return bytes(data)
    raise ParseError(
        "Expected terminating '>' for data at line %d" % line_number_strings(pi)
    )


def parse_plist_object(pi: ParseInfo, required: bool = True):
    if not advance_to_non_space(pi):
        if required:
            raise ParseError("Unexpected EOF while parsing plist")
    ch = pi.char()
    pi.curr += 1
    if ch == "{":
        return parse_plist_dict(pi)
    if ch == "(":
        return parse_plist_array(pi)
    if ch == "<":
        return parse_plist_data(pi)
    if ch == "'" or ch == '"':
        return parse_quoted_plist_string(pi, ch)
    if is_valid_unquoted_string_char(ch):
        pi.curr -= 1
        return parse_unquoted_plist_string(pi)
    pi.curr -= 1
    if required:
        raise ParseError(
            "Unexpected character at line %d: %r" % (line_number_strings(pi), ch)
        )
    return None


def _tounicode(string) -> str:
    if isinstance(string, str):
        return string
    raise TypeError(f"Could not convert to unicode: {string!r}")


def loads(string, dict_type=dict, use_numbers=False):
    s = _tounicode(string)
    pi = ParseInfo(s, 0, dict_type, use_numbers)
    begin = pi.curr
    if not advance_to_non_space(pi):
        return {}
    result = parse_plist_object(pi, required=True)
    if result:
        if advance_to_non_space(pi):
            if not isinstance(result, str):
                raise ParseError(
                    "Junk after plist at line %d" % line_number_strings(pi)
                )
            pi.curr = begin
            result = parse_plist_dict_content(pi)
    return result


def load(fp, dict_type=dict, use_numbers=False):
    return loads(fp.read(), dict_type=dict_type, use_numbers=use_numbers)
