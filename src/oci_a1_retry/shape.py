"""How much A1 capacity a stack asks for, read from its Terraform configuration (doctor).

Since 15 June 2026, Always Free covers 1,500 OCPU hours and 9,000 GB hours of A1 per month:
2 OCPUs and 12 GB of memory in total across all A1 instances of a tenancy (it was 4 and 24).
A stack saved before that can still ask for 4 OCPUs and 24 GB. A free account's Apply then
fails with a service limit error; a Pay As You Go account gets the VM and pays for the extra.

The parser is deliberately small: it understands the configuration the OCI Console's
"Save as stack" writes (oci_core_instance resources with literal values or simple var.x
references). Anything it cannot read with confidence raises SizeUnknown instead of guessing.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass

FREE_A1_OCPUS = 2
FREE_A1_MEMORY_GB = 12
A1_SHAPE = "VM.Standard.A1.Flex"
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024

_INSTANCE = re.compile(r'resource\s+"oci_core_instance"\s+"[^"]*"\s*\{')
_VARIABLE = re.compile(r'variable\s+"([^"]+)"\s*\{')
_SHAPE_CONFIG = re.compile(r"(?m)^\s*shape_config\s*\{")
_HEREDOC = re.compile(r"<<-?\s*([A-Za-z_][A-Za-z0-9_]*)[^\n]*\n")


class SizeUnknown(Exception):
    """The configuration could not be read with confidence; the message says why."""


@dataclass(frozen=True)
class A1Size:
    ocpus: float
    memory_gb: float
    instances: int

    @property
    def within_free_tier(self) -> bool:
        return self.ocpus <= FREE_A1_OCPUS and self.memory_gb <= FREE_A1_MEMORY_GB


def a1_size(archive: bytes, variables: Mapping[str, str] | None = None) -> A1Size | None:
    """Total OCPUs and memory of the A1 instances in a stack's Terraform zip.

    None when the configuration has no VM.Standard.A1.Flex instance at all.
    """
    sources = _tf_sources(archive)
    text = "\n".join(sources)
    values = dict(_variable_defaults(text))
    values.update({name: str(value) for name, value in (variables or {}).items()})

    ocpus = memory = 0.0
    instances = 0
    for body in _blocks(text, _INSTANCE):
        shape = _attribute(body, "shape")
        if shape is None or _resolve(shape, values, "shape") != A1_SHAPE:
            continue
        if _attribute(body, "count") is not None or _attribute(body, "for_each") is not None:
            raise SizeUnknown("an A1 instance uses count or for_each")
        configs = _blocks(body, _SHAPE_CONFIG)
        if len(configs) != 1:
            raise SizeUnknown("an A1 instance has no shape_config block")
        config = configs[0]
        ocpus += _number(_required(config, "ocpus"), values, "ocpus")
        memory += _number(_required(config, "memory_in_gbs"), values, "memory_in_gbs")
        instances += 1
    if instances == 0:
        return None
    return A1Size(ocpus=ocpus, memory_gb=memory, instances=instances)


def format_amount(value: float) -> str:
    return f"{value:g}"


# -- reading the archive ---------------------------------------------------------


def _tf_sources(archive: bytes) -> list[str]:
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise SizeUnknown("the Terraform configuration is larger than 20 MB")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            names = [name for name in bundle.namelist() if name.endswith(".tf")]
            sources = [
                _strip_comments(bundle.read(name).decode("utf-8", "replace")) for name in names
            ]
    except (zipfile.BadZipFile, OSError) as exc:
        raise SizeUnknown("the Terraform configuration is not a readable zip file") from exc
    if not sources:
        raise SizeUnknown("the Terraform configuration has no .tf files")
    return sources


def _strip_comments(text: str) -> str:
    """Remove # and // line comments and /* */ blocks, leaving strings and heredocs alone."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == '"':
            end = _string_end(text, i)
            out.append(text[i:end])
            i = end
        elif text.startswith("<<", i) and (match := _HEREDOC.match(text, i)):
            end = _heredoc_end(text, match)
            out.append(text[i:end])
            i = end
        elif char == "#" or text.startswith("//", i):
            newline = text.find("\n", i)
            i = n if newline == -1 else newline
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = n if close == -1 else close + 2
        else:
            out.append(char)
            i += 1
    return "".join(out)


