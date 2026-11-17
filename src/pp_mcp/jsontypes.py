"""The JSON shape of tool responses.

`JsonValue` is recursive and uses the read-only `Sequence`/`Mapping` for nesting, so a
`list[JsonObject]` can be placed inside a response without copying (`list` is invariant,
`Sequence` is not). At runtime it is a `TypeAliasType`, which keeps the recursion intact for pydantic, which
FastMCP uses to derive each tool's output schema.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, TypeAlias

from typing_extensions import TypeAliasType

if TYPE_CHECKING:
    # Pyright loses track of a string-defined TypeAliasType when it is first reached from inside
    # a recursive function (jsonable); the classic recursive alias does not have that problem.
    JsonValue: TypeAlias = "None | bool | int | float | str | Sequence[JsonValue] | Mapping[str, JsonValue]"
else:
    JsonValue = TypeAliasType(
        "JsonValue",
        "None | bool | int | float | str | Sequence[JsonValue] | Mapping[str, JsonValue]",
    )
JsonObject = dict[str, JsonValue]

__all__ = ["JsonObject", "JsonValue"]
