"""The meeting series catalog, the topic tag vocabulary, and the title rule.

A recurring meeting is titled "<series name> – <short topic>", with the en
dash and its spaces as the separator. A one-off meeting is titled by its
topic alone. The folder name then takes the date stamp as before
("LT Weekly Meeting – Budget Reallocation 10282026").

The model never writes a title as free text. It names a series id from
SERIES (or "one_off"), the other person for a person series, and a short
topic. build_title() makes the title from those parts. That is what keeps
every meeting of one series under one spelling.

A person series uses the first name. A last initial is added only when two
known people share the first name ("Robin C."). The owner is never the
person of a 1:1 series.

TAGS is a fixed, controlled vocabulary of topic tags. The model picks 3 to 5
of them. Anything else it writes is dropped.

The catalog was read off the library on 2026-11-05. Add a series here, not
in a prompt: the prompt is built from this list.
"""
import re
from dataclasses import dataclass, field

EN_DASH = "–"
SEP = f" {EN_DASH} "
ONE_OFF = "one_off"


@dataclass(frozen=True)
class Series:
    id: str
    template: str            # "{person} Weekly Check-in" or "LT Weekly Meeting"
    hint: str                # when to use it, for the prompt
    aliases: tuple = field(default_factory=tuple)   # regexes over an old title

    @property
    def has_person(self) -> bool:
        return "{person}" in self.template

    def name(self, person: str = "") -> str:
        """The series name. A person series with no person drops the slot."""
        if not self.has_person:
            return self.template
        person = (person or "").strip()
        if person:
            return self.template.format(person=person)
        return self.template.replace("{person}", "").strip()


SERIES = (
    Series("weekly_checkin", "{person} Weekly Check-in",
           "the owner's recurring weekly 1:1 check-in with one staff member "
           "(old titles also say 'Check-in', 'Weekly Check-in <Name>', "
           "'Check-in Weekly')",
           (r"\bweekly\s+check[\s-]?in\b", r"\bcheck[\s-]?in\s+weekly\b",
            r"^\S+(?:\s+\S\.?)?\s+check[\s-]?in\b")),
    Series("thrive_checkin", "{person} Thrive Check-in",
           "a Thrive performance check-in (mid-year review conversation) with "
           "one staff member",
           (r"\bthrive\s+check[\s-]?in\b",)),
    Series("thrive_icd", "{person} Thrive Initial Career Dialogue",
           "a Thrive Initial Career Dialogue, also written 'ICD': the "
           "start-of-year goals and career conversation with one staff member",
           (r"\binitial\s+career\s+dialogue\b", r"\bicd\b")),
    Series("thrive_eoy", "{person} Thrive EOY Dialogue",
           "a Thrive end-of-year (EOY) review dialogue with one staff member",
           (r"\beoy\s+dialogue\b", r"\bend[\s-]of[\s-]year\s+dialogue\b")),
    Series("monthly_2x2", "{person} Monthly 2x2",
           "a monthly 2x2 conversation with one staff member",
           (r"\bmonthly\s+2x2\b", r"\b2x2\b")),
    Series("lt_weekly", "LT Weekly Meeting",
           "the weekly Leadership Team (LT) meeting",
           (r"^lt\s+weekly\b", r"^lt\s+meeting\b", r"\bleadership\s+team\s+weekly\b")),
    Series("anthropic_weekly", "Weekly Anthropic Meeting",
           "the weekly partnership meeting or call with Anthropic",
           (r"\bweekly\s+anthropic\b", r"\banthropic\s+weekly\b")),
    Series("km_weekly", "Weekly KM Meeting",
           "the weekly Knowledge Management (KM) team meeting",
           (r"\bweekly\s+km\b", r"^km\s+weekly\b")),
    Series("ds_weekly", "Weekly DS Meeting",
           "the weekly Data Services (DS) team meeting",
           (r"\bweekly\s+ds\b", r"\bweekly\s+check[\s-]?in\s+ds\b")),
    Series("board_regular", "Regular Board Meeting",
           "a regular (voting) meeting of the Board of Education",
           (r"\bregular\s+board\s+meeting\b",)),
    Series("board_cotw", "Committee of the Whole",
           "a Board Committee of the Whole meeting",
           (r"\bcommittee\s+of\s+the\s+whole\b",)),
    Series("board_study", "Board Study Session",
           "a Board study session",
           (r"^(board\s+)?study\s+session\b",)),
    Series("pssp", "PSSP DPSCD",
           "the PowerSchool Special Programs (PSSP, also 'PS Special Programs') "
           "implementation project meetings with the vendor: case management "
           "workflows, discovery workshops, integration, reports, training, "
           "executive updates",
           (r"^pssp\b", r"\bps\s+special\s+programs\b",
            r"\bpowerschool\s+special\s+programs\b")),
    Series("enrollment_review", "Enrollment Data Review",
           "the recurring enrollment data review meeting",
           (r"\benrollment\s+data\s+review\b",)),
)

