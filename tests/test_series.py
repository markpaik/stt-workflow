"""stt/series.py: the series catalog, the tag vocabulary, and the title
rule. Every name here is synthetic."""
from stt import series

DASH = "–"


def test_a_series_title_is_the_name_an_en_dash_and_the_topic():
    assert (series.build_title("lt_weekly", "Budget Reallocation")
            == f"LT Weekly Meeting {DASH} Budget Reallocation")
    assert (series.build_title("weekly_checkin", "Hiring Plan", "Robin")
            == f"Robin Weekly Check-in {DASH} Hiring Plan")
    # no topic: the series name alone
    assert series.build_title("board_cotw", "") == "Committee of the Whole"


def test_the_topic_never_repeats_the_series_name_person_or_a_date():
    assert (series.build_title("lt_weekly", "LT Weekly Budget Reallocation")
            == f"LT Weekly Meeting {DASH} Budget Reallocation")
    assert (series.build_title("weekly_checkin", "Weekly Check-in Robin Hiring 9.14", "Robin")
            == f"Robin Weekly Check-in {DASH} Hiring")
    t = series.build_title("one_off", "Vendor Review: Phase 2 / Pricing 10-5")
    assert ":" not in t and "/" not in t and "10-5" not in t


def test_topics_are_title_case_with_small_words_lower_and_acronyms_kept():
    assert (series.build_title("weekly_checkin", "enrollment targets And staffing", "Robin")
            == f"Robin Weekly Check-in {DASH} Enrollment Targets and Staffing")
    assert series.title_case("ESE survey for OKRs") == "ESE Survey for OKRs"


def test_one_off_titles_are_capped_and_never_empty():
    long = "Planning the Big District Pilot Launch With Many Partner Organizations Today"
    assert len(series.build_title("one_off", long).split()) <= 8
    assert series.build_title("one_off", "") == "Untitled Meeting"
    assert series.build_title("one_off", "Budget Review") == "Budget Review"


def test_person_label_adds_a_last_initial_only_on_a_shared_first_name():
    known = ["Robin Ash", "Robin Cole", "Dana Fox"]
    assert series.person_label("Robin Cole", known) == "Robin C."
    assert series.person_label("Dana Fox", known) == "Dana"
    assert series.person_label("Robin C", known) == "Robin C."
    assert series.label_people("Dana Fox and Robin Cole", known) == "Dana and Robin C."


def test_resolve_person_prefers_the_roster_and_never_the_owner():
    roster = ["Robin Cole", "Owen Park"]
    known = ["Robin Ash", "Robin Cole", "Owen Park", "Dana Fox"]
    me = "Owen Park"
    assert series.resolve_person("Robin", roster, known, me) == "Robin Cole"
    assert series.resolve_person("Robin C.", [], known, me) == "Robin Cole"
    assert series.resolve_person("Dana", roster, known, me) == "Dana Fox"
    assert series.resolve_person("Owen Park", roster, known, me) == ""
    assert series.resolve_person("none", roster, known, me) == ""
    assert series.resolve_person("Kim", roster, known, me) == "Kim"
    assert (series.resolve_person("Robin and Dana", roster, known, me)
            == "Robin Cole and Dana Fox")


def test_guess_series_reads_old_title_variants():
    g = series.guess_series
    assert g("Robin Check-in 10-5") == "weekly_checkin"
    assert g("Weekly Check-in Robin 9.17") == "weekly_checkin"
    assert g("Robin Check-in Weekly") == "weekly_checkin"
    assert g("Thrive Check-in Robin") == "thrive_checkin"
    assert g("Robin ICD") == "thrive_icd"
    assert g("Robin Thrive Initial Career Dialogue") == "thrive_icd"
    assert g("Robin Thrive EOY Dialogue") == "thrive_eoy"
    assert g("Weekly Check-in DS") == "ds_weekly"
    assert g("Weekly Anthropic Call 9.14.26") == "anthropic_weekly"
    assert g("PS Special Programs Training Call") == "pssp"
    assert g("Regular Board Meeting") == "board_regular"
    assert g("Budget Review") is None


