#!/usr/bin/env python3
"""广外通知雷达：定时抓取广外各部门通知，有新通知就推送到微信（PushPlus）。

用法
  python gw_notify.py            正常运行：抓取 → 找出新通知 → 推送 → 更新网页
  python gw_notify.py --dry-run  只抓取和打印，不推送、不保存
环境变量
  PUSHPLUS_TOKEN   PushPlus 的 token（不设就只打印不推送）
"""
import json, os, re, sys, time, html, ssl, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "seen.json")
SITE = os.path.join(HERE, "docs", "index.html")
CST = timezone(timedelta(hours=8))

# 来源：名称, 列表页
SOURCES = [
    ("学生工作部", "https://nstu.gdufs.edu.cn/"),
    ("会计学院·学生工作", "https://kjxy.gdufs.edu.cn/xsgz/xsdt.htm"),
    ("会计学院·国际交流", "https://kjxy.gdufs.edu.cn/gjjl/jlxm.htm"),
    ("会计学院·首页", "https://kjxy.gdufs.edu.cn/"),
    ("国际交流合作部·通知", "https://internationaloffice.gdufs.edu.cn/tzgg1/gjxm.htm"),
    ("国际交流合作部·公示", "https://internationaloffice.gdufs.edu.cn/gs/gjxm.htm"),
    ("国际交流合作部·首页", "https://internationaloffice.gdufs.edu.cn/"),
    ("教务部·通知", "https://jwb.gdufs.edu.cn/xwtz/tz.htm"),
    ("教务部·教学通知", "https://jwb.gdufs.edu.cn/xwtz/jxtz.htm"),
    ("校团委", "https://youth.gdufs.edu.cn/"),
    ("创新创业学院", "https://gwcxcy.gdufs.edu.cn/"),
]

# 命中这些词的通知会加星、排前面
HOT = ["先到先得", "额满即止", "名额有限", "截止", "报名", "选拔", "申请", "资助", "奖学金",
       "助学金", "访学", "交流项目", "交换", "夏令营", "冬令营", "海外", "境外", "新生",
       "2026级", "大一", "竞赛", "大赛", "推免", "保研", "PRIME"]
# 纯新闻稿的特征，降权（仍然记录，但不加星）
NEWSY = ["圆满", "举办", "举行", "召开", "来访", "开展主题", "顺利", "成功"]

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def get(url, tries=3):
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:  # 学校网站偶尔断连，重试
            err = e
            time.sleep(2 + 3 * i)
    raise err


DATE_RE = re.compile(r"(20\d\d)\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")


def parse_list(base, page):
    """解析博达(VSB)站群的列表页：链接形如 info/栏目号/文章号.htm"""
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
        title = DATE_RE.split(title)[0].strip(" /-")  # 有的站把日期和摘要拼在标题后面
        if len(title) < 5:
            continue
        d = DATE_RE.search(m.group(2) + m.group(3))
        date = "%s-%02d-%02d" % (d.group(1), int(d.group(2)), int(d.group(3))) if d else ""
        seen.add(url)
        out.append({"title": title, "url": url, "date": date})
    return out


def article_text(url):
    page = get(url)
    body = re.search(r'<div[^>]+(?:id="vsb_content|class="v_news_content)[^>]*>(.*?)</div>\s*</div>', page, re.S)
    txt = body.group(1) if body else page
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", "", txt, flags=re.S)
    txt = html.unescape(re.sub(r"<[^>]+>", " ", txt))
    return re.sub(r"\s+", " ", txt)


def key_lines(txt, limit=3):
    """从正文里摘出和截止、名额有关的句子"""
    hits = []
    for sent in re.split(r"[。；;！!\n]", txt):
        s = sent.strip()
        if 6 < len(s) < 140 and re.search(r"截止|前提交|之前|先到先得|额满|名额|报名时间|申请时间", s):
            if s not in hits:
                hits.append(s)
        if len(hits) >= limit:
            break
    return hits


def score(title):
    s = sum(1 for k in HOT if k in title)
    if any(k in title for k in NEWSY):
        s = 0
    return s


def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {"items": {}, "initialized": False}


