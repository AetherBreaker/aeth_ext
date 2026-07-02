# ruff: noqa: TC003
# Standard library imports
from collections.abc import Sequence
from email.headerregistry import Address
from enum import StrEnum as _StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, override

if TYPE_CHECKING:
  # Standard library imports
  from typing import Any


__all__ = [
  "AddressLike",
  "EmailMessageParts",
  "StrEnum",
]


class StrEnum(_StrEnum):
  """
  Custom string enum that returns the member name as the value.
  """

  @override
  @staticmethod
  def _generate_next_value_(name: str, start: int, count: int, last_values: list[Any]) -> Any:
    """
    Return the member name.
    """
    return name


type AddressLike = str | Address | tuple[str, str | None, str | None, str | None]


class EmailMessageParts(TypedDict):
  subject: str
  body: str
  from_addr: AddressLike
  to_addrs: Sequence[AddressLike] | AddressLike
  cc_addrs: NotRequired[Sequence[AddressLike] | AddressLike]
  bcc_addrs: NotRequired[Sequence[AddressLike] | AddressLike]
  attachments: NotRequired[Sequence[Path] | Path]


class IsPydantic: ...


class IsPydanticSlots:
  __slots__ = ()