def test_normalize_series_and_tags_keep_only_the_vocabulary():
    assert series.normalize_series("lt_weekly") == "lt_weekly"
    assert series.normalize_series("**weekly-checkin**") == "weekly_checkin"
    assert series.normalize_series("something else") == series.ONE_OFF
    tags = series.normalize_tags("Budget and finance, attendance, made-up, ai tools, "
                                 "attendance, literacy, math, enrollment")
    assert tags == ["budget-finance", "attendance", "ai-tools", "literacy", "math"]
    assert len(tags) <= series.TAG_MAX


def test_the_catalog_and_tags_cover_every_entry_once():
    ids = [s.id for s in series.SERIES]
    assert len(ids) == len(set(ids))
    slugs = [t[0] for t in series.TAGS]
    assert len(slugs) == len(set(slugs))
    text = series.catalog_text()
    assert all(f"- {i}:" in text for i in ids) and f"- {series.ONE_OFF}:" in text


def test_split_title():
    assert series.split_title(f"LT Weekly Meeting {DASH} Budget") == ("LT Weekly Meeting", "Budget")
    assert series.split_title("Budget Review") == (None, "Budget Review")


def test_title_case_restores_known_acronyms_written_in_lower_case():
    """'iep review' came out 'Iep Review'. A word the tag list or the series
    catalog writes in capitals (IEP, OKR, HR, AI, LT, PSSP) or in mixed case
    (PowerSchool) takes that spelling, with a plural or possessive kept."""
    from stt import series
    assert series.title_case("iep review") == "IEP Review"
    assert series.title_case("q3 mid-year review of iep's") == "q3 Mid-year Review of IEP's"
    assert series.title_case("okrs and hr policy") == "OKRs and HR Policy"
    assert series.title_case("ai tools pilot") == "AI Tools Pilot"
    assert series.title_case("Ese referrals") == "ESE Referrals"
    assert series.title_case("lt agenda (pssp)") == "LT Agenda (PSSP)"
    assert series.title_case("powerschool sync") == "PowerSchool Sync"
    assert series.title_case("cte-fte costs") == "CTE-FTE Costs"
    # what the model wrote in capitals stays, known or not
    assert series.title_case("UAT review") == "UAT Review"
    # a word that only looks like one keeps its title case
    assert series.title_case("hrs logged") == "Hrs Logged"
    assert series.title_case("aid plan") == "Aid Plan"
    # a name with an O' prefix keeps its second capital
    assert series.title_case("meeting with o'brien") == "Meeting with O'Brien"
    assert series.build_title("one_off", "iep review with o'brien") == "IEP Review with O'Brien"


def test_title_case_reads_each_part_of_a_slashed_word_and_school_acronyms():
    """'iep/ese review' read as one word and came out 'Iep/ese Review', and
    'okr planning for plc' gave 'Plc', since no tag or series writes PLC."""
    from stt import series
    assert series.title_case("iep/ese review") == "IEP/ESE Review"
    assert series.title_case("okr planning for plc") == "OKR Planning for PLC"
    assert series.title_case("plcs and mtss") == "PLCs and MTSS"
    assert series.title_case("sel and ela kpis") == "SEL and ELA KPIs"
    assert series.title_case("budget/staffing plan") == "Budget/Staffing Plan"
    assert series.title_case("pros and/or cons") == "Pros and/or Cons"
    assert series.title_case("it sync") == "It Sync"                 # 'it' stays a word


def test_title_case_keeps_program_names_with_small_words():
    from stt import series
    assert series.title_case("early on workflow UAT review") == "Early On Workflow UAT Review"
    assert series.title_case("transfer in workflow discovery") == "Transfer In Workflow Discovery"
    assert series.title_case("decisions made on the budget") == "Decisions Made on the Budget"


def test_a_person_with_a_path_character_or_dash_never_reaches_the_title():
    # the model's PERSON line matched nobody, so it arrives as written
    t = series.build_title("weekly_checkin", "Budget", f"Robin {DASH} Sam")
    assert t == f"Robin Sam Weekly Check-in {DASH} Budget"
    assert series.split_title(t) == ("Robin Sam Weekly Check-in", "Budget")
    t = series.build_title("weekly_checkin", "Budget", "Robin/Sam")
    assert t == f"Robin Sam Weekly Check-in {DASH} Budget"
    assert not any(c in series.build_title("weekly_checkin", "Budget", 'A:B"C?')
                   for c in ':"?/')
    # a person that is nothing but such characters drops the slot
    assert series.build_title("weekly_checkin", "Budget", "/") == f"Weekly Check-in {DASH} Budget"
