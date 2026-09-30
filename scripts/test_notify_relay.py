# -*- coding: utf-8 -*-
"""
5959 일정·다이어리 등록 알림(2026-09-30) — 오프라인 유닛테스트.

scripts/ogu_push_relay.py는 `import firebase_admin`을 main() 안에서만 호출하므로(모듈
최상단이 아님), 이 파일은 진짜 Firestore/FCM 없이도 릴레이 모듈을 그냥 import해서
순수 함수(_pref_on/_display_name/_fmt_md/_diary_snippet/_poll_new/_plan_event_notifications/
_plan_diary_notifications)를 직접 검증한다. e2e.js(프론트, 목 Firestore)와 대칭되는
백엔드 쪽 목 테스트 — 실제 네트워크·서비스계정 키 불필요.

실행: python scripts/test_notify_relay.py
"""
import os, sys, importlib.util
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ogu_push_relay", os.path.join(HERE, "ogu_push_relay.py"))
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(name)
        print("FAIL:", name)


def eq(name, got, want):
    check(name + " (got=%r want=%r)" % (got, want), got == want)


# ── 순수 헬퍼 ────────────────────────────────────────────────────────────
def test_pref_on_default():
    eq("pref 기본 ON(미설정)", relay._pref_on({}, "u1", "schedule"), True)
    eq("pref 기본 ON(uid 자체 없음)", relay._pref_on({"u2": {"schedule": False}}, "u1", "schedule"), True)


def test_pref_on_off():
    eq("pref OFF 반영", relay._pref_on({"u1": {"schedule": False}}, "u1", "schedule"), False)
    eq("pref ON 명시", relay._pref_on({"u1": {"schedule": True}}, "u1", "schedule"), True)
    eq("pref 다른 키 무간섭", relay._pref_on({"u1": {"schedule": False}}, "u1", "diary"), True)


def test_display_name():
    eq("이름 있음", relay._display_name({"partnerLabel": {"u1": "여봉"}}, "u1"), "여봉")
    eq("이름 없음 폴백", relay._display_name({}, "u1"), "짝")
    eq("profile 자체 없음", relay._display_name(None, "u1"), "짝")


def test_fmt_md():
    eq("10/3 포맷", relay._fmt_md("2026-10-03"), "10/3")
    eq("12/25 포맷", relay._fmt_md("2026-12-25"), "12/25")
    eq("이상값 폴백", relay._fmt_md("garbage"), "garbage")


def test_diary_snippet():
    eq("짧은 글 그대로", relay._diary_snippet("오늘 하루"), "오늘 하루")
    long_line = "가" * 25  # 20자 한도를 넘는 임의 텍스트(수치로 검증 — 한글 손수 세기 실수 방지)
    eq("20자 초과 말줄임", relay._diary_snippet(long_line), ("가" * 20) + "…")
    eq("정확히 20자는 안 자름", relay._diary_snippet("가" * 20), "가" * 20)
    eq("빈 텍스트=사진", relay._diary_snippet(""), "(사진)")
    eq("빈 텍스트(None)=사진", relay._diary_snippet(None), "(사진)")
    eq("첫 줄만 사용", relay._diary_snippet("첫줄\n둘째줄"), "첫줄")


# ── _poll_new: 최초 관측 백로그 스킵 + 중복 방지(커서 전진) ──────────────
class _FakeDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return self._data


class _FakeTs:
    def __init__(self, dt):
        self._dt = dt

    def timestamp(self):
        return self._dt.timestamp()


