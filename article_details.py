"""Conservative, side-effect-free deadline enrichment for notification bodies.

Only the fetched body is evidence: titles and attachment filenames are not
used to infer deadlines. Attachments are linked by the caller, not parsed here.
"""

from datetime import date
import re


_DATE = re.compile(
    r"(?<!\d)(?:(?P<iy>\d{4})[-/](?P<im>\d{1,2})[-/](?P<id>\d{1,2})(?!\d)"
    r"|(?:(?P<cy>\d{4})年)?(?P<cm>\d{1,2})月(?P<cd>\d{1,2})日"
    r"|(?P<short>\d{1,2})日)"
    r"(?:\s*[（(](?:星期|周)?[一二三四五六日天][）)])?"
    r"(?:\s*(?P<period>上午|中午|下午|晚上)?\s*(?P<hour>\d{1,2})"
    r"(?:(?:[:：])(?P<minute>\d{2})|[时点](?:(?P<cnminute>\d{1,2})分)?))?"
)
_ACTION = re.compile(r"报名|申请|申报|提交|报送|递交|发送.{0,8}(?:材料|申请|报名)")
_DEADLINE = re.compile(r"截止|截至|最[迟晚]|不[晚迟]于|之前|以前|日前|前(?:提交|报名|申请|报送|递交|发送|将|把|完成)|前\s*$")
_RANGE = re.compile(r"^\s*(?:起)?\s*[-—–至到~～]\s*$")
_EXCLUDED = re.compile(r"公示|异议|申诉|复议|投诉|举报|结果公告|名单公布")
_ACTIVITY = re.compile(r"(?:活动|项目|交流|访学|开课|课程|举办|出访|行程|面试|考试|宣讲)(?:的)?(?:时间|日期|安排)")
_HEADING = re.compile(r"(?:报名|申请|申报|材料提交|提交材料)(?:时间|期限|截止时间|截止日期)\s*[：:]?$")
_REGISTRATION_PERIOD = re.compile(
    r"(?:报名|申请|申报|提交|报送|递交)(?:材料)?(?:的)?(?:时间|期限|期间|日期|时段)"
    r"|(?:报名|申请|申报|提交|报送|递交)[：:为自从]"
)
_CLOCK_RANGE_END = re.compile(r"^\s*[-—–至到~～]\s*(\d{1,2})[:：](\d{2})(?!\d)")


def _publication_date(it, art):
    for value in (art.get("date"), it.get("date")):
        try:
            return date.fromisoformat(str(value)[:10])
        except (TypeError, ValueError):
            pass
    return None


def _date_value(match, publication, previous=None):
    """Return (date, clock), preserving explicit years and bounded rollover."""
    raw_year = match.group("iy") or match.group("cy")
    raw_month = match.group("im") or match.group("cm")
    raw_day = match.group("id") or match.group("cd") or match.group("short")
    if not raw_month and previous is None:
        return None
    month = int(raw_month) if raw_month else previous.month
    day = int(raw_day)
    if raw_year:
        year = int(raw_year)
    elif previous is not None:
        year = previous.year
        if raw_month and month < previous.month:
            year += 1
    elif publication is not None:
        year = publication.year
    else:
        return None
    try:
        result = date(year, month, day)
        if not raw_year and previous is None and publication is not None:
            # A winter notice can refer to the following spring. A nearby
            # already-passed date stays in the publication year, not next year.
            if (publication - result).days > 183:
                rolled = date(year + 1, month, day)
                if 0 <= (rolled - publication).days <= 183:
                    result = rolled
    except ValueError:
        return None
    clock = ""
    if match.group("hour"):
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or match.group("cnminute") or 0)
        period = match.group("period")
        if period in ("下午", "晚上") and hour < 12:
            hour += 12
        elif period == "上午" and hour == 12:
            hour = 0
        elif period == "中午" and hour < 11:
            hour += 12
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None
        clock = f"{hour:02d}:{minute:02d}"
    return result, clock


def _candidate(clause, publication, heading=""):
    context = (heading + "：" + clause) if heading else clause
    if _EXCLUDED.search(context) or not _ACTION.search(context):
        return None
    # Separate activity and registration clauses before this function, and
    # reject ambiguous clauses that still mix these two kinds of dates.
    if _ACTIVITY.search(clause):
        return None
    matches = list(_DATE.finditer(clause))
    if not matches:
        return None
    ranges = []
    for first, last in zip(matches, matches[1:]):
        if _RANGE.fullmatch(clause[first.end():last.start()]):
            start = _date_value(first, publication)
            end = _date_value(last, publication, start[0] if start else None)
            if start is None or end is None or end[0] < start[0]:
                return None
            # A range is a deadline only when explicitly a registration,
            # application, or submission period.
            if not (_REGISTRATION_PERIOD.search(context)
                    or _ACTION.search(clause[last.end():])):
                continue
            ranges.append((end[0], end[1], context))
    if ranges:
        return ranges[0] if len(ranges) == 1 and len(matches) == 2 else None
    if len(matches) != 1:
        return None
    parsed = _date_value(matches[0], publication)
    clock_end = _CLOCK_RANGE_END.match(clause[matches[0].end():])
    if parsed and parsed[1] and clock_end and _REGISTRATION_PERIOD.search(context):
        hour, minute = map(int, clock_end.groups())
        if hour > 23 or minute > 59:
            return None
        clock = f"{hour:02d}:{minute:02d}"
        return (parsed[0], clock, context) if clock >= parsed[1] else None
    if not _DEADLINE.search(context):
        return None
    return (parsed[0], parsed[1], context) if parsed else None


def enrich_article(it, art):
    """Return a new article dict with verified deadline and attachment notes.

    ``deadline_iso`` is the date to use for expiry checks. ``deadline`` is a
    Chinese display label including the verified/inferred year and any clock.
    Multiple distinct deadlines are left unresolved instead of choosing a
    deadline for the wrong stage. No current-clock or network dependency.
    """
    result = dict(art)
    result.update(deadline="", deadline_iso="", deadline_context="",
                  deadline_time="", attachment_note="")
    publication = _publication_date(it, art)
    candidates = []
    heading = ""
    for paragraph in art.get("paras", []):
        text = str(paragraph).strip()
        if len(text) <= 35 and _HEADING.search(text) and not _DATE.search(text):
            heading = text.rstrip("：:")
            continue
        for clause in re.split(r"[。；;！!\n，,]", text):
            clause = clause.strip(" ｜")
            if clause:
                candidate = _candidate(clause, publication, heading)
                if candidate:
                    candidates.append(candidate)
        # Only the immediately following paragraph inherits a time heading.
        heading = ""
    unique = {(value[0], value[1]) for value in candidates}
    if len(unique) == 1:
        value, clock, context = candidates[0]
        result.update(
            deadline=f"{value.year}年{value.month}月{value.day}日" + (f" {clock}" if clock else ""),
            deadline_iso=value.isoformat(),
            deadline_time=clock,
            deadline_context=context,
        )
    if art.get("atts") and not result["deadline_iso"]:
        result["attachment_note"] = (
            "网页正文未能确认唯一的报名或申请截止时间；附件内容尚未解析，"
            "请查看附件及原通知，以其中的具体要求为准。"
        )
    return result
