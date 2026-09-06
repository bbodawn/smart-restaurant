"""record_no 生成契约（Phase 6-B-1 v2.1 冻结：无 MAX+1/无计数器，由 item 主键派生）。"""
from datetime import date

from app.services.inventory import compose_inbound_record_no


def test_record_no_format_and_uniqueness_by_item_id():
    assert compose_inbound_record_no(date(2026, 9, 6), 23) == "INBOUND-20260906-00000023"
    # 不同 item id → 唯一；同 date 不同 item 不冲突
    assert compose_inbound_record_no(date(2026, 9, 6), 24) == "INBOUND-20260906-00000024"
    assert compose_inbound_record_no(date(2026, 9, 6), 23) != compose_inbound_record_no(date(2026, 9, 6), 24)
    # 不同日期同 item → 前缀不同但仍唯一（item 1:1 保证不会同 item 跨日重复）
    assert compose_inbound_record_no(date(2026, 9, 7), 23) == "INBOUND-20260907-00000023"


def test_record_no_leading_zero_padding():
    assert compose_inbound_record_no(date(2026, 9, 6), 1) == "INBOUND-20260906-00000001"
    assert compose_inbound_record_no(date(2026, 9, 6), 12345678) == "INBOUND-20260906-12345678"
