# Standard library imports
from logging import Filter, getLogger
from typing import TYPE_CHECKING, override

logger = getLogger(__name__)

# First party imports

if TYPE_CHECKING:
  # First party imports
  from aeth_ext.central_log_server.protocol import TaggedLogRecord


class NotFilter(Filter):
  @override
  def filter(self, record: TaggedLogRecord) -> bool:  # pyright: ignore[reportIncompatibleMethodOverride]
    logger.info("Filtering record: %s", record)
    if self.nlen == 0:
      return True
    elif self.name == record.name:
      return False
    elif record.name.find(self.name, 0, self.nlen) != 0:
      return True
    return record.name[self.nlen] != "."
