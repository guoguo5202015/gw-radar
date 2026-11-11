"""Regression coverage for the international office's real list layout."""

import copy
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from international import SOURCES, classify, content_hash, next_page, parse_list


FIXTURES = Path(__file__).with_name("fixtures")
BASE = "https://internationaloffice.gdufs.edu.cn/gjxm/hsqxm.htm"


def item(title, category="hsqxm"):
    return {"title": title, "category": category,
            "url": "https://internationaloffice.gdufs.edu.cn/info/1061/17519.htm"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.skip = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip = True

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip = False
        if tag in {"p", "div", "li"}:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)


class InternationalListsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = (FIXTURES / "international_hsqxm.html").read_text(encoding="utf-8")

    def test_all_eight_requested_sections(self):
        self.assertEqual(len(SOURCES), 8)
        self.assertEqual(len({url for _, url in SOURCES}), 8)
        self.assertTrue(all(name.startswith("国际项目·") for name, _ in SOURCES))

    def test_real_list_dates_before_links_and_bare_project_titles(self):
        rows = parse_list(BASE, self.page)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["date"], "2026-11-11")
        self.assertEqual(rows[1]["date"], "2026-11-03")
        self.assertEqual(rows[0]["url"], "https://internationaloffice.gdufs.edu.cn/info/1061/17519.htm")
        self.assertEqual(rows[0]["source"], "国际项目·寒暑期项目")
        self.assertEqual(rows[0]["category"], "hsqxm")
        self.assertEqual(rows[0]["priority"], 100)
        self.assertNotIn("报名", rows[0]["title"])
        self.assertGreaterEqual(classify(rows[0]), 100)

    def test_real_reverse_numbered_pagination(self):
        self.assertEqual(next_page(BASE, self.page),
                         "https://internationaloffice.gdufs.edu.cn/gjxm/hsqxm/58.htm")
        self.assertEqual(next_page("https://internationaloffice.gdufs.edu.cn/gjxm/hsqxm/58.htm",
                                   '<span class="p_next p_fun"><a href="57.htm">下页</a></span>'),
                         "https://internationaloffice.gdufs.edu.cn/gjxm/hsqxm/57.htm")

    def test_scope_relative_links_nested_text_and_duplicate_rows(self):
        row = '<li><span class="data">2026年11月10日</span><a href="../info/1061/123.htm"><b>新加坡</b>项目</a></li>'
        page = ('<a href="../info/1061/999.htm">导航链接</a><div class="list_r_list"><ul>' +
                row + row + '<li><a href="https://example.com/info/1061/124.htm">外国链接</a></li></ul></div>')
        rows = parse_list(BASE, page)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "新加坡项目")
        self.assertEqual(rows[0]["date"], "2026-11-10")

    def test_next_page_rejects_foreign_origin_and_wrong_section(self):
        for href in ["https://example.com/gjxm/hsqxm/57.htm", "jhsxm/29.htm",
                     "../info/1061/17519.htm", "javascript:alert(1)", "hsqxm.htm",
                     "https://internationaloffice.gdufs.edu.cn:444/gjxm/hsqxm/57.htm",
                     "hsqxm/57.htm?redirect=https://example.com"]:
            with self.subTest(href=href):
                self.assertIsNone(next_page(BASE, '<span class="p_next"><a href="' + href + '">下页</a></span>'))