# -- blocks and attributes -----------------------------------------------------


def _blocks(text: str, opener: re.Pattern[str]) -> list[str]:
    """The bodies of the blocks that start with ``opener`` (top level of ``text`` only)."""
    bodies: list[str] = []
    position = 0
    while match := opener.search(text, position):
        end = _matching_brace(text, match.end())
        bodies.append(text[match.end() : end])
        position = end + 1
    return bodies


def _matching_brace(text: str, start: int) -> int:
    """Index of the "}" that closes the block whose "{" is just before ``start``."""
    depth = 1
    i, n = start, len(text)
    while i < n:
        char = text[i]
        if char == '"':
            i = _string_end(text, i)
            continue
        if text.startswith("<<", i) and (match := _HEREDOC.match(text, i)):
            i = _heredoc_end(text, match)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise SizeUnknown("the Terraform configuration has an unclosed block")


def _string_end(text: str, start: int) -> int:
    """Index just after the string that starts with the quote at ``start``."""
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    return len(text)


def _heredoc_end(text: str, match: re.Match[str]) -> int:
    marker = re.compile(rf"(?m)^\s*{re.escape(match.group(1))}\s*$")
    found = marker.search(text, match.end())
    return len(text) if found is None else found.end()


def _attribute(body: str, name: str) -> str | None:
    """The raw value of ``name = value`` at the top level of a block body."""
    depth = 0
    i, n = 0, len(body)
    line_start = True
    while i < n:
        char = body[i]
        if char == '"':
            i = _string_end(body, i)
            line_start = False
            continue
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        elif char == "\n":
            line_start = True
            i += 1
            continue
        elif depth == 0 and line_start and not char.isspace():
            match = re.compile(rf"{re.escape(name)}\s*=\s*([^\n]+)").match(body, i)
            if match:
                return match.group(1).strip()
            line_start = False
        i += 1
    return None


def _required(body: str, name: str) -> str:
    value = _attribute(body, name)
    if value is None:
        raise SizeUnknown(f"a shape_config has no {name}")
    return value


def _variable_defaults(text: str) -> list[tuple[str, str]]:
    defaults = []
    for match in _VARIABLE.finditer(text):
        body = text[match.end() : _matching_brace(text, match.end())]
        value = _attribute(body, "default")
        if value is not None:
            defaults.append((match.group(1), value))
    return defaults


def _resolve(raw: str, values: Mapping[str, str], what: str, depth: int = 0) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        inner = raw[1:-1]
        if "${" not in inner:
            return inner
        raw = (
            inner.removeprefix("${").removesuffix("}")
            if re.fullmatch(r"\$\{[^}]*\}", inner)
            else raw
        )
    reference = re.fullmatch(r"var\.([A-Za-z_][A-Za-z0-9_-]*)", raw)
    if reference and depth < 5:
        name = reference.group(1)
        if name not in values:
            raise SizeUnknown(f"{what} comes from variable '{name}', which has no value")
        return _resolve(values[name], values, what, depth + 1)
    if re.fullmatch(r"-?\d+(\.\d+)?", raw) or raw.startswith("VM."):
        return raw
    raise SizeUnknown(f"{what} is an expression this check cannot evaluate")


def _number(raw: str, values: Mapping[str, str], what: str) -> float:
    resolved = _resolve(raw, values, what)
    try:
        return float(resolved)
    except ValueError as exc:
        raise SizeUnknown(f"{what} is not a number") from exc