def save_state(st):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def push(title, content_html):
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if not token:
        print("[未设置 PUSHPLUS_TOKEN，跳过推送]")
        return
    data = json.dumps({"token": token, "title": title, "content": content_html,
                       "template": "html"}).encode()
    req = urllib.request.Request("https://www.pushplus.plus/send", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        print("推送结果", r.read().decode()[:200])


def render_site(st):
    items = sorted(st["items"].values(), key=lambda x: (x.get("date") or x["found"][:10], x["found"]), reverse=True)
    rows = []
    for it in items[:400]:
        star = "★ " if it.get("score", 0) else ""
        keys = "".join(f"<div class=k>{html.escape(k)}</div>" for k in it.get("keys", []))
        rows.append(
            f'<li class="{"hot" if it.get("score",0) else ""}" data-t="{html.escape(it["title"] + it["source"])}">'
            f'<a href="{it["url"]}" target="_blank">{star}{html.escape(it["title"])}</a>'
            f'<div class=m>{html.escape(it["source"])} · {it.get("date") or "无日期"} · 发现于 {it["found"][:16]}</div>{keys}</li>')
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
    os.makedirs(os.path.dirname(SITE), exist_ok=True)
    with open(SITE, "w", encoding="utf-8") as f:
        f.write(f"""<!doctype html><html lang=zh-CN><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>广外通知雷达</title>
<style>
:root{{--bg:#fafaf7;--fg:#1d1d1b;--mut:#77756f;--acc:#b4321e;--line:#e6e3dc}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe7;--mut:#9a978f;--acc:#ff7a5c;--line:#2b2a28}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:760px;margin:0 auto;padding:20px 16px 60px}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:var(--mut);font-size:13px;margin-bottom:14px}}
input{{width:100%;box-sizing:border-box;padding:10px 12px;font-size:15px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--fg)}}
label{{display:inline-block;margin:10px 0;color:var(--mut);font-size:14px}}
ul{{list-style:none;padding:0;margin:0}} li{{padding:12px 0;border-bottom:1px solid var(--line)}}
a{{color:var(--fg);text-decoration:none}} li.hot a{{color:var(--acc);font-weight:600}}
.m{{color:var(--mut);font-size:12px;margin-top:2px}} .k{{font-size:13px;margin-top:4px;padding-left:8px;border-left:2px solid var(--acc)}}
</style></head><body><main>
<h1>广外通知雷达</h1><div class=sub>更新于 {now} · 共 {len(items)} 条 · ★ 为含报名、资助、访学等关键词的通知</div>
<input id=q placeholder="搜索标题或来源，如 访学 奖学金 会计学院">
<label><input type=checkbox id=h style="width:auto"> 只看 ★</label>
<ul id=l>{''.join(rows)}</ul></main>
<script>
const q=document.getElementById('q'),h=document.getElementById('h');
function f(){{const v=q.value.trim().split(/\\s+/).filter(Boolean);for(const li of document.querySelectorAll('#l li')){{
const t=li.dataset.t;li.style.display=(v.every(w=>t.includes(w))&&(!h.checked||li.classList.contains('hot')))?'':'none'}}}}
q.oninput=f;h.onchange=f;
</script></body></html>""")


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
            it.update(source=name, found=now, score=score(it["title"]), keys=[])
            st["items"][it["url"]] = it
            new.append(it)

    first = not st.get("initialized")
    # 首次运行只建立底库，不推送，避免一次推几百条旧通知
    if not first:
        for it in new:
            if it["score"]:
                try:
                    it["keys"] = key_lines(article_text(it["url"]))
                except Exception as e:
                    print("[正文失败]", it["url"], e)

    new.sort(key=lambda x: -x["score"])
    print(f"\n新通知 {len(new)} 条{'（首次运行，只建底库）' if first else ''}")
    for it in new[:30]:
        print(("★ " if it["score"] else "  ") + f"{it['date']} [{it['source']}] {it['title']}")
        for k in it["keys"]:
            print("     ·", k)

    if dry:
        return
    if new and not first:
        hot = [x for x in new if x["score"]]
        title = (f"★ {hot[0]['title'][:40]}" if hot else f"广外新通知 {len(new)} 条")
        if len(new) > 1:
            title += f" 等{len(new)}条"
        body = []
        for it in new[:20]:
            color = "#b4321e" if it["score"] else "#222"
            keys = "".join(f"<div style='font-size:13px;color:#555;margin:2px 0 0 8px'>· {html.escape(k)}</div>" for k in it["keys"])
            body.append(f"<p style='margin:10px 0'><a href='{it['url']}' style='color:{color};font-weight:{600 if it['score'] else 400}'>"
                        f"{'★ ' if it['score'] else ''}{html.escape(it['title'])}</a><br>"
                        f"<span style='font-size:12px;color:#888'>{html.escape(it['source'])} · {it['date']}</span>{keys}</p>")
        push(title, "".join(body))
    st["initialized"] = True
    save_state(st)
    render_site(st)


if __name__ == "__main__":
    main()