BY_ID = {s.id: s for s in SERIES}

# (slug, label, hint). Read off the library's summaries on 2026-11-05.
TAGS = (
    ("attendance", "Attendance", "student attendance, chronic absence"),
    ("enrollment", "Enrollment", "enrollment counts, transfers, leavers, projections"),
    ("assessment", "Assessment", "tests, benchmarks, grades, school grading"),
    ("literacy", "Literacy", "reading, phonics, OG intervention"),
    ("math", "Math", "math instruction and intervention"),
    ("special-education", "Special education", "ESE, IEPs, Early On, case management"),
    ("multilingual", "Multilingual learners", "English learners, multilingual education"),
    ("school-performance", "School performance", "school-level results and goals"),
    ("school-programs", "School programs", "program design: redesign, Montessori, CTE"),
    ("budget-finance", "Budget and finance", "budgets, invoices, reallocations, FTE costs"),
    ("grants-funding", "Grants and funding", "grant applications, funders, funding priorities"),
    ("hiring-staffing", "Hiring and staffing", "hiring, onboarding, roles, staffing plans"),
    ("hr-labor", "HR and labor relations", "grievances, contracts, HR policy"),
    ("performance-reviews", "Performance reviews", "Thrive check-ins, EOY reviews, feedback"),
    ("career-growth", "Career growth", "career goals, leadership development, coaching"),
    ("goals-okrs", "Goals and OKRs", "OKRs, goal setting, priorities"),
    ("team-operations", "Team operations", "team structure, workload, process, project management"),
    ("data-systems", "Data systems", "data architecture, integration, PowerSchool, SharePoint"),
    ("reporting-dashboards", "Reporting and dashboards", "reports, dashboards, data requests"),
    ("research-evaluation", "Research and evaluation", "studies, evaluations, research agreements"),
    ("ai-tools", "AI tools", "AI pilots, Claude, AI training"),
    ("professional-learning", "Professional learning", "training sessions, PD, workshops"),
    ("partnerships", "Partnerships and vendors", "outside partners, vendors, RFPs"),
    ("board-governance", "Board and governance", "Board meetings, governance, superintendent"),
    ("policy-compliance", "Policy and compliance", "policy, state reporting, compliance"),
    ("family-community", "Families and community", "parents, families, community outreach"),
    ("personal", "Personal", "personal matters outside work"),
)
TAG_LABELS = {slug: label for slug, label, _ in TAGS}
TAG_MIN, TAG_MAX = 3, 5

_TOPIC_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f–—]')
_DATEISH = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?|\d{8}|\d{1,2}:\d{2})\b")


def catalog_text() -> str:
    """The catalog as the prompt lists it."""
    lines = [f"- {s.id}: \"{s.template.replace('{person}', '<Person>')}\" -- {s.hint}"
             for s in SERIES]
    lines.append(f"- {ONE_OFF}: anything that is not one of the series above")
    return "\n".join(lines)


def tags_text() -> str:
    return "\n".join(f"- {slug}: {hint}" for slug, _, hint in TAGS)


