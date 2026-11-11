import copy
import unittest

from article_details import enrich_article


class ArticleDetailsTests(unittest.TestCase):
    def article(self, body, published="2026-11-11", title="2027年寒假项目报名通知", atts=None):
        return enrich_article(
            {"date": published, "title": title},
            {"paras": body if isinstance(body, list) else [body], "atts": atts or [],
             "keys": [], "deadline": "旧提取结果", "date": published},
        )

    def test_explicit_year_and_time_are_preserved(self):
        art = self.article("请于2027年3月1日14:00前提交申请材料。")
        self.assertEqual(art["deadline_iso"], "2027-03-01")
        self.assertEqual(art["deadline"], "2027年3月1日 14:00")
        self.assertEqual(art["deadline_context"], "请于2027年3月1日14:00前提交申请材料")

    def test_iso_date(self):
        art = self.article("报名截止时间：2027-03-01 14:00。")
        self.assertEqual(art["deadline_iso"], "2027-03-01")
        self.assertEqual(art["deadline_time"], "14:00")

    def test_title_year_does_not_override_publication_year(self):
        art = self.article("申请截止日期为11月25日。")
        self.assertEqual(art["deadline_iso"], "2026-11-25")

    def test_winter_notice_rolls_missing_year_into_spring(self):
        art = self.article("请于3月1日前提交申请。")
        self.assertEqual(art["deadline_iso"], "2027-03-01")

    def test_recent_past_date_is_not_rolled_a_year(self):
        art = self.article("报名截止日期为11月10日。")
        self.assertEqual(art["deadline_iso"], "2026-11-10")

    def test_explicit_previous_year_is_not_rolled(self):
        art = self.article("报名截止日期为2025年3月1日。")
        self.assertEqual(art["deadline_iso"], "2025-03-01")

    def test_full_registration_range_uses_end(self):
        art = self.article("报名时间：2026年11月1日至2027年3月1日14:00。")
        self.assertEqual(art["deadline_iso"], "2027-03-01")
        self.assertEqual(art["deadline_time"], "14:00")

    def test_short_end_date_inherits_range_month(self):
        art = self.article("报名时间：11月1日至25日。")
        self.assertEqual(art["deadline_iso"], "2026-11-25")

    def test_range_end_inherits_year_with_rollover(self):
        art = self.article("申请时间为2026年12月20日至1月5日。")
        self.assertEqual(art["deadline_iso"], "2027-01-05")

    def test_time_heading_can_be_separate_paragraph(self):
        art = self.article(["报名时间：", "2026年11月1日至25日", "活动时间：2027年1月5日至15日"])
        self.assertEqual(art["deadline_iso"], "2026-11-25")

    def test_activity_date_is_not_registration_deadline(self):
        art = self.article("项目时间为2027年1月5日至1月20日，请于11月25日前报名。")
        self.assertEqual(art["deadline_iso"], "2026-11-25")
        for text in ("活动时间为2027年1月5日至1月20日。", "报名成功后，项目时间为2027年1月5日至1月20日。"):
            with self.subTest(text=text):
                self.assertEqual(self.article(text)["deadline_iso"], "")

    def test_public_notice_and_objection_deadlines_are_excluded(self):
        for text in ("申请名单公示截止时间为11月20日。", "如有异议请于11月20日前提交材料。"):
            with self.subTest(text=text):
                self.assertEqual(self.article(text)["deadline_iso"], "")

    def test_invalid_dates_and_clocks_are_rejected(self):
        for value in ("2027年2月29日", "2026年2月30日", "2026年13月1日", "2027-03-32", "2027年3月1日25:00"):
            with self.subTest(value=value):
                self.assertEqual(self.article(f"报名截止日期为{value}。")["deadline_iso"], "")

    def test_leap_day(self):
        self.assertEqual(self.article("申请截止时间2028年2月29日。")["deadline_iso"], "2028-02-29")

    def test_missing_publication_does_not_guess_year(self):
        self.assertEqual(self.article("报名截止时间11月20日。", published="")["deadline_iso"], "")

    def test_submission_without_a_deadline_does_not_imply_one(self):
        self.assertEqual(self.article("11月20日开始接受申请。")["deadline_iso"], "")

    def test_registered_students_activity_range_is_not_a_deadline(self):
        art = self.article("申请成功的学生将于2027年1月5日至1月20日参加活动。")
        self.assertEqual(art["deadline_iso"], "")

    def test_submit_during_range(self):
        art = self.article("请于11月1日至25日期间提交申请。")
        self.assertEqual(art["deadline_iso"], "2026-11-25")

    def test_before_clock_with_jiang(self):
        art = self.article("请于2027年3月1日14:00前将申请表发送至项目邮箱。")
        self.assertEqual(art["deadline_iso"], "2027-03-01")
        self.assertEqual(art["deadline_time"], "14:00")

    def test_single_day_registration_clock_range_uses_end(self):
        art = self.article("报名时间：2026年11月20日9:00—17:30。")
        self.assertEqual(art["deadline_iso"], "2026-11-20")
        self.assertEqual(art["deadline_time"], "17:30")

    def test_reversed_or_invalid_clock_range_is_not_guessed(self):
        for text in ("报名时间：2026年11月20日17:00—9:00。", "报名时间：2026年11月20日9:00—27:30。"):
            with self.subTest(text=text):
                self.assertEqual(self.article(text)["deadline_iso"], "")

    def test_multiple_stages_are_left_unresolved(self):
        art = self.article("个人申请截止11月20日；学院提交截止11月25日。")
        self.assertEqual(art["deadline_iso"], "")

    def test_repeated_same_deadline_is_accepted(self):
        art = self.article(["申请截止时间为11月20日。", "请于11月20日前提交申请材料。"])
        self.assertEqual(art["deadline_iso"], "2026-11-20")

    def test_afternoon_time(self):
        art = self.article("请于11月20日（星期五）下午2时30分前提交材料。")
        self.assertEqual(art["deadline_time"], "14:30")

    def test_attachment_note_does_not_claim_attachment_was_read(self):
        art = self.article("报名详情见附件。", atts=[["2027年3月1日截止申请.pdf", "https://example.org/a.pdf"]])
        self.assertEqual(art["deadline_iso"], "")
        self.assertIn("附件内容尚未解析", art["attachment_note"])
        self.assertIn("请查看附件及原通知", art["attachment_note"])

    def test_no_attachment_note_when_body_has_clear_deadline(self):
        art = self.article("申请截止日期11月20日。", atts=[["通知.pdf", "https://example.org/a.pdf"]])
        self.assertEqual(art["attachment_note"], "")

    def test_input_is_not_modified_and_other_fields_are_retained(self):
        original = {"paras": ["报名截止时间11月20日"], "atts": [], "keys": ["资格"], "deadline": "旧", "date": "2026-11-01"}
        before = copy.deepcopy(original)
        result = enrich_article({"date": "2026-10-01"}, original)
        self.assertEqual(original, before)
        for field in ("paras", "atts", "keys", "date"):
            self.assertEqual(result[field], original[field])
        self.assertIsNot(result, original)


if __name__ == "__main__":
    unittest.main()
