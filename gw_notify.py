#!/usr/bin/env python3
"""广外通知雷达：定时抓取广外各部门通知，有要紧的新通知就发邮件。

设计原则（手机优先）
- 重要通知（★）一条一推：微信卡片上直接看到简短标题和截止日期
- 点开推送就是排好版的全文、关键信息、附件，不需要再跳去学校电脑版网页
- 每条通知另存一份手机版页面在 docs/n/ 下，网页列表也链接到这里

用法
  python gw_notify.py            正常运行
  python gw_notify.py --dry-run  只抓取和打印，不推送、不保存
环境变量
  MAIL_USER   发件QQ邮箱，如 12345@qq.com（不设就只打印不发信）
  MAIL_PASS   QQ邮箱的SMTP授权码（不是QQ密码）
  MAIL_TO     收件人，逗号分隔；不设就发给自己
  SITE_URL         手机版页面的网址前缀，默认 https://guoguo5202015.github.io/gw-radar
"""
import json, os, re, sys, time, html, smtplib, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
import international
from article_details import enrich_article
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from html.parser import HTMLParser
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "seen.json")
DOCS = os.path.join(HERE, "docs")
SITE_URL = os.environ.get("SITE_URL", "https://guoguo5202015.github.io/gw-radar").rstrip("/")
CST = timezone(timedelta(hours=8))

SOURCES = international.SOURCES + [
    ("学生工作部", "https://nstu.gdufs.edu.cn/"),
    ("会计学院·学生工作", "https://kjxy.gdufs.edu.cn/xsgz/xsdt.htm"),
    ("会计学院·国际交流", "https://kjxy.gdufs.edu.cn/gjjl/jlxm.htm"),
    ("会计学院", "https://kjxy.gdufs.edu.cn/"),
    ("国际交流合作部", "https://internationaloffice.gdufs.edu.cn/tzgg1/gjxm.htm"),
    ("国际交流合作部·公示", "https://internationaloffice.gdufs.edu.cn/gs/gjxm.htm"),
    ("国际交流合作部", "https://internationaloffice.gdufs.edu.cn/"),
    ("教务部", "https://jwb.gdufs.edu.cn/xwtz/tz.htm"),
    ("教务部", "https://jwb.gdufs.edu.cn/xwtz/jxtz.htm"),
    ("校团委", "https://youth.gdufs.edu.cn/"),
    ("创新创业学院", "https://gwcxcy.gdufs.edu.cn/"),
]

HOT = ["先到先得", "额满即止", "名额有限", "截止", "报名", "选拔", "申请", "资助", "奖学金",
       "助学金", "补助", "访学", "交流项目", "交换", "夏令营", "冬令营", "海外", "境外", "新生",
       "2026级", "大一", "竞赛", "大赛", "推免", "保研", "PRIME", "招募"]
# 只要通知类栏目；新闻、领导活动、辅导员专栏等栏目一律不收（站点前缀, 栏目号）
NOTICE_COLS = {("nstu", "1056"), ("kjxy", "1107"), ("kjxy", "1470"), ("internationaloffice", "1069"),
               ("internationaloffice", "1112"), ("jwb", "1044"), ("jwb", "1045"), ("youth", "1050"),
               ("gwcxcy", "1201")}
# 结果公示、只对老师或研究生的，不发邮件（网页上照样能查到）
QUIET = ["公示", "名单", "结果", "研究生", "教师", "教工", "辅导员", "立项", "学籍", "毕业生"]
NEWSY = ["圆满", "举办", "举行", "召开", "来访", "开展主题", "顺利", "成功", "喜报", "专栏"]

MAX_MAIL = 8              # 一轮最多发几封，防止意外刷屏

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
DATE_RE = re.compile(r"(20\d\d)\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")


def get(url, tries=3):
    err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            err = e
            time.sleep(2 + 3 * i)
    raise err


# ---------- 列表页 ----------
def parse_list(base, page):
    out, seen = [], set()
    for m in re.finditer(r"<a([^>]+)>(.*?)</a>(.{0,300})", page, re.S):
        attrs = m.group(1)
        h = re.search(r'href="([^"]*info/\d+/\d+\.htm)"', attrs)
        if not h:
            continue
        url = urllib.parse.urljoin(base, h.group(1))
        if url in seen:
            continue
        t = re.search(r'title="([^"]+)"', attrs)
        title = t.group(1) if t else re.sub(r"<[^>]+>", "", m.group(2))
        title = html.unescape(re.sub(r"\s+", " ", title)).strip()
        title = DATE_RE.split(title)[0].strip(" /-")
        if len(title) < 5:
            continue
        d = DATE_RE.search(m.group(2) + m.group(3))
        date = "%s-%02d-%02d" % (d.group(1), int(d.group(2)), int(d.group(3))) if d else ""
        seen.add(url)
        out.append({"title": title, "url": url, "date": date})
    return out


