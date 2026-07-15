from __future__ import annotations

import unittest
from datetime import datetime

from core.time_normalizer import TimeAnnotation, annotation_note, extract


# 主锚点：2026-07-13 周一。周五锚点 2026-07-17，年末锚点 2026-12-30（周三），
# 年初锚点 2026-01-02（周五）。
MON = datetime(2026, 7, 13, 10, 30)
FRI = datetime(2026, 7, 17, 10, 30)
EOY = datetime(2026, 12, 30, 10, 30)
BOY = datetime(2026, 1, 2, 10, 30)


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
        self.assertEqual("2026-07-10", _first("大前天见过他", MON).date)

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

    def test_month_day_resolves_to_nearest_instance(self) -> None:
        # 年末说一月 → 次年（未来实例更近）
        self.assertEqual("2027-01-05", _first("1月5日交报告", EOY).date)
        # 七月中回顾 7月10日 → 当年的过去日期，不得前滚到明年
        self.assertEqual("2026-07-10", _first("7月10日我们见过面", MON).date)
        # 年初回顾 12月31日 → 上一年年末
        self.assertEqual("2025-12-31", _first("12月31日见过面", BOY).date)

    def test_month_day_leap_and_impossible_dates(self) -> None:
        # 2月29日：取最近的有效闰日实例（2024 与 2028 之间 2028 更近）
        self.assertEqual("2028-02-29", _first("2月29日请假", MON).date)
        self.assertEqual([], extract("2月30日", anchor=MON))

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

    def test_compound_words_do_not_leak_day_substrings(self) -> None:
        # 后天/前天 是纯字符序列，任何「X后/X前 + 天…」组合都会包含它们；
        # 词边界验证必须整类拒绝，而不是依赖前缀字黑名单。
        for text in (
            "今后天气会转冷", "明后天来找我", "史前天气很热",
            "然后天气怎么样", "最后天气也不错", "午后天气很热",
            "雨后天晴", "饭后天气凉快", "睡前天气预报",
            "术后天气", "课后天气不错",
        ):
            self.assertEqual([], extract(text, anchor=MON), text)

    def test_day_word_prefix_of_longer_token_is_not_a_date(self) -> None:
        # 「后天性」「明天科技」被 jieba 切成整体 token，日期词只是其前缀，
        # 余部不是时段词 → 整类拒绝。
        for text in (
            "后天性耳聋", "后天性心脏病", "明天科技公司", "后天下之乐而乐",
        ):
            self.assertEqual([], extract(text, anchor=MON), text)

    def test_day_words_on_word_boundaries_still_annotate(self) -> None:
        self.assertEqual("2026-07-15", _first("后天气温骤降", MON).date)  # 后天|气温
        self.assertEqual("2026-07-15", _first("我后天去北京", MON).date)
        self.assertEqual("2026-07-11", _first("他前天来过", MON).date)
        self.assertEqual("2026-07-13", _first("今晚上加班", MON).date)
        # jieba 把「今天下午」「昨天晚上」合并成整体 token：
        # 日期词是前缀且余部是时段词，仍应标注
        self.assertEqual("2026-07-13", _first("今天下午开会", MON).date)
        self.assertEqual("2026-07-12", _first("昨天晚上下雨", MON).date)
        self.assertEqual("2026-07-12", _first("昨天夜里失眠", MON).date)


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