class _FakeQuery:
    def __init__(self, docs):
        self._docs = docs

    def where(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def get(self):
        return list(self._docs)


class _FakeDocRef:
    def __init__(self, subcols):
        self._subcols = subcols

    def collection(self, name):
        return _FakeQuery(self._subcols.get(name, []))


class _FakeDb:
    def __init__(self, pairs):
        self._pairs = pairs  # {pid: {subcol: [docs]}}

    def collection(self, name):
        assert name == "pairs"
        return self

    def document(self, pid):
        return _FakeDocRef(self._pairs.get(pid, {}))


def test_poll_new_first_observation_skips_backlog():
    now = datetime.now(timezone.utc)
    docs = [_FakeDoc("e1", {"createdAt": _FakeTs(now - timedelta(hours=1)), "title": "old"})]
    db = _FakeDb({"pidA": {"events": docs}})
    state = {}
    out, cursor = relay._poll_new(db, "pidA", "events", "createdAt", state, "pidA:ev", 50)
    eq("최초 관측=빈 리스트(백로그 스킵)", out, [])
    check("최초 관측 커서가 state에 저장됨", "pidA:ev" in state and state["pidA:ev"] == cursor)


def test_poll_new_detects_new_doc_after_cursor():
    now = datetime.now(timezone.utc)
    state = {"pidB:ev": int((now - timedelta(minutes=10)).timestamp() * 1000)}
    new_doc = _FakeDoc("e2", {"createdAt": _FakeTs(now), "title": "새 일정"})
    db = _FakeDb({"pidB": {"events": [new_doc]}})
    out, cursor = relay._poll_new(db, "pidB", "events", "createdAt", state, "pidB:ev", 50)
    eq("커서 이후 신규 문서 1건 감지", len(out), 1)
    eq("문서 id 보존", out[0]["id"], "e2")
    check("커서가 새 문서 시각으로 전진", cursor > state["pidB:ev"] or cursor == int(now.timestamp() * 1000))


def test_poll_new_dedup_on_repeat_poll():
    """중복 방지: 같은 문서를 두 번 폴링해도 두 번째는 잡히지 않는다(커서가 이미 전진했으므로)."""
    now = datetime.now(timezone.utc)
    state = {"pidC:ev": int((now - timedelta(minutes=10)).timestamp() * 1000)}
    doc = _FakeDoc("e3", {"createdAt": _FakeTs(now), "title": "일정"})
    db = _FakeDb({"pidC": {"events": [doc]}})  # 두 스윕 모두 같은 목록을 반환(실서비스의 서버측 필터를
    #                                              흉내내지 않고, _poll_new 내부의 ts<=last 안전망만으로
    #                                              중복을 걸러내는지 검증)
    out1, cursor1 = relay._poll_new(db, "pidC", "events", "createdAt", state, "pidC:ev", 50)
    state["pidC:ev"] = cursor1
    out2, cursor2 = relay._poll_new(db, "pidC", "events", "createdAt", state, "pidC:ev", 50)
    eq("1차 폴링=1건 감지", len(out1), 1)
    eq("2차 폴링(같은 문서 재조회)=0건(중복 방지)", len(out2), 0)


# ── _plan_event_notifications ───────────────────────────────────────────
MEMBERS = ["me", "partner"]
# partnerLabel[X] = "X가 자기 파트너를 부르는 이름"(index.html nameOf()와 동일 규약).
# 아래 테스트는 전부 creator/changer/author="me" → 받는 사람="partner" 구도라, "partner"가
# "me"를 부르는 이름(partnerLabel["partner"])이 알림 문구에 찍힌다.
PROFILE = {"partnerLabel": {"partner": "여봉", "me": "쭈니"}}


def _tokens():
    return {"me": "TOKEN_ME", "partner": "TOKEN_PARTNER"}


def test_plan_event_new_single_sends_to_partner_only():
    created = [{"id": "ev1", "createdBy": "me", "title": "치과", "date": "2026-10-03"}]
    plan = relay._plan_event_notifications(created, [], MEMBERS, _tokens(), {}, PROFILE)
    eq("1건 계획", len(plan), 1)
    p = plan[0]
    eq("받는 사람=partner(작성자 본인 제외)", p["uid"], "partner")
    eq("종류=sch", p["kind"], "sch")
    eq("문구 형식", p["body"], "여봉님이 10/3 일정을 등록했어요: 치과")
    eq("딥링크 탭", p["extra"]["tab"], "schedule")
    eq("딥링크 id", p["extra"]["id"], "ev1")


def test_plan_event_sticker_only_excluded():
    created = [{"id": "s1", "createdBy": "me", "title": "", "date": "2026-10-03", "sticker": "🎂"}]
    plan = relay._plan_event_notifications(created, [], MEMBERS, _tokens(), {}, PROFILE)
    eq("제목 없는 스티커 문서는 계획에서 제외", len(plan), 0)


def test_plan_event_batch_over_threshold():
    created = [{"id": "e%d" % i, "createdBy": "me", "title": "일정%d" % i, "date": "2026-10-0%d" % (i + 1)}
               for i in range(4)]  # 4건 > NOTIFY_BATCH_THRESHOLD(3)
    plan = relay._plan_event_notifications(created, [], MEMBERS, _tokens(), {}, PROFILE)
    eq("묶음 1건만 생성", len(plan), 1)
    eq("묶음 종류", plan[0]["kind"], "sch_batch")
    eq("묶음 문구", plan[0]["body"], "여봉님이 일정 4건 등록했어요")
    check("묶음엔 개별 id 없음", "id" not in plan[0]["extra"])


def test_plan_event_under_threshold_not_batched():
    created = [{"id": "e%d" % i, "createdBy": "me", "title": "일정%d" % i, "date": "2026-10-01"}
               for i in range(3)]  # 정확히 3건 = "초과" 아님(임계 미포함)
    plan = relay._plan_event_notifications(created, [], MEMBERS, _tokens(), {}, PROFILE)
    eq("3건은 개별 3건(묶지 않음)", len(plan), 3)
    check("모두 sch(묶음 아님)", all(p["kind"] == "sch" for p in plan))


def test_plan_event_toggle_off_no_notification():
    created = [{"id": "ev1", "createdBy": "me", "title": "치과", "date": "2026-10-03"}]
    prefs = {"partner": {"schedule": False}}
    plan = relay._plan_event_notifications(created, [], MEMBERS, _tokens(), prefs, PROFILE)
    eq("수신자가 일정 알림 꺼두면 미발송", len(plan), 0)


def test_plan_event_no_token_no_notification():
    created = [{"id": "ev1", "createdBy": "me", "title": "치과", "date": "2026-10-03"}]
    plan = relay._plan_event_notifications(created, [], MEMBERS, {"me": "T"}, {}, PROFILE)  # partner 토큰 없음
    eq("수신자 토큰 없으면 미발송", len(plan), 0)


def test_plan_event_date_change_single_notification():
    dc = [{"id": "ev1", "dateChangedBy": "me", "title": "치과", "date": "2026-10-05"}]
    plan = relay._plan_event_notifications([], dc, MEMBERS, _tokens(), {}, PROFILE)
    eq("날짜변경 1건", len(plan), 1)
    eq("종류=sch_date", plan[0]["kind"], "sch_date")
    eq("문구", plan[0]["body"], "여봉님이 10/5 일정 날짜를 바꿨어요: 치과")


def test_plan_event_date_change_toggle_off():
    dc = [{"id": "ev1", "dateChangedBy": "me", "title": "치과", "date": "2026-10-05"}]
    prefs = {"partner": {"scheduleDateChange": False}}
    plan = relay._plan_event_notifications([], dc, MEMBERS, _tokens(), prefs, PROFILE)
    eq("날짜변경 토글 꺼두면 미발송(등록 토글과 별개)", len(plan), 0)


# ── _plan_diary_notifications ───────────────────────────────────────────
def test_plan_diary_new_single():
    created = [{"id": "d1", "authorUid": "me", "text": "오늘 데이트 다녀왔어요"}]
    plan = relay._plan_diary_notifications(created, MEMBERS, _tokens(), {}, PROFILE)
    eq("1건 계획", len(plan), 1)
    eq("종류=diary", plan[0]["kind"], "diary")
    eq("문구=이름+첫줄", plan[0]["body"], "여봉님이 다이어리를 썼어요: 오늘 데이트 다녀왔어요")


def test_plan_diary_batch_over_threshold():
    created = [{"id": "d%d" % i, "authorUid": "me", "text": "글%d" % i} for i in range(5)]
    plan = relay._plan_diary_notifications(created, MEMBERS, _tokens(), {}, PROFILE)
    eq("묶음 1건", len(plan), 1)
    eq("묶음 문구", plan[0]["kind"], "diary_batch")
    eq("묶음 카운트", plan[0]["body"], "여봉님이 다이어리 5건 썼어요")


def test_plan_diary_toggle_off():
    created = [{"id": "d1", "authorUid": "me", "text": "글"}]
    prefs = {"partner": {"diary": False}}
    plan = relay._plan_diary_notifications(created, MEMBERS, _tokens(), prefs, PROFILE)
    eq("다이어리 알림 꺼두면 미발송", len(plan), 0)


def test_plan_diary_author_gets_no_self_notification():
    created = [{"id": "d1", "authorUid": "me", "text": "글"}]
    plan = relay._plan_diary_notifications(created, MEMBERS, _tokens(), {}, PROFILE)
    check("작성자 본인은 수신자 목록에 없음", all(p["uid"] != "me" for p in plan))


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    total = PASS + FAIL
    print("=" * 50)
    print("PASS %d/%d" % (PASS, total))
    if FAIL:
        print("FAILED:")
        for f in FAILURES:
            print(" -", f)
        sys.exit(1)
    sys.exit(0)
