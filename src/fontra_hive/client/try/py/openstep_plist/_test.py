"""The helpers openstep_plist's own tests use (from its _test.pyx)."""

from .parser import ParseInfo, _tounicode
from .parser import advance_to_non_space as _advance_to_non_space
from .parser import get_slashed_char as _get_slashed_char
from .parser import is_valid_unquoted_string_char as _is_valid
from .parser import line_number_strings as _line_number_strings
from .parser import parse_plist_string as _parse_plist_string
from .parser import parse_unquoted_plist_string as _parse_unquoted_plist_string


def is_valid_unquoted_string_char(c):
    return _is_valid(chr(c) if isinstance(c, int) else c)


def line_number_strings(s, offset=0):
    return _line_number_strings(ParseInfo(_tounicode(s), offset))


def advance_to_non_space(s, offset=0):
    pi = ParseInfo(_tounicode(s), offset)
    return s[pi.curr] if _advance_to_non_space(pi) else None


def get_slashed_char(s, offset=0):
    return _get_slashed_char(ParseInfo(_tounicode(s), offset))


def parse_unquoted_plist_string(s):
    return _parse_unquoted_plist_string(ParseInfo(_tounicode(s)))


def parse_plist_string(s, required=True):
    return _parse_plist_string(ParseInfo(_tounicode(s)), required=required)