def guess_series(old_title: str):
    """The series an old title names, by the catalog aliases, else None.
    Used to check the model's choice, never to decide it."""
    t = re.sub(r"\s+", " ", str(old_title or "")).strip().lower()
    # the most specific first: Thrive check-in before weekly check-in
    order = ("thrive_icd", "thrive_eoy", "thrive_checkin", "monthly_2x2",
             "ds_weekly", "lt_weekly", "anthropic_weekly", "km_weekly",
             "board_regular", "board_cotw", "board_study", "pssp",
             "enrollment_review", "weekly_checkin")
    for sid in order:
        if any(re.search(a, t) for a in BY_ID[sid].aliases):
            return sid
    return None


def exact_series(old_title: str):
    """(series id, person text) when an old title IS a catalog series name,
    word for word apart from case: 'LT Weekly Meeting', 'Robin Weekly
    Check-in'. A person slot takes one to four words. Else (None, "").
    Code then makes the series choice and the model is not asked."""
    t = re.sub(r"\s+", " ", str(old_title or "")).strip()
    for sr in SERIES:
        if not sr.has_person:
            if t.lower() == sr.template.lower():
                return sr.id, ""
            continue
        pre, post = sr.template.split("{person}")
        rx = (re.escape(pre.strip()) + r"\s*" if pre.strip() else "") + \
            r"(\S+(?:\s+\S+){0,3}?)" + (r"\s+" + re.escape(post.strip()) if post.strip() else "")
        m = re.fullmatch(rx, t, flags=re.I)
        if m and not re.search(r"\b(thrive|weekly|check-?in|dialogue|icd)\b",
                               m.group(1), flags=re.I):
            return sr.id, m.group(1)
    return None, ""


def normalize_series(raw: str) -> str:
    """A series id from the model's SERIES line: an id ('lt_weekly',
    'weekly-checkin'), or a catalog name, with or without its person
    ('LT Weekly Meeting', 'Weekly Check-in', 'Robin Weekly Check-in').
    Anything else is ONE_OFF."""
    s = re.sub(r"[`'\"*]", "", str(raw or "")).strip().rstrip(".,;:")
    if not s:
        return ONE_OFF
    tok = s.split()[0].lower().rstrip(".,;:")
    for k in (tok, tok.replace("-", "_")):
        if k in BY_ID:
            return k
    low = re.sub(r"\s+", " ", s).lower()
    for sr in SERIES:
        if low == sr.template.replace("{person}", "").strip().lower():
            return sr.id
    sid, _ = exact_series(s)
    return sid or ONE_OFF


def normalize_tags(raw) -> list:
    """Up to TAG_MAX slugs from the vocabulary, in the model's order. A
    label, a slug, or a slug with spaces all match. Others are dropped."""
    if isinstance(raw, str):
        raw = re.split(r"[,;\n]", raw)
    by_label = {label.lower(): slug for slug, label, _ in TAGS}
    out = []
    for t in raw or []:
        k = re.sub(r"[`'\"*#\[\]]", "", str(t)).strip().lower().rstrip(".")
        slug = (k if k in TAG_LABELS else by_label.get(k)
                or (k.replace(" ", "-") if k.replace(" ", "-") in TAG_LABELS else None))
        if slug and slug not in out:
            out.append(slug)
    return out[:TAG_MAX]