# ---------- 正文页 ----------
class _Body(HTMLParser):
    """取出 v_news_content 里的段落，表格行合成一行"""
    BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "br", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0          # 进入正文 div 后的嵌套层数
        self.skip = 0
        self.cell = 0           # 在表格单元格里时，内部的段落不断行
        self.buf, self.paras, self.cells = [], [], []

    def flush(self):
        t = re.sub(r"\s+", " ", "".join(self.buf)).strip()
        if t:
            self.paras.append(t)
        self.buf = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if not self.depth:
            if tag == "div" and "v_news_content" in (a.get("class") or ""):
                self.depth = 1
            return
        if tag == "div":
            self.depth += 1
        if tag in ("style", "script"):
            self.skip += 1
        if tag in ("td", "th"):
            self.cell += 1
            if self.buf and "".join(self.buf).strip():
                self.buf.append(" ｜ ")
        elif tag == "tr":
            self.cell = 0
            self.flush()
        elif tag in self.BLOCK and not self.cell:
            self.flush()
        elif tag in self.BLOCK:
            self.buf.append(" ")

    def handle_endtag(self, tag):
        if not self.depth:
            return
        if tag in ("style", "script"):
            self.skip = max(0, self.skip - 1)
        if tag in ("td", "th"):
            self.cell = max(0, self.cell - 1)
        elif tag in self.BLOCK and not self.cell:
            self.flush()
        if tag == "div":
            self.depth -= 1
            if self.depth == 0:
                self.flush()

    def handle_data(self, data):
        if self.depth and not self.skip:
            self.buf.append(data)


KEY_RE = re.compile(r"截止|之前|前提交|前将|前报|前发送|先到先得|额满|名额|报名时间|申请时间|报名方式|申请方式"
                    r"|申报对象|申请对象|申请条件|选拔对象|联系人|咨询")
DEADLINE_RE = re.compile(r"(\d{1,2}月\d{1,2}日(?:[（(][^）)]{1,4}[）)])?(?:\s*\d{1,2}[:：]\d{2})?)[^，。；]{0,8}?(?:前|之前|截止)"
                         r"|截止(?:时间|日期)?[为是：:]?\s*(?:20\d\d年)?(\d{1,2}月\d{1,2}日)")


RANGE_RE = re.compile(r"(\d{1,2}月\d{1,2}日)[^。；｜]{0,14}?[-—–至到~]\s*(\d{1,2}月)?(\d{1,2}日(?:\s*\d{1,2}[:：]\d{2})?)")


def find_deadline(paras):
    """先找“X月X日前/截止”，再找报名、提交那一行里的日期区间，取结束日期"""
    text = " ".join(paras)
    m = DEADLINE_RE.search(text)
    if m:
        return m.group(1) or m.group(2)
    for x in paras:
        if re.search(r"报名|提交|申请", x):
            r = RANGE_RE.search(x)
            if r:
                month = r.group(2) or re.match(r"\d{1,2}月", r.group(1)).group(0)
                return month + r.group(3)
    return ""


def fetch_article(url):
    page = get(url)
    p = _Body()
    p.feed(page)
    paras = [x for x in p.paras if not re.fullmatch(r"[\s｜]*", x)]
    atts, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="([^"]*download\.jsp[^"]*|[^"]+\.(?:pdf|docx?|xlsx?|zip|rar))"[^>]*>(.*?)</a>', page, re.S):
        u = urllib.parse.urljoin(url, html.unescape(m.group(1)))
        name = html.unescape(re.sub(r"<[^>]+>|\s", "", m.group(2))) or "附件"
        if u not in seen and not name.lower().endswith((".jpg", ".png", ".jpeg", ".gif")):
            seen.add(u)
            atts.append([name, u])
    pdf = re.search(r'showVsbpdfIframe\("([^"]+)"', page)  # 有的通知正文是嵌在页面里的PDF
    if pdf:
        atts.insert(0, ["通知正文（PDF）", urllib.parse.urljoin(url, html.unescape(pdf.group(1)))])
    text = " ".join(paras)
    keys = [x for x in paras if RANGE_RE.search(x) and re.search(r"报名|提交|申请", x) and len(x) < 160][:2]
    for sent in re.split(r"[。；;！!]", text):
        s = sent.strip(" ｜")
        if 6 < len(s) < 160 and KEY_RE.search(s) and s not in keys:
            keys.append(s)
        if len(keys) >= 5:
            break
    deadline = find_deadline(paras)
    head = page[:page.find("v_news_content")] if "v_news_content" in page else page
    dm = list(DATE_RE.finditer(head))
    date = "%s-%02d-%02d" % (dm[-1].group(1), int(dm[-1].group(2)), int(dm[-1].group(3))) if dm else ""
    return {"paras": paras, "atts": atts, "keys": keys, "deadline": deadline, "date": date}


