"""Small C source scans. These report textual references, never runtime access."""

from __future__ import annotations

import re

_IGNORED = re.compile(
    r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.DOTALL
)
_FIELD = re.compile(r"\b(\w+)\s*->\s*(\w+)")
_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


def clean_source(text: str) -> str:
    return _IGNORED.sub(lambda match: " " + "\n" * match[0].count("\n"), text)


def function_body(lines: list[str], line: int) -> str | None:
    """Extract balanced braces starting at the DWARF source location."""
    if not 1 <= line <= len(lines):
        return None
    text = clean_source("\n".join(lines[line - 1 :]))
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index]
    return None


def field_references(body: str, parameters: dict[str, str]) -> list[tuple[str, str]]:
    references = set()
    for name, field in _FIELD.findall(body):
        type_name = parameters.get(name, "")
        match = re.fullmatch(r"(?:const\s+)?struct\s+(\w+)\s*\*", type_name)
        if match:
            references.add((f"{match[1]}.{field}", type_name))
    return sorted(references)


def parameter_calls(body: str, parameters: dict[str, str]) -> dict[str, set[int]]:
    """Direct calls passing a caller parameter unchanged, indexed by argument.

    Nested parentheses are balanced. Casts, member expressions, function pointer
    calls, and locals are deliberately excluded; their provenance needs more
    than this textual scan can establish.
    """
    calls: dict[str, set[int]] = {}
    for match in _CALL.finditer(body):
        if match[1] in parameters or match[1] in {
            "if",
            "for",
            "while",
            "switch",
            "sizeof",
            "typeof",
            "__typeof__",
            "return",
        }:
            continue
        prefix = body[: match.start()].rstrip()
        if prefix.endswith(("->", ".")):
            continue
        depth = 0
        begin = match.end()
        arguments = []
        for index in range(begin, len(body)):
            char = body[index]
            if char in "([":
                depth += 1
            elif char == ")" and depth == 0:
                arguments.append(body[begin:index].strip())
                break
            elif char in ")]":
                depth -= 1
            elif char == "," and depth == 0:
                arguments.append(body[begin:index].strip())
                begin = index + 1
        else:
            continue
        for position, argument in enumerate(arguments):
            if argument in parameters and "struct " in parameters[argument]:
                calls.setdefault(match[1], set()).add(position)
    return calls
