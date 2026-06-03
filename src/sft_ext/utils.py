from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  from zoneinfo import ZoneInfo

  type IntOrInf = int | float

shift = timedelta()


def today(tzinfo: ZoneInfo | None = None):
  """
  Returns a :py:class:`datetime` representing the current day at midnight

  :param tzinfo:
      The time zone to attach (also used to determine the current day).

  :return:
      A :py:class:`datetime.datetime` object representing the current day
      at midnight.
  """
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
  from dateutil.relativedelta import SA, relativedelta

  now = get_now(tzinfo=tzinfo) if dt is None else dt
  return now + relativedelta(weekday=SA(-1))


def get_next_sat(dt: datetime | None = None, tzinfo: ZoneInfo | None = None):
  from dateutil.relativedelta import SA, relativedelta

  now = get_now(tzinfo=tzinfo) if dt is None else dt
  return now + relativedelta(weekday=SA(+1))