# ---------- 工具 ----------
def column(url):
    m = re.search(r"https?://([a-z0-9]+)\.gdufs\.edu\.cn/info/(\d+)/", url)
    return m.groups() if m else None


def score(title):
    notice = title.startswith("关于") or "通知" in title  # “关于举办××大赛的通知”是通知，不是新闻稿
    if any(k in title for k in QUIET) or (not notice and any(k in title for k in NEWSY)):
        return 0
    return sum(1 for k in HOT if k in title)


def short_title(t, n=22):
    s = re.sub(r"^(关于|广东外语外贸大学|我校)", "", t)
    s = re.sub(r"^(开展|做好|举办|公布|发布)", "", s)
    s = re.sub(r"^20\d\d(-20\d\d)?(学?年度?)?", "", s)
    s = re.sub(r"(的通知|的公告|通知|公告)$", "", s)
    return s if len(s) <= n else s[:n - 1] + "…"


def page_id(url):
    m = re.search(r"https?://([a-z0-9]+)\.gdufs\.edu\.cn/info/(\d+)/(\d+)\.htm", url)
    return "-".join(m.groups()) if m else re.sub(r"\W+", "-", url)[-40:]


def has_page(it):
    return os.path.exists(os.path.join(DOCS, "n", page_id(it["url"]) + ".html"))


def page_url(it):
    return f"{SITE_URL}/n/{page_id(it['url'])}.html"


CSS = """:root{--bg:#fafaf7;--fg:#1d1d1b;--mut:#77756f;--acc:#b4321e;--box:#f3ece4;--line:#e6e3dc}
@media (prefers-color-scheme:dark){:root{--bg:#151514;--fg:#ecebe7;--mut:#9a978f;--acc:#ff7a5c;--box:#2a2420;--line:#2b2a28}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.75 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;-webkit-text-size-adjust:100%}
main{max-width:680px;margin:0 auto;padding:18px 16px 60px}
h1{font-size:21px;line-height:1.45;margin:0 0 6px}.m{color:var(--mut);font-size:13px}
.box{background:var(--box);border-radius:10px;padding:12px 14px;margin:14px 0}
.box b{color:var(--acc)}.box div{font-size:15px;margin:4px 0}
p{margin:0 0 12px;word-break:break-word}h2{font-size:15px;color:var(--mut);margin:22px 0 8px}
a{color:var(--acc)}.att a{display:block;padding:10px 0;border-bottom:1px solid var(--line);text-decoration:none}
.back{display:inline-block;margin-bottom:12px;font-size:14px;color:var(--mut);text-decoration:none}"""


def render_article(it, art):
    esc = html.escape
    dl = f"<div><b>截止 {esc(art['deadline'])}</b></div>" if art["deadline"] else ""
    keys = "".join(f"<div>· {esc(k)}</div>" for k in art["keys"])
    box = f'<div class="box">{dl}{keys}</div>' if (dl or keys) else ""
    body = "".join(f"<p>{esc(x)}</p>" for x in art["paras"]) or "<p>（正文抓取失败，请看原文）</p>"
    atts = ""
    if art["atts"]:
        atts = "<h2>附件</h2><div class=att>" + "".join(
            f'<a href="{esc(u)}">📎 {esc(n)}</a>' for n, u in art["atts"]) + "</div>"
    os.makedirs(os.path.join(DOCS, "n"), exist_ok=True)
    with open(os.path.join(DOCS, "n", page_id(it["url"]) + ".html"), "w", encoding="utf-8") as f:
        f.write(f"""<!doctype html><html lang=zh-CN><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{esc(short_title(it['title']))}</title>
<style>{CSS}</style></head><body><main><a class=back href="../index.html">← 全部通知</a>
<h1>{esc(it['title'])}</h1><div class=m>{esc(it['source'])} · {it.get('date') or ''}</div>
{box}{body}{atts}<h2>原文</h2><p><a href="{esc(it['url'])}">在学校网站打开（电脑版）</a></p></main></body></html>""")


# 不带引号的写法也是合法 CSS，放进 style='…' 里不会把属性截断
SERIF = "Noto Serif SC,Source Han Serif SC,Songti SC,STSong,SimSun,serif"
SANS = "-apple-system,BlinkMacSystemFont,PingFang SC,MiSans,HarmonyOS Sans SC,Microsoft YaHei,sans-serif"
INK, BODY, MUTE, LINE, ACC, PAPER = "#1c1a17", "#3b3732", "#9a9185", "#ebe5da", "#a8321f", "#f4f0e8"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def _clauses(paras):
    text = "。".join(paras)
    return [c.strip(" ｜，,") for c in re.split(r"[。；;！!\n]", text) if c.strip(" ｜，,")]