def clean_topic(topic: str, avoid=(), max_words: int = 6) -> str:
    """A short topic: no path or wildcard characters, no dashes, no dates,
    no words that repeat the series name or the person, Title Case kept as
    the model wrote it, at most `max_words` words."""
    t = _TOPIC_BAD.sub(" ", str(topic or ""))
    t = _DATEISH.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" .,;-")
    for a in sorted((x for x in avoid if x), key=len, reverse=True):
        t = re.sub(r"(?i)(?<!\w)" + re.escape(a) + r"(?!\w)", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .,;-&")
    t = re.sub(r"^(?:and|with|on|of|for|the|-)\s+", "", t, flags=re.I)
    t = re.sub(r"\s+(?:and|with|on|of|for|the|-)$", "", t, flags=re.I)
    words = t.split()
    if words and words[-1].lower() in ("and", "with", "on", "of", "for", "the"):
        words = words[:-1]
    if len(words) > max_words:
        words = words[:max_words]
        while words and words[-1].lower() in ("and", "with", "on", "of", "for", "the", "&"):
            words.pop()
    return title_case(" ".join(words))


_SMALL = {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or",
          "the", "to", "vs", "via", "with"}


def _spellings() -> dict:
    """{UPPER CASE: spelling} for each acronym and mixed-case name the tag
    list and the series catalog write: 'IEP', 'OKR', 'HR', 'AI', 'LT',
    'PSSP', 'PowerSchool'. A plural the lists write ('IEPs', 'RFPs') gives
    its singular. Read from the lists, so a new tag or series adds its own."""
    text = " ".join([f"{label} {hint}" for _, label, hint in TAGS]
                    + [f"{sr.template} {sr.hint}" for sr in SERIES]
                    + list(_MORE_ACRONYMS))
    out = {}
    for w in re.findall(r"(?<!\w)[A-Za-z][A-Za-z0-9]*", text):
        if not any(c.isupper() for c in w[1:]):
            continue
        if len(w) > 3 and w[-1] == "s" and w[:-1].isupper():
            w = w[:-1]
        out.setdefault(w.upper(), w)
    return out


_SPELLINGS = None
# school acronyms a meeting topic uses that no tag or series writes. Only
# ones that are never an English word: 'it' and 'sip' stay words.
_MORE_ACRONYMS = ("PLC", "PLCs", "MTSS", "SEL", "ELA", "ELL", "ESL", "KPI", "KPIs")


def _known(part: str):
    """The catalog spelling of one word written without inner capitals
    ('iep' -> 'IEP', "iep's" -> "IEP's", 'okrs' -> 'OKRs'), else None. The
    plural 's' counts only past three letters, so 'hrs' stays a word."""
    global _SPELLINGS
    if _SPELLINGS is None:
        _SPELLINGS = _spellings()
    m = re.fullmatch(r"(\W*)([\w']+?)(\W*)", part)
    if not m:
        return None
    lead, core, tail = m.groups()
    tries = [(core, "")]
    if core.lower().endswith("'s"):
        tries.append((core[:-2], core[-2:].lower()))
    elif len(core) > 3 and core.lower().endswith("s"):
        tries.append((core[:-1], "s"))
    for stem, suffix in tries:
        hit = _SPELLINGS.get(stem.upper())
        if hit:
            return lead + hit + suffix + tail
    return None


def _cap(w: str) -> str:
    """The first letter in capitals, and the letter after an O' or D'
    prefix ("o'brien" -> "O'Brien")."""
    w = w[:1].upper() + w[1:]
    return re.sub(r"^(\W*[OD]')([a-z])", lambda m: m.group(1) + m.group(2).upper(), w)


def title_case(t: str) -> str:
    """Title Case with the small words lower-case after the first word.
    A word already holding a capital past its first letter (an acronym
    such as 'ESE', 'OKRs', 'PlayLab') is kept as written. A word the model
    wrote without one takes the tag list's or the catalog's spelling when
    they write it ('iep' -> 'IEP', 'powerschool' -> 'PowerSchool'), each
    part of a hyphenated or slashed word on its own."""
    s = " ".join(_case_word(w, i == 0) for i, w in enumerate(str(t or "").split()))
    # program names whose second word is a small word ('Early On' is the
    # Part C program, 'Transfer In' a workflow): the small-word rule above
    # must not lower-case them
    for name in _PROPER:
        s = re.sub(rf"\b{re.escape(name)}\b", name, s, flags=re.IGNORECASE)
    return s


_PROPER = ("Early On", "Transfer In")


def _case_word(w: str, first: bool) -> str:
    """One word of title_case. A slash parts two words, and each is cased
    on its own ('iep/ese' read as one word and came out 'Iep/ese')."""
    if "/" in w.strip("/"):
        return "/".join(_case_word(p, first) if p else p for p in w.split("/"))
    if any(c.isupper() for c in w[1:]) or any(c.isdigit() for c in w):
        return w
    parts = w.split("-")
    known = [_known(p) for p in parts]
    if any(known):
        return "-".join(k or _cap(p) for k, p in zip(known, parts))
    if not first and w.lower() in _SMALL:
        return w.lower()
    return _cap(w)


def _first(name: str) -> str:
    return (str(name or "").split() or [""])[0]


def _initial(name: str) -> str:
    parts = str(name or "").split()
    return (parts[-1][0].upper() + ".") if len(parts) > 1 and parts[-1][0].isalpha() else ""


def person_label(full: str, known=()) -> str:
    """The first name, with a last initial only when another known person
    shares that first name: 'Robin C.' beside another known 'Robin A.'.
    A name that is already short ('Robin C.') stays as it is."""
    full = re.sub(r"\s+", " ", str(full or "")).strip()
    if not full:
        return ""
    if re.fullmatch(r"\S+\s+[A-Z]\.?", full):          # 'Robin C' / 'Robin C.'
        return full if full.endswith(".") else full + "."
    first = _first(full)
    others = {k for k in known if k and k != full and _first(k).lower() == first.lower()}
    ini = _initial(full)
    return f"{first} {ini}" if others and ini else first


def resolve_person(raw: str, roster=(), known=(), me=None) -> str:
    """The full name the model's PERSON line means, else the raw text.

    `roster` holds this meeting's named speakers, `known` every enrolled
    person, `me` the owner. A full-name match wins, then a unique
    first-name match in the roster, then a unique one among the known.
    'A and B' resolves each side. 'none' and the owner resolve to ''."""
    s = re.sub(r"\s+", " ", str(raw or "")).strip(" .,;[]\"'")
    if not s or s.lower() in ("none", "n/a", "na", "unknown", "-"):
        return ""
    parts = re.split(r"\s+(?:and|&)\s+|,\s*", s)
    if len(parts) > 1:
        got = [resolve_person(p, roster, known, me) for p in parts]
        got = [g for g in got if g]
        return " and ".join(dict.fromkeys(got))
    roster = [r for r in roster if r and r != me]
    known = [k for k in known if k and k != me]
    if me and s.lower() == str(me).lower():
        return ""
    for pool in (roster, known):
        for n in pool:
            if n.lower() == s.lower():
                return n
    short = re.fullmatch(r"(\S+)\s+([A-Za-z])\.?", s)     # 'Robin C.'
    for pool in (roster, known):
        if short:
            hits = [n for n in pool if _first(n).lower() == short.group(1).lower()
                    and _initial(n)[:1].lower() == short.group(2).lower()]
        else:
            hits = [n for n in pool if _first(n).lower() == _first(s).lower()]
        hits = list(dict.fromkeys(hits))
        if len(hits) == 1:
            return hits[0]
    if me and _first(s).lower() == _first(me).lower() and not short and len(s.split()) == 1:
        return ""
    return s


def label_people(full: str, known=()) -> str:
    """person_label over 'A and B'."""
    return " and ".join(person_label(p, known) for p in
                        re.split(r"\s+and\s+", full) if p.strip())


def build_title(series_id: str, topic: str, person: str = "") -> str:
    """The title without its date stamp. A series: '<name> – <topic>', or
    the name alone when there is no topic. A one-off: the topic, else
    'Untitled Meeting'."""
    s = BY_ID.get(series_id)
    if s is None:
        t = clean_topic(topic, max_words=8)
        return t or "Untitled Meeting"
    name = s.name(person)
    avoid = [s.template.replace("{person}", "").strip()] + (
        [person] + [_first(p) for p in re.split(r"\s+and\s+", person)] if person else [])
    # a topic that opens with the series' own words ('LT Weekly Budget')
    # repeats the name: those leading words go
    own = {w.lower() for w in re.findall(r"[\w-]+", s.template.replace("{person}", ""))}
    own |= {"weekly", "meeting", "call", "check-in", "checkin", "thrive", "dialogue",
            "monthly", "session"}
    words = str(topic or "").split()
    while words and re.sub(r"[^\w-]", "", words[0]).lower() in own:
        words.pop(0)
    t = clean_topic(" ".join(words), avoid=avoid)
    return f"{name}{SEP}{t}" if t else name


def split_title(title: str):
    """(series name, topic) for a title in the '<series> – <topic>' form,
    else (None, title)."""
    if SEP in str(title):
        a, b = str(title).split(SEP, 1)
        return a, b
    return None, str(title)
