# Standard library imports
from datetime import datetime, timedelta
from pathlib import Path
from sys import modules
from typing import TYPE_CHECKING, cast

# First party imports
from sft_ext.const_parsing import parse_and_grab_constants

if TYPE_CHECKING:
  # Standard library imports
  from zoneinfo import ZoneInfo

  type IntOrInf = int | float


expected_consts = parse_and_grab_constants(
  Path(cast("str", modules["__main__"].__file__)), {"SHIFT": "shift"}, {"timedelta": timedelta}
)

shift = expected_consts.get("shift", timedelta())


def today(tzinfo: ZoneInfo | None = None):
  """
  Returns a :py:class:`datetime` representing the current day at midnight

  :param tzinfo:
      The time zone to attach (also used to determine the current day).

  :return:
      A :py:class:`datetime.datetime` object representing the current day
      at midnight.
  """
  # Third party imports
  from dateutil.utils import today as _today

  result = _today(tzinfo=tzinfo)

  result += shift

  return result


def get_now(tzinfo: ZoneInfo | None = None):
  """
  Returns a :py:class:`datetime` representing the current date and time

  :param tzinfo:
      The time zone to attach (also used to determine the current date and time).

  :return:
      A :py:class:`datetime.datetime` object representing the current date and time.
  """

  result = datetime.now(tz=tzinfo)

  result += shift

  return result


def get_last_sat(dt: datetime | None = None, tzinfo: ZoneInfo | None = None):
  # Third party imports
  from dateutil.relativedelta import SA, relativedelta

  now = get_now(tzinfo=tzinfo) if dt is None else dt
  return now + relativedelta(weekday=SA(-1))


def get_next_sat(dt: datetime | None = None, tzinfo: ZoneInfo | None = None):
  # Third party imports
  from dateutil.relativedelta import SA, relativedelta

  now = get_now(tzinfo=tzinfo) if dt is None else dt
  return now + relativedelta(weekday=SA(+1))