def _clip(s, key, n=80):
    """长句按逗号切开，只留关键词所在的那一截和它前面一截"""
    if len(s) <= n:
        return s
    segs = re.split(r"(?<=[，,])", s)
    j = next((k for k, x in enumerate(segs) if key in x), 0)
    for lo in (j - 1, j):
        part = "".join(segs[max(lo, 0):j + 1]).strip("，, ")
        if len(part) <= n:
            return ("…" if lo > 0 else "") + part + ("…" if j + 1 < len(segs) else "")
    return segs[j][:n] + "…"


def deadline_info(it, art):
    """返回 (大字日期, 补充说明, 还剩几天) ；没有截止日期返回 None"""
    if art.get("deadline_iso"):
        date = datetime.strptime(art["deadline_iso"], "%Y-%m-%d").date()
        left = (date - datetime.now(CST).date()).days
        return f"{date.month}月{date.day}日", art.get("deadline_time", ""), left
    dl = art.get("deadline") or ""
    m = re.match(r"(\d{1,2})月(\d{1,2})日", dl)
    if not m:
        return None
    mon, day = int(m.group(1)), int(m.group(2))
    base = (it.get("date") or datetime.now(CST).strftime("%Y-%m-%d"))
    year = int(base[:4]) + (1 if mon < int(base[5:7]) - 6 else 0)
    today = datetime.now(CST).date()
    try:
        left = (datetime(year, mon, day, tzinfo=CST).date() - today).days
    except ValueError:
        left = None
    text = "。".join(art["paras"])
    i = text.find(m.group(0))
    tail = text[i + len(m.group(0)): i + len(m.group(0)) + 14] if i >= 0 else ""
    t = re.match(r"\s*(?:[（(][^）)]{1,4}[）)])?\s*((?:上午|中午|下午|晚上)?\s*\d{1,2}(?:[:：]\d{2}|点|时)(?:\d{1,2}分)?)", tail)
    clock = t.group(1).replace("：", ":").replace(" ", "") if t else ""
    if not clock:
        c2 = re.search(r"\d{1,2}[:：]\d{2}", dl)
        clock = c2.group(0).replace("：", ":") if c2 else ""
    return f"{mon}月{day}日", clock, left


def key_rows(it, art):
    """从正文里挑出几条要点：怎么交、谁能报、名额、时间、联系"""
    rows, used = [], set()
    cl = _clauses(art["paras"])
    rules = [
        ("怎么交", r"邮箱|发送至|发至|报送|提交|系统|链接|二维码", 1),
        ("谁能报", r"对象|面向|条件|要求.{0,6}(学生|本科)|全日制", 0),
        ("名额", r"名额|人数|限额|额满", 0),
        ("时间", r"报名时间|申请时间|活动时间|举办时间|宣讲", 0),
        ("联系", r"联系人|联系电话|咨询|电话", 0),
    ]
    for label, pat, want_mail in rules:
        best = None
        for c in cl:
            mm = re.search(pat, c)
            if not mm or c in used or len(c) < 12:
                continue
            if want_mail and EMAIL_RE.search(c):
                best = c
                break
            best = best or c
        if best:
            used.add(best)
            key = (EMAIL_RE.search(best) or re.search(pat, best)).group(0)
            rows.append((label, _clip(best, key)))
    return rows[:4]


def _body_html(paras):
    out = []
    n = len(paras)
    for i, x in enumerate(paras):
        e = html.escape(x)
        e = EMAIL_RE.sub(lambda m: f"<a href='mailto:{m.group(0)}' style='color:{ACC};text-decoration:none'>{m.group(0)}</a>", e)
        e = re.sub(r"(\d{1,2}月\d{1,2}日[^，。；]{0,12}?(?:前|之前|截止))",
                   rf"<span style='color:{ACC};font-weight:600'>\1</span>", e)
        tail = i >= n - 3 and len(x) < 30 and not re.search(r"[，。]", x)
        if re.match(r"^([一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|\d+[.、．]\s*\S{2,12}$)", x) and len(x) < 40:
            out.append(f"<p style='margin:22px 0 8px;font-size:16px;font-weight:600;color:{INK}'>{e}</p>")
        elif tail:
            out.append(f"<p style='margin:0;font-size:14px;color:{MUTE};text-align:right'>{e}</p>")
        elif i == 0 and x.endswith(("：", ":")) and len(x) < 20:
            out.append(f"<p style='margin:0 0 12px;font-size:15px;color:{MUTE}'>{e}</p>")
        else:
            out.append(f"<p style='margin:0 0 14px;font-size:16px;line-height:1.85;color:{BODY};text-align:justify'>{e}</p>")
    return "".join(out)


