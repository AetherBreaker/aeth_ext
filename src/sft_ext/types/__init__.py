# Standard library imports
from enum import StrEnum as _StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  # Standard library imports
  from typing import Any


class StrEnum(_StrEnum):
  """
  Custom string enum that returns the member name as the value.
  """

  @staticmethod
  def _generate_next_value_(name: str, start: int, count: int, last_values: list) -> Any:
    """
    Return the member name.
    """
    return name
