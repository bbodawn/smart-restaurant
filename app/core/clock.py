from datetime import date, timedelta
from typing import Optional

_virtual_date: Optional[date] = None

def get_current_date() -> date:
    global _virtual_date
    if _virtual_date is None:
        _virtual_date = date.today()
    return _virtual_date

def set_virtual_date(new_date: date) -> date:
    global _virtual_date
    _virtual_date = new_date
    return _virtual_date

def advance_virtual_days(days: int = 1) -> date:
    global _virtual_date
    current = get_current_date()
    _virtual_date = current + timedelta(days=days)
    return _virtual_date

def reset_virtual_clock() -> date:
    global _virtual_date
    _virtual_date = date.today()
    return _virtual_date