def mail_shell(inner, preheader=""):
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"></head>
<body style="margin:0;padding:0;background:{PAPER};font-family:{SANS};-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{html.escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAPER}"><tr><td align="center" style="padding:20px 10px 32px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:16px;border:1px solid {LINE}">
<tr><td style="padding:26px 24px 30px">{inner}</td></tr></table>
<p style="margin:16px 0 0;font-size:12px;color:{MUTE};letter-spacing:1px">广外通知雷达 · 自动发送</p>
</td></tr></table></body></html>"""


def mail_html(it, art):
    """邮件正文：最上面是截止和要点，下面是排好版的全文"""
    esc = html.escape
    meta = f"{esc(it['source'])}<span style='color:{LINE}'>　｜　</span>{esc(it.get('date') or '')}"
    parts = [f"<div style='font-size:12px;letter-spacing:2px;color:{MUTE};margin-bottom:14px'>{meta}</div>",
             f"<h1 style='margin:0 0 20px;font-family:{SERIF};font-size:23px;line-height:1.45;font-weight:700;color:{INK}'>"
             f"{esc(short_title(it['title'], 60))}</h1>"]
    if it.get("priority"):
        label = "国际项目 · 重点关注"
        if it.get("pending_reason") == "updated":
            label += " · 原通知有更新，请重新核对条件和附件"
        elif it.get("pending_reason") == "backfill":
            label += " · 近期项目补收录，请核对是否仍开放报名"
        parts.insert(0, f"<p style='color:{ACC};font-size:13px;font-weight:600'>{label}</p>")
    dli = deadline_info(it, art)
    pre = ""
    if dli:
        day, clock, left = dli
        if left is not None and left < 0:
            chip, cc = "已截止", MUTE
        elif left is not None:
            chip = "今天截止" if left == 0 else f"还剩 {left} 天"
            cc = ACC if left <= 3 else "#8a6d3b"
        else:
            chip, cc = "", MUTE
        chip_html = (f"<span style='display:inline-block;margin-left:10px;padding:3px 10px;border-radius:99px;"
                     f"font-family:{SANS};font-size:13px;font-weight:600;color:#fff;background:{cc};vertical-align:middle'>{chip}</span>") if chip else ""
        parts.append(
            f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='margin:0 0 18px;background:#fbf3ee;border-radius:12px'>"
            f"<tr><td style='padding:16px 18px;border-left:4px solid {ACC};border-radius:12px'>"
            f"<div style='font-size:12px;letter-spacing:2px;color:{ACC};margin-bottom:4px'>截止</div>"
            f"<div style='font-family:{SERIF};font-size:28px;line-height:1.3;font-weight:700;color:{INK}'>{day}"
            f"<span style='font-size:18px;font-weight:600;margin-left:6px'>{esc(clock)}</span>{chip_html}</div></td></tr></table>")
        pre = f"截止{day}{clock}"
        if art.get("deadline_context"):
            parts.append(f"<p style='font-size:13px;color:{BODY}'>截止依据：{esc(art['deadline_context'])}</p>")
    if art.get("attachment_note"):
        parts.append(f"<p style='font-size:14px;color:{ACC}'>{esc(art['attachment_note'])}</p>")
    rows = key_rows(it, art)
    if rows:
        tr = "".join(
            f"<tr><td valign='top' style='width:56px;padding:9px 0;font-size:13px;color:{MUTE};border-top:1px solid {LINE};white-space:nowrap'>{esc(l)}</td>"
            f"<td style='padding:9px 0;font-size:15px;line-height:1.65;color:{INK};border-top:1px solid {LINE}'>"
            + EMAIL_RE.sub(lambda m: f"<a href='mailto:{m.group(0)}' style='color:{ACC};text-decoration:none'>{m.group(0)}</a>", esc(v))
            + "</td></tr>" for l, v in rows)
        parts.append(f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='margin:0 0 18px;border-bottom:1px solid {LINE}'>{tr}</table>")
        pre += " · " + rows[0][1][:40]
    if art["atts"]:
        a = "".join(
            f"<a href='{esc(u)}' style='display:block;margin:0 0 8px;padding:11px 14px;border:1px solid {LINE};border-radius:10px;"
            f"font-size:14px;color:{INK};text-decoration:none;word-break:break-all'><span style='color:{ACC};font-weight:600'>↓</span>　{esc(n)}</a>"
            for n, u in art["atts"])
        parts.append(f"<div style='font-size:12px;letter-spacing:2px;color:{MUTE};margin:4px 0 10px'>附件 {len(art['atts'])}</div>{a}")
    parts.append(f"<div style='margin:26px 0 16px;text-align:center;font-size:12px;letter-spacing:4px;color:{MUTE}'>— 原文 —</div>")
    parts.append(_body_html(art["paras"]) or f"<p style='font-size:15px;color:{MUTE};text-align:center'>这条通知的正文是PDF或图片，点上面的附件或下面的原网页查看。</p>")
    parts.append(f"<div style='margin-top:26px;padding-top:16px;border-top:1px solid {LINE};text-align:center'>"
                 f"<a href='{esc(it['url'])}' style='font-size:13px;color:{MUTE};text-decoration:none'>在学校网站查看原文 →</a></div>")
    return mail_shell("".join(parts), pre or it["source"])


def mail_subject(it, art):
    dli = deadline_info(it, art)
    head = ""
    if dli and (dli[2] is None or dli[2] >= 0):
        m = re.match(r"(\d+)月(\d+)日", dli[0])
        head = f"【{m.group(1)}/{m.group(2)}截止】"
    topic = "【国际项目更新】" if it.get("pending_reason") == "updated" else "【国际项目】"
    return (topic if it.get("priority") else "") + head + short_title(it["title"], 55 if it.get("priority") else 40)


def send_mail(subject, content_html):
    user = os.environ.get("MAIL_USER", "").strip()
    pwd = os.environ.get("MAIL_PASS", "").strip()
    if not (user and pwd):
        print("[未设置 MAIL_USER/MAIL_PASS，跳过发信]", subject)
        return False
    to = [x.strip() for x in os.environ.get("MAIL_TO", "").split(",") if x.strip()] or [user]
    msg = MIMEText(content_html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("广外通知雷达", "utf-8")), user))
    msg["To"] = ", ".join(to)
    for i in range(3):
        try:
            with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as s:
                s.login(user, pwd)
                s.sendmail(user, to, msg.as_string())
            print("已发信", subject)
            return True
        except Exception as e:
            print("[发信失败，重试]", e)
            time.sleep(5)
    raise RuntimeError("发信失败：检查 MAIL_USER 是不是邮箱地址、MAIL_PASS 是不是授权码")


def render_index(st):
    esc = html.escape
    items = sorted(st["items"].values(), key=lambda x: (bool(x.get("priority") and x.get("score")), x.get("date") or "0000", x["found"]), reverse=True)
    rows = []
    for it in items[:500]:
        hot = it.get("score", 0) > 0
        link = page_url(it).replace(SITE_URL + "/", "") if has_page(it) else it["url"]
        dl = f' · <b>截止 {esc(it["deadline"])}</b>' if it.get("deadline") else ""
        rows.append(f'<li class="{"hot" if hot else ""}" data-t="{esc(it["title"] + it["source"])}">'
                    f'<a href="{esc(link)}">{"★ " if hot else ""}{esc(it["title"])}</a>'
                    f'<div class=m>{esc(it["source"])} · {it.get("date") or "无日期"}{dl}</div></li>')
    now = datetime.now(CST).strftime("%m-%d %H:%M")
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
        f.write(f"""<!doctype html><html lang=zh-CN><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>广外通知雷达</title>
