#!/usr/bin/env python3
"""广外通知雷达：定时抓取广外各部门通知，有新通知就推送到微信（PushPlus）。

设计原则（手机优先）
- 重要通知（★）一条一推：微信卡片上直接看到简短标题和截止日期
- 点开推送就是排好版的全文、关键信息、附件，不需要再跳去学校电脑版网页
- 每条通知另存一份手机版页面在 docs/n/ 下，网页列表也链接到这里

用法
  python gw_notify.py            正常运行
  python gw_notify.py --dry-run  只抓取和打印，不推送、不保存
环境变量
  PUSHPLUS_TOKEN   PushPlus 的 token（不设就只打印不推送）
  SITE_URL         手机版页面的网址前缀，默认 https://guoguo5202015.github.io/gw-radar
"""
import json, os, re, sys, time, html, urllib.request, urllib.parse
from html.parser import HTMLParser
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "seen.json")
DOCS = os.path.join(HERE, "docs")
SITE_URL = os.environ.get("SITE_URL", "https://guoguo5202015.github.io/gw-radar").rstrip("/")
CST = timezone(timedelta(hours=8))

SOURCES = [
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
NEWSY = ["圆满", "举办", "举行", "召开", "来访", "开展主题", "顺利", "成功", "喜报", "专栏"]

MAX_SINGLE_PUSH = 6       # 一轮最多单独推几条★，其余并进汇总
PUSH_GAP = 13             # PushPlus 限制每分钟5条

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
def score(title):
    if any(k in title for k in NEWSY):
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


def push_html(it, art):
    """PushPlus 正文：手机上直接可读"""
    esc = html.escape
    st = "font-size:16px;line-height:1.7;margin:0 0 10px"
    first = (f"截止 {art['deadline']}｜" if art["deadline"] else "") + f"{it['source']} · {it.get('date','')}"
    parts = [f"<p style='{st};color:#888;font-size:13px'>{esc(first)}</p>"]
    if art["keys"]:
        parts.append("<div style='background:#f3ece4;border-radius:8px;padding:10px 12px;margin:0 0 14px'>" +
                     "".join(f"<div style='font-size:15px;margin:3px 0'>· {esc(k)}</div>" for k in art["keys"]) + "</div>")
    total = 0
    for x in art["paras"]:
        if total > 3500:
            parts.append(f"<p style='{st};color:#888'>……全文较长，点下面链接看完整版</p>")
            break
        parts.append(f"<p style='{st}'>{esc(x)}</p>")
        total += len(x)
    for n, u in art["atts"]:
        parts.append(f"<p style='{st}'><a href='{esc(u)}'>📎 {esc(n)}</a></p>")
    parts.append(f"<p style='{st}'><a href='{page_url(it)}'>手机版全文</a>　<a href='{esc(it['url'])}'>学校原网页</a></p>")
    return "".join(parts)


def push(title, content_html):
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        print("[未设置 PUSHPLUS_TOKEN，跳过推送]", title)
        return
    data = json.dumps({"token": token, "title": title, "content": content_html, "template": "html"}).encode()
    req = urllib.request.Request("https://www.pushplus.plus/send", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        print("推送结果", r.read().decode()[:200])


def render_index(st):
    esc = html.escape
    items = sorted(st["items"].values(), key=lambda x: (x.get("date") or "0000", x["found"]), reverse=True)
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
<div class=bar><button data-k="★" class=on>★重要</button><button data-k="">全部</button><button data-k="访学 交流 交换 境外 海外">访学</button>
<button data-k="奖学金 助学金 资助 补助">奖助</button><button data-k="竞赛 大赛">竞赛</button><button data-k="会计学院">会计学院</button></div>
<input type=search id=q placeholder="搜索标题或来源">
<ul id=l>{''.join(rows)}</ul></main>
<script>
let k='★';const q=document.getElementById('q');
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
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def main():
    dry = "--dry-run" in sys.argv
    st = load_state()
    now = datetime.now(CST).isoformat(timespec="minutes")
    new = []
    for name, url in SOURCES:
        try:
            items = parse_list(url, get(url))
        except Exception as e:
            print(f"[失败] {name} {url} {e}")
            continue
        print(f"[ok] {name} {len(items)} 条")
        for it in items:
            if it["url"] in st["items"]:
                continue
            it.update(source=name, found=now, score=score(it["title"]))
            st["items"][it["url"]] = it
            new.append(it)

    first = not st.get("initialized")
    arts = {}
    # 新通知全部抓正文；旧通知补做手机版页面（每轮最多30篇，慢慢补齐）
    todo = [x for x in new] + [x for x in st["items"].values() if not has_page(x) and x not in new][:40]
    for it in todo:
        try:
            art = fetch_article(it["url"])
            arts[it["url"]] = art
            it["deadline"] = art["deadline"]
            if not it.get("date") and art["date"]:
                it["date"] = art["date"]
            if not dry:
                render_article(it, art)
                it["page"] = True
        except Exception as e:
            print("[正文失败]", it["url"], e)

    new.sort(key=lambda x: -x["score"])
    print(f"\n新通知 {len(new)} 条{'（首次运行，只建底库）' if first else ''}")
    for it in new[:30]:
        a = arts.get(it["url"], {})
        print(("★ " if it["score"] else "  ") + f"{it['date']} [{it['source']}] {it['title']}  截止:{a.get('deadline','')}")

    if new and not first and not dry:
        hot = [x for x in new if x["score"] and x["url"] in arts][:MAX_SINGLE_PUSH]
        rest = [x for x in new if x not in hot]
        for i, it in enumerate(hot):
            if i:
                time.sleep(PUSH_GAP)
            art = arts[it["url"]]
            md = re.match(r"(\d{1,2})月(\d{1,2})日", art["deadline"] or "")
            title = "★" + (f"{md.group(1)}/{md.group(2)}截止｜" if md else "") + short_title(it["title"], 16 if md else 22)
            push(title, push_html(it, art))
        if rest:
            if hot:
                time.sleep(PUSH_GAP)
            body = "".join(
                f"<p style='font-size:16px;line-height:1.6;margin:0 0 12px'><a href='{page_url(x) if has_page(x) else x['url']}'>"
                f"{html.escape(x['title'])}</a><br><span style='font-size:13px;color:#888'>{html.escape(x['source'])} · {x['date']}</span></p>"
                for x in rest[:25])
            push(f"其他新通知 {len(rest)} 条", f"<p style='font-size:13px;color:#888'>{html.escape(rest[0]['source'])} 等</p>" + body)

    if dry:
        return
    st["initialized"] = True
    save_state(st)
    render_index(st)


if __name__ == "__main__":
    main()
