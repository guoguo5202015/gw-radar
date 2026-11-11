"""International-project sources and conservative opportunity classification.

The international office publishes important application calls without action
words in their titles. Its dedicated opportunity lists therefore opt articles
in by default, with narrow exclusions for results and explicit audience limits.
All parsing here uses the standard library and makes no network requests.
"""

import hashlib
import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


ROOT = "https://internationaloffice.gdufs.edu.cn"
_CATEGORIES = {
    "ztqk": ("总体情况", "1101"),
    "jhsxm": ("交换生项目", "1012"),
    "dxpcxm": ("单向派出项目", "1013"),
    "sxwxm": ("双学位项目", "1014"),
    "ljwxm": ("留基委项目", "1015"),
    "hsqxm": ("寒暑期项目", "1061"),
    "hzbx": ("合作办学", "1211"),
    "fxjw": ("访学纪闻", "1212"),
}
SOURCES = [("国际项目·" + name, ROOT + "/gjxm/" + slug + ".htm")
           for slug, (name, _) in _CATEGORIES.items()]
_OPPORTUNITIES = {"jhsxm", "dxpcxm", "sxwxm", "ljwxm", "hsqxm"}
_DATE = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
_ARTICLE_PATH = re.compile(r"/info/(\d+)/(\d+)\.htm$")


def _clean(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _origin(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
            return None
        return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def _category(url):
    parsed = urlsplit(url)
    if parsed.hostname != "internationaloffice.gdufs.edu.cn":
        return ""
    match = re.fullmatch(r"/gjxm/([a-z]+)(?:\.htm|/\d+\.htm)", parsed.path)
    if match and match.group(1) in _CATEGORIES:
        return match.group(1)
    match = _ARTICLE_PATH.fullmatch(parsed.path)
    if match:
        return next((slug for slug, (_, col) in _CATEGORIES.items() if col == match.group(1)), "")
    return ""


class _ListParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scope = 0
        self.skip = 0
        self.row = None
        self.link = None
        self.rows = []

    def _finish_row(self):
        if self.row is not None:
            self.rows.append(self.row)
        self.row = self.link = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            if self.scope:
                self.scope += 1
            elif "list_r_list" in attrs.get("class", "").split():
                self.scope = 1
        if not self.scope:
            return
        if tag in ("script", "style"):
            self.skip += 1
        if self.skip:
            return
        if tag == "li":
            self._finish_row()
            self.row = {"text": [], "links": []}
        elif tag == "a" and self.row is not None:
            self.link = {"href": attrs.get("href", ""), "title": attrs.get("title", ""), "text": []}
            self.row["links"].append(self.link)

    def handle_endtag(self, tag):
        if not self.scope:
            return
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        if self.skip:
            return
        if tag == "a":
            self.link = None
        elif tag == "li":
            self._finish_row()
        elif tag == "div":
            self.scope -= 1
            if not self.scope:
                self._finish_row()

    def handle_data(self, data):
        if self.scope and not self.skip and self.row is not None:
            self.row["text"].append(data)
            if self.link is not None:
                self.link["text"].append(data)


def parse_list(base, page):
    """Read dated article rows, including dates that precede their link."""
    category = _category(base)
    if not category:
        return []
    parser = _ListParser()
    parser.feed(page)
    parser.close()
    parser._finish_row()
    found, seen = [], set()
    for row in parser.rows:
        match = _DATE.search("".join(row["text"]))
        date = ""
        if match:
            try:
                from datetime import date as calendar_date
                date = calendar_date(*map(int, match.groups())).isoformat()
            except ValueError:
                pass
        for link in row["links"]:
            url = urljoin(base, link["href"])
            parsed = urlsplit(url)
            if _origin(url) != _origin(base) or not _ARTICLE_PATH.fullmatch(parsed.path):
                continue
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            title = _clean(link["title"] or "".join(link["text"]))
            if not title or url in seen:
                continue
            seen.add(url)
            found.append({"title": title, "url": url, "date": date,
                          "source": "国际项目·" + _CATEGORIES[category][0],
                          "priority": 100, "category": category})
    return found


class _NextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.current = None
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        in_next = any(is_next for _, is_next in self.stack)
        in_next = in_next or "p_next" in attrs.get("class", "").split()
        if tag == "a":
            self.current = {"href": attrs.get("href", ""), "text": [],
                            "next": in_next or "next" in attrs.get("rel", "").split()}
            self.links.append(self.current)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, in_next))

    def handle_endtag(self, tag):
        if tag == "a":
            self.current = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"].append(data)


def next_page(base, page):
    """Follow the actual next link; VSB's archive filenames count backwards."""
    category = _category(base)
    if not category:
        return None
    parser = _NextParser()
    parser.feed(page)
    parser.close()
    for link in parser.links:
        if not link["next"] and _clean("".join(link["text"])) not in {"下页", "下一页"}:
            continue
        url = urljoin(base, link["href"])
        parsed = urlsplit(url)
        if (_origin(url) != _origin(base) or _category(url) != category or
                not re.fullmatch(r"/gjxm/" + re.escape(category) + r"/\d+\.htm", parsed.path) or
                parsed.query):
            continue
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        if url != base.split("#", 1)[0]:
            return url
    return None