<style>{CSS}
input[type=search]{{width:100%;padding:11px 12px;font-size:16px;border:1px solid var(--line);border-radius:10px;background:transparent;color:var(--fg)}}
.bar{{display:flex;gap:8px;margin:10px 0 4px;flex-wrap:wrap}}.bar button{{border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:16px;padding:5px 12px;font-size:14px}}
.bar button.on{{background:var(--acc);border-color:var(--acc);color:#fff}}
ul{{list-style:none;padding:0;margin:0}}li{{padding:12px 0;border-bottom:1px solid var(--line)}}
li a{{color:var(--fg);text-decoration:none;font-size:16px}}li.hot a{{color:var(--acc);font-weight:600}}li b{{color:var(--acc);font-weight:600}}
</style></head><body><main>
<h1>广外通知雷达</h1><div class=m>更新于 {now} · {len(items)} 条</div>
<div class=bar><button data-k="国际项目" class=on>国际项目</button><button data-k="★">★重要</button><button data-k="">全部</button><button data-k="访学 交流 交换 境外 海外">访学</button>
<button data-k="奖学金 助学金 资助 补助">奖助</button><button data-k="竞赛 大赛">竞赛</button><button data-k="会计学院">会计学院</button></div>
<input type=search id=q placeholder="搜索标题或来源">
<ul id=l>{''.join(rows)}</ul></main>
<script>
let k='国际项目';const q=document.getElementById('q');
function f(){{const w=q.value.trim().split(/\\s+/).filter(Boolean);const ks=k?k.split(' '):[];
for(const li of document.querySelectorAll('#l li')){{const t=li.dataset.t+(li.classList.contains('hot')?'★':'');
li.style.display=(w.every(x=>t.includes(x))&&(!ks.length||ks.some(x=>t.includes(x))))?'':'none'}}}}
f();q.oninput=f;document.querySelectorAll('.bar button').forEach(b=>b.onclick=()=>{{document.querySelectorAll('.bar button').forEach(x=>x.classList.remove('on'));b.classList.add('on');k=b.dataset.k;f()}});
</script></body></html>""")


def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {"items": {}, "initialized": False}


def save_state(st):
    temporary = STATE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(temporary, STATE)


def collect_source(source):
    """Keep complete results from earlier pages if a later page fails."""
    name, url = source
    project = source in international.SOURCES
    found, visited, errors = [], set(), []
    current = url
    for _ in range(3 if project else 1):
        if not current or current in visited:
            break
        visited.add(current)
        try:
            page = get(current)
            rows = international.parse_list(current, page) if project else parse_list(current, page)
            if project and not rows:
                raise ValueError("国际项目列表为空，请检查网站结构")
            for row in rows:
                row.setdefault("source", name)
            found.extend(rows)
            current = international.next_page(current, page) if project else None
        except Exception as exc:
            errors.append(f"{current}: {exc}")
            break
    return source, found, errors


def active_opportunity(it, art):
    """Backfill recent opportunities, never flood the inbox with years of archives."""
    today = datetime.now(CST).date()
    if art.get("deadline_iso"):
        return datetime.strptime(art["deadline_iso"], "%Y-%m-%d").date() >= today
    try:
        published = datetime.strptime(it.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        return True  # Missing dates must not silently discard a new opportunity.
    return (today - published).days <= 45


def apply_article(it, art, first=False):
    """A pending delivery survives caps, fetch errors and SMTP failures."""
    previous = it.get("content_hash")
    fingerprint = international.content_hash(it, art)
    if it.get("priority"):
        it["score"] = international.classify(it, art)
    changed = previous is not None and previous != fingerprint
    static_update = it.get("category") in ("ztqk", "hzbx") and (
        changed or (it.get("pending_reason") == "updated" and bool(it.get("pending_hash"))))
    if static_update:
        it["score"] = max(it["score"], 80)
    update_alert = changed or (it.get("pending_reason") == "updated" and bool(it.get("pending_hash")))
    eligible = it.get("score", 0) > 0 and (active_opportunity(it, art) or update_alert)
    if not eligible:
        it.pop("pending_hash", None)
        it.pop("pending_reason", None)
    elif not first and (it.get("new_candidate") or changed):
        if fingerprint != it.get("delivered_hash"):
            it["pending_hash"] = fingerprint
            it["pending_reason"] = "updated" if changed else "new"
            if not changed and it.get("priority") and it.get("date"):
                try:
                    age = (datetime.now(CST).date() - datetime.strptime(it["date"], "%Y-%m-%d").date()).days
                    if age > 7:
                        it["pending_reason"] = "backfill"
                except ValueError:
                    pass
    elif it.get("pending_hash") and fingerprint != it["pending_hash"]:
        it["pending_hash"] = fingerprint
    it["content_hash"] = fingerprint
    it["deadline"] = art.get("deadline", "")
    it["deadline_iso"] = art.get("deadline_iso", "")
    it.pop("new_candidate", None)
    it.pop("detail_error", None)


def pending_items(st):
    return sorted((x for x in st["items"].values() if x.get("pending_hash")),
                  key=lambda x: (-x.get("priority", 0), x.get("found", ""),
                                 -int(re.sub(r"\D", "", x.get("date") or "0")), -x.get("score", 0), x["url"]))


def deliver_pending(st, arts, limit=MAX_MAIL):
    sent, failures = 0, []
    for it in pending_items(st):
        if sent >= limit:
            break
        art = arts.get(it["url"])
        if art is None:
            continue
        try:
            if not send_mail(mail_subject(it, art), mail_html(it, art)):
                raise RuntimeError("邮件配置缺失，通知保留在待发送队列")
        except Exception as exc:
            failures.append(f"{it['url']}: {exc}")
            break  # SMTP outages must not consume the remaining queue.
        it["delivered_hash"] = it.pop("pending_hash")
        it["delivered_at"] = datetime.now(CST).isoformat(timespec="minutes")
        it.pop("pending_reason", None)
        save_state(st)  # Checkpoint successful deliveries before attempting another.
        sent += 1
        time.sleep(1)
    return sent, failures


def main():
    dry = "--dry-run" in sys.argv
    only_international = "--international-only" in sys.argv
    st = load_state()
    first = not st.get("initialized")
    now = datetime.now(CST).isoformat(timespec="minutes")
    sources = international.SOURCES if only_international else SOURCES
    new, observed, errors = [], set(), []
    st.setdefault("source_health", {})
    # The same CMS article may appear under multiple column URLs.
    identities = {urllib.parse.urlparse(x["url"]).path.rsplit("/", 1)[-1]: x["url"]
                  for x in st["items"].values() if column(x["url"]) and column(x["url"])[0] == "internationaloffice"}
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(collect_source, sources))
    for (name, url), items, failures in results:
        st["source_health"][url] = {"checked_at": now, "items": len(items), "errors": failures}
        print(f"[{'失败' if failures else 'ok'}] {name} {len(items)} 条 {str(failures) if failures else ''}")
        if (name, url) in international.SOURCES:
            errors.extend(failures)
        for row in items:
            priority = row.get("priority", 0)
            if not priority and column(row["url"]) not in NOTICE_COLS:
                continue
            if column(row["url"]) and column(row["url"])[0] == "internationaloffice":
                identity = urllib.parse.urlparse(row["url"]).path.rsplit("/", 1)[-1]
                row["url"] = identities.setdefault(identity, row["url"])
            known = st["items"].get(row["url"])
            if known is None:
                row.update(found=now, score=score(row["title"]), new_candidate=True)
                st["items"][row["url"]] = row
                new.append(row)
                known = row
            elif priority:
                was_priority = known.get("priority")
                known.update({k: v for k, v in row.items() if v or k not in known})
                if not was_priority:
                    known["new_candidate"] = True
            observed.add(known["url"])

    todo = {x["url"]: x for x in st["items"].values()
            if x.get("new_candidate") or x.get("pending_hash") or (x.get("priority") and x["url"] in observed)}
    if not only_international:
        for x in [x for x in st["items"].values() if not has_page(x)][:30]:
            todo.setdefault(x["url"], x)
    arts = {}
    def fetch_one(it):
        try:
            art = fetch_article(it["url"])
            if not art["paras"] and not art["atts"]:
                raise ValueError("未取得正文或附件，稍后重试")
            if it.get("priority"):
                art = enrich_article(it, art)
            return it, art, None
        except Exception as exc:
            return it, None, str(exc)
    with ThreadPoolExecutor(max_workers=6) as pool:
        for it, art, failure in pool.map(fetch_one, todo.values()):
            if failure:
                it["detail_error"] = failure
                print("[正文失败]", it["url"], failure)
                if it.get("priority"):
                    errors.append(f"{it['url']}: {failure}")
                continue
            arts[it["url"]] = art
            if not it.get("date") and art["date"]:
                it["date"] = art["date"]
            apply_article(it, art, first)
            if not dry:
                render_article(it, art)
                it["page"] = True
    pending = pending_items(st)
    print(f"新发现 {len(new)} 条，待发送 {len(pending)} 条，本轮上限 {MAX_MAIL} 封")
    for it in pending:
        print(f"[待发送/{it.get('pending_reason')}] {it.get('date', '')} [{it['source']}] {it['title']} 截止:{it.get('deadline', '')}")
    if "--preview-dir" in sys.argv:
        dest = sys.argv[sys.argv.index("--preview-dir") + 1]
        os.makedirs(dest, exist_ok=True)
        for it in pending:
            if it["url"] in arts:
                with open(os.path.join(dest, page_id(it["url"]) + ".html"), "w", encoding="utf-8") as f:
                    f.write(mail_html(it, arts[it["url"]]))
        with open(os.path.join(dest, "verification.json"), "w", encoding="utf-8") as f:
            json.dump({"checked_at": now, "dry_run": dry, "new": len(new), "pending": pending,
                       "source_health": st["source_health"], "errors": errors}, f, ensure_ascii=False, indent=2)
    if dry:
        if errors:
            raise SystemExit(f"验证发现 {len(errors)} 项抓取失败")
        return
    st["initialized"] = True
    save_state(st)
    sent, mail_errors = deliver_pending(st, arts)
    errors.extend(mail_errors)
    # Test mail is opt-in; dispatching a normal run must not create duplicate samples.
    if os.environ.get("SEND_SAMPLE") == "true":
        sample = next((x for x in st["items"].values() if x.get("score") and x["url"] in arts), None)
        if sample:
            if not send_mail("【样例】" + mail_subject(sample, arts[sample["url"]]), mail_html(sample, arts[sample["url"]])):
                errors.append("样例邮件未发送：配置缺失")
    save_state(st)
    render_index(st)
    print(f"本轮已发送 {sent} 封，待下轮发送 {len(pending_items(st))} 封")
    if errors:
        # The existing workflow saves records only on a successful script exit.
        # Recoverable network/SMTP errors must still persist delivery receipts.
        for error in errors:
            safe_error = str(error).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::warning::{safe_error}")
        print(f"发现 {len(errors)} 项可重试异常；未发送通知已保留，详情见上方日志")


if __name__ == "__main__":
    main()