class InternationalClassificationTest(unittest.TestCase):
    def test_real_undergraduate_only_winter_notice_is_not_rejected(self):
        parser = _Text()
        parser.feed((FIXTURES / "international_winter_17519.html").read_text(encoding="utf-8"))
        paras = "".join(parser.text).splitlines()
        self.assertIn("不接受研究生申请", "".join(paras))
        self.assertGreaterEqual(classify(item("2027寒假新加坡、韩国首尔名企名校访学项目"), {"paras": paras}), 100)

    def test_eligible_and_mixed_audiences_are_retained(self):
        for text in ["本渠道仅面向本科生开放报名，不接受研究生申请。",
                     "仅面向研究生和本科生开放报名。", "仅面向本科生、研究生。",
                     "并非仅限研究生，本项目开放报名。", "不只限研究生，欢迎报名。",
                     "本科生不得重复报名同一项目。", "本科生和研究生均可申请。"]:
            with self.subTest(text=text):
                self.assertGreaterEqual(classify(item("国际访学项目"), {"paras": [text]}), 100)
        self.assertGreaterEqual(classify(item("本科生与研究生国际交换项目报名通知")), 100)
        self.assertGreaterEqual(classify(item("法国商学院4+1本升硕项目报名通知", "sxwxm")), 100)

    def test_explicit_graduate_or_staff_only_calls_are_quiet(self):
        for text in ["本项目仅限全日制研究生申请。", "仅面向本校教师。",
                     "本项目不接受本科生申请。", "本科生不能报名。"]:
            with self.subTest(text=text):
                self.assertEqual(classify(item("交流项目选拔通知"), {"paras": [text]}), 0)
        self.assertEqual(classify(item("教师访学项目申报通知")), 0)
        self.assertEqual(classify(item("研究生交换项目报名通知")), 0)

    def test_results_are_quiet_but_extra_applications_are_not(self):
        for title in ["【春季27SJH014的公示】交换生项目拟推荐名单的公示",
                      "关于公布交换生项目选拔结果的通知", "奖学金拟资助名单",
                      "关于2027年春季交换生项目学生遴选公示"]:
            with self.subTest(title=title):
                self.assertEqual(classify(item(title, "jhsxm")), 0)
        for title in ["交换生拟推荐名单公示及补充报名通知", "关于交换项目报名延期的通知",
                      "赴英国交流项目申请资格调整通知", "赴法国项目补录通知"]:
            with self.subTest(title=title):
                self.assertGreaterEqual(classify(item(title, "jhsxm")), 120)

    def test_separate_scholarship_restriction_does_not_cancel_project_eligibility(self):
        art = {"paras": ["本项目面向本科生和研究生开放报名。",
                         "其中，专项奖学金仅限研究生申请。"]}
        self.assertGreaterEqual(classify(item("国际交流项目报名通知"), art), 100)
        self.assertEqual(classify(item("国际交流专项奖学金申请通知"), art), 0)

    def test_background_sections_need_an_actual_application_call(self):
        for category, title in [("ztqk", "本科生国（境）外访学交流项目介绍"),
                                ("hzbx", "合作办学机构及项目简介"),
                                ("fxjw", "漫步剑桥：在思辨与行走间遇见多元世界"),
                                ("fxjw", "加州大学春季交流访学报告")]:
            with self.subTest(category=category):
                self.assertEqual(classify(item(title, category)), 0)
                self.assertGreaterEqual(classify(item("2027交流项目报名通知", category)), 100)
        self.assertGreaterEqual(classify(item("新的合作交流项目", "hzbx"),
                                        {"paras": ["本项目现已开放报名，请同学提交申请。"]}), 100)

    def test_static_project_help_is_quiet_even_in_opportunity_sections(self):
        self.assertEqual(classify(item("本科生访学项目常见问题", "jhsxm")), 0)
        self.assertEqual(classify(item("项目操作流程", "dxpcxm")), 0)


class InternationalFingerprintTest(unittest.TestCase):
    def setUp(self):
        self.item = item("赴英国交流项目选拔通知")
        self.art = {"paras": ["报名截止为2026年11月30日。", "访问量：100"],
                    "atts": [["申请指南.docx", "https://internationaloffice.gdufs.edu.cn/system/_content/download.jsp?owner=1&wbfileid=abc"]],
                    "deadline": "2026年11月30日", "fetched_at": "2026-11-11"}

    def test_ignores_fetch_time_counters_derived_keys_and_query_order(self):
        modified = copy.deepcopy(self.art)
        modified.update(fetched_at="2026-11-12", keys=["derived summary"], date="2026-11-12")
        modified["paras"][1] = "访问量：999"
        modified["atts"][0][1] = "https://internationaloffice.gdufs.edu.cn/system/_content/download.jsp?wbfileid=abc&owner=1#download"
        self.assertEqual(content_hash(self.item, self.art), content_hash(self.item, modified))

    def test_detects_deadline_body_attachment_and_title_changes(self):
        original = content_hash(self.item, self.art)
        for key, value in [("deadline", "2026年12月1日"),
                           ("paras", ["报名延期至12月1日。"]),
                           ("atts", [["新申请指南.docx", "https://example.com/new.docx"]])]:
            changed = dict(self.art, **{key: value})
            with self.subTest(key=key):
                self.assertNotEqual(original, content_hash(self.item, changed))
        self.assertNotEqual(original, content_hash(item("英国交流项目补充报名通知"), self.art))


if __name__ == "__main__":
    unittest.main()