_ACTION = re.compile(r"报名|招生|选拔|遴选|招募|申请|申报|补录|补报名|延期|延长|延后|更正|勘误|补充通知|资格.*(?:变更|调整)|截止.*(?:变更|调整)")
_BODY_CALL = re.compile(r"(?:已|现|正在|即将)?(?:开放|开始|接受|启动|组织).{0,16}(?:报名|申请|招生|选拔|遴选|招募)|(?:报名|申请|申报)(?:截止|时间|方式)|现.{0,8}(?:选派|招募|选拔)")
_FOLLOWUP = re.compile(r"补充(?:报名|申请|招募|选拔|遴选)|(?:补录|补报名)|重新(?:开放|报名|申请)|(?:报名|申请|申报|截止|资格|资助).{0,12}(?:延期|延长|延后|调整|变更|更正)|(?:延期|延长|延后).{0,12}(?:报名|申请|申报|截止)")
_RESULT = re.compile(r"公示|(?:拟)?(?:推荐|录取|入选|入围|获奖|资助|派出)名单|(?:选拔|遴选|录取|评选|评审|申报|申请)结果|(?:名单|结果).{0,8}(?:公布|公告)|公布.{0,8}(?:名单|结果)")
_STATIC = re.compile(r"总体情况|项目(?:总体情况)?介绍|项目列表|常见问题|政策法规|管理规定|操作流程|机构及项目简介")
_STORY = re.compile(r"访学(?:纪闻|见闻|报告|心得|体会|分享)|交流(?:心得|体会|报告)|(?:圆满|顺利)(?:结束|完成|开展)|(?:访学|研学|交换)之旅|访学体验")
_UNDERGRAD = re.compile(r"本科|本硕|本升硕|[234]\s*\+\s*[12]")
_RESTRICTED = re.compile(r"(?:仅(?:限|面向|对)|只(?:限|面向|对)|限定为).{0,12}(?:研究生|硕士生|博士生|教师|教职工|教工|辅导员)")
_NO_UNDERGRAD = re.compile(r"(?:不接受|不面向|不含|不包括|不招收)(?:在校|在籍|全日制|本校|我校|普通|任何|应届)*本科生|本科生(?:暂)?(?:不可|不能|不得)(?:报名|申请|参加)|本科生(?:的)?(?:报名|申请)不予受理|本科生不在.{0,8}(?:申请|申报|招生)范围")
_UNDERGRAD_ALLOWED = re.compile(r"(?:面向|接受|招收|选派|选拔|欢迎).{0,18}本科生|本科生.{0,16}(?:均可|可报名|可申请|可以申请|可以报名|可参加|开放报名)|20\d{2}级.{0,14}本科生")
_FUNDING = re.compile(r"奖学金|资助|补助|奖励")


def _exclusive_audience(title, paras):
    # A restriction to graduate students is different from "不接受研究生".
    clauses = re.split(r"[。；;！!\n]", "\n".join([title] + paras))
    allows_undergraduate = any(_UNDERGRAD_ALLOWED.search(clause) and not _NO_UNDERGRAD.search(clause)
                              for clause in clauses)
    for clause in clauses:
        # A separate scholarship's eligibility does not restrict the exchange
        # programme itself. Preserve a clear undergraduate invitation elsewhere.
        if allows_undergraduate and _FUNDING.search(clause) and not _FUNDING.search(title):
            continue
        if _NO_UNDERGRAD.search(clause):
            return True
        if not _UNDERGRAD.search(clause):
            for match in _RESTRICTED.finditer(clause):
                prefix = re.sub(r"\s+", "", clause[:match.start()])
                if not re.search(r"(?:不|非|不是)$", prefix):
                    return True
    # An explicitly graduate/staff programme in a title is also unambiguous;
    # undergraduate-to-master's and mixed undergraduate calls remain eligible.
    return (not _UNDERGRAD.search(title) and
            bool(re.search(r"(?:研究生|硕士生|博士生|教师|教职工)(?:国际|海外|交换|访学|出国|留学|奖学金|项目|培训|研修|招生)", title)))


def classify(item, art=None):
    """Return 100+ for opportunities, 0 for narrowly identified non-calls.

    Missing article text never suppresses a dedicated opportunity-list item.
    Eligibility and deadlines still need to be shown verbatim to the reader.
    """
    category = item.get("category") or _category(item.get("url", ""))
    if category not in _CATEGORIES:
        return 0
    title = _clean(item.get("title"))
    art = art or {}
    paras = [_clean(p) for p in art.get("paras", []) if p]
    action = bool(_ACTION.search(title))
    followup = bool(_FOLLOWUP.search(title))
    if _exclusive_audience(title, paras):
        return 0
    if _RESULT.search(title) and not followup:
        return 0
    if (_STATIC.search(title) or _STORY.search(title)) and not action:
        return 0
    if category not in _OPPORTUNITIES and not (action or _BODY_CALL.search(" ".join(paras))):
        return 0
    return 100 + (10 if action else 0) + (20 if followup else 0) + (5 if re.search(r"奖学金|资助|补助", title) else 0)


_COUNTS = re.compile(r"(?:访问量|浏览量|浏览次数|点击量|点击次数|下载次数)\s*[:：]?\s*\d+|已下载\s*\d+\s*次")


def _stable_text(value):
    return _clean(_COUNTS.sub("", _clean(value)))


def content_hash(item, art):
    """Fingerprint notice content, excluding fetch timestamps and counters."""
    art = art or {}
    attachments = []
    for att in art.get("atts", []):
        if isinstance(att, dict):
            name, url = att.get("name", ""), att.get("url", "")
        elif isinstance(att, (list, tuple)) and len(att) >= 2:
            name, url = att[:2]
        else:
            continue
        parsed = urlsplit(html.unescape(str(url)))
        canonical_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                   urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True))), ""))
        attachments.append((_stable_text(name), canonical_url))
    data = {"title": _stable_text(item.get("title", "")),
            "paras": [text for text in (_stable_text(p) for p in art.get("paras", [])) if text],
            "atts": sorted(set(attachments)), "deadline": _stable_text(art.get("deadline", ""))}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()
