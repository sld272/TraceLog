from __future__ import annotations

import unittest
from datetime import datetime

from core.time_normalizer import TimeAnnotation, annotation_note, extract


# 主锚点：2026-07-13 周一。周五锚点 2026-07-17，跨年锚点 2026-12-30（周三）。
MON = datetime(2026, 7, 13, 10, 30)
FRI = datetime(2026, 7, 17, 10, 30)
EOY = datetime(2026, 12, 30, 10, 30)


def _first(text: str, anchor: datetime) -> TimeAnnotation:
    anns = extract(text, anchor=anchor)
    assert anns, f"expected at least one annotation for {text!r}"
    return anns[0]


class ExtractMatrixTest(unittest.TestCase):
    """精确日期矩阵，anchor=2026-07-13（周一）。"""

    def test_relative_day_words(self) -> None:
        self.assertEqual("2026-07-14", _first("明天交作业", MON).date)
        self.assertEqual("2026-07-14", _first("明早八点", MON).date)
        self.assertEqual("2026-07-14", _first("明晚见", MON).date)
        self.assertEqual("2026-07-15", _first("后天出发", MON).date)
        self.assertEqual("2026-07-16", _first("大后天到", MON).date)
        self.assertEqual("2026-07-13", _first("今天很累", MON).date)
        self.assertEqual("2026-07-13", _first("今晚加班", MON).date)
        self.assertEqual("2026-07-12", _first("昨天下雨", MON).date)
        self.assertEqual("2026-07-11", _first("前天开会", MON).date)

    def test_this_week(self) -> None:
        self.assertEqual("2026-07-15", _first("这周三要交报告", MON).date)
        self.assertEqual("2026-07-15", _first("本周三", MON).date)
        self.assertEqual("2026-07-17", _first("这周五", MON).date)
        self.assertFalse(_first("这周三", MON).ambiguous)

    def test_next_week_is_ambiguous_with_alternative(self) -> None:
        ann = _first("下周三要交报告", MON)
        self.assertEqual("2026-07-22", ann.date)          # 标准读法＝下一个 ISO 周的周三
        self.assertEqual("周三", ann.weekday_label)
        self.assertTrue(ann.ambiguous)
        self.assertEqual("2026-07-15", ann.alternative)   # 口语中的“即将到来的周三”

    def test_next_next_week(self) -> None:
        self.assertEqual("2026-07-29", _first("下下周三", MON).date)

    def test_bare_weekday_upcoming(self) -> None:
        self.assertEqual("2026-07-17", _first("周五前搞定", MON).date)
        self.assertFalse(_first("周五", MON).ambiguous)
        # 今天是周一 → 裸“周一”指当天
        ann = _first("周一开会", MON)
        self.assertEqual("2026-07-13", ann.date)
        self.assertFalse(ann.ambiguous)
        # 周日/周天 同义
        self.assertEqual("2026-07-19", _first("周日", MON).date)
        self.assertEqual("2026-07-19", _first("周天", MON).date)

    def test_month_day_absolute(self) -> None:
        self.assertEqual("2026-07-20", _first("7月20日", MON).date)
        self.assertEqual("2026-07-20", _first("7月20号", MON).date)

    def test_month_day_rolls_into_next_year_when_current_year_passed(self) -> None:
        self.assertEqual("2027-01-05", _first("1月5日交报告", EOY).date)

    def test_days_later(self) -> None:
        self.assertEqual("2026-07-16", _first("3天后", MON).date)
        self.assertEqual("2026-07-16", _first("三天之后", MON).date)
        self.assertEqual("2026-07-23", _first("十天后", MON).date)

    def test_month_parts_are_ambiguous(self) -> None:
        eom = _first("月底前交", MON)
        self.assertEqual("2026-07-31", eom.date)
        self.assertEqual("周五", eom.weekday_label)
        self.assertTrue(eom.ambiguous)
        self.assertIsNone(eom.alternative)
        self.assertEqual("2026-07-01", _first("月初", MON).date)
        self.assertEqual("2026-07-15", _first("月中", MON).date)


class OtherAnchorsTest(unittest.TestCase):
    def test_bare_weekday_rolls_into_next_week_from_friday(self) -> None:
        # anchor 周五（2026-07-17）：本周三已过 → 裸“周三”指下一个周三 07-22
        self.assertEqual("2026-07-22", _first("周三", FRI).date)

    def test_next_week_across_year_boundary(self) -> None:
        # anchor 2026-12-30（周三）：下周三 → 2027-01-06
        self.assertEqual("2027-01-06", _first("下周三", EOY).date)


class NegativeGuardTest(unittest.TestCase):
    """误匹配防线：宁缺勿错。"""

    def test_past_and_periodic_and_weekend_are_not_annotated(self) -> None:
        self.assertEqual([], extract("上周三我就说过", anchor=MON))   # 过去：识别但不标注
        self.assertEqual([], extract("上上周五", anchor=MON))
        self.assertEqual([], extract("每周三例会", anchor=MON))       # 周期：不标注
        self.assertEqual([], extract("这周末去爬山", anchor=MON))     # 周末不解析
        self.assertEqual([], extract("三天两头往外跑", anchor=MON))   # 无具体天数
        self.assertEqual([], extract("下周再说吧", anchor=MON))       # 无具体周X
        self.assertEqual([], extract("下个月底", anchor=MON))         # 他月：不标注

    def test_empty_and_plain_text(self) -> None:
        self.assertEqual([], extract("", anchor=MON))
        self.assertEqual([], extract("今天天气不错但没有具体安排", anchor=MON)[1:])  # 只命中一个“今天”


class AnnotationNoteTest(unittest.TestCase):
    def test_none_when_no_match(self) -> None:
        self.assertIsNone(annotation_note("随便聊聊没有时间", anchor=MON))

    def test_plain_single_annotation_format(self) -> None:
        self.assertEqual(
            "月底≈2026年7月末（模糊时间，未指定具体日期）",
            annotation_note("月底前交", anchor=MON),
        )

    def test_ambiguous_annotation_shows_colloquial_reading(self) -> None:
        self.assertEqual(
            "下周三＝2026-07-22（周三；口语中也可能指 2026-07-15）",
            annotation_note("下周三要交报告", anchor=MON),
        )

    def test_multiple_annotations_joined(self) -> None:
        note = annotation_note("下周三要交报告，月底前全部搞定", anchor=MON)
        self.assertEqual(
            "下周三＝2026-07-22（周三；口语中也可能指 2026-07-15）；"
            "月底≈2026年7月末（模糊时间，未指定具体日期）",
            note,
        )

    def test_duplicate_span_collapses(self) -> None:
        # 同一 span 出现两次只保留第一处
        note = annotation_note("下周三交初稿，下周三再确认", anchor=MON)
        self.assertEqual(1, note.count("下周三＝"))


if __name__ == "__main__":
    unittest.main()
