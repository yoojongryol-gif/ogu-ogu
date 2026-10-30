# -*- coding: utf-8 -*-
"""
5959(오구오구) 웹푸시 릴레이 — 서버 없는 구조의 "PC 릴레이".
Firestore의 신규 대화 메시지를 감지해, 그 메시지를 "받는 쪽(상대 멤버)"의 FCM 토큰으로만 푸시를 보낸다.
내용은 절대 싣지 않는다(data-only) → 브라우저/서비스워커가 고정 문구 "새 소식이 도착했어요"만 표시한다.

설계 원칙(사장님 승인 라운드):
  - 본인 메시지엔 미발송(보낸 사람에게는 알림 X). 상대 멤버 토큰으로만.
  - 내용 미노출: notification 블록을 절대 넣지 않는다(data-only). 문구는 sw.js onBackgroundMessage가 고정.
  - 중복 발송 방지: (a) 프로세스 단일 인스턴스 락, (b) 메시지별 처리 상태(state.json)로 재발송 차단.

2026-09-30 확장(사장님 "일정이나 다이어리도 등록이 되면 알림이 오도록") — 일정(events) 신규 등록·
날짜변경, 다이어리(diary) 신규 등록 알림을 이 릴레이가 "새 발송 종류"로 추가한다(새 인프라 없음,
같은 프로세스·같은 FCM 경로·같은 state.json·같은 단일 인스턴스 락 재사용):
  - ★내용 노출 결정(채팅과 다른 점): 채팅/합류요청은 위 원칙대로 내용 미노출이지만, 일정·다이어리는
    사장님이 문구를 직접 지정했다("○○님이 10/3 일정을 등록했어요: 제목") — 이름·날짜·제목/첫줄을
    data.body에 실어 보낸다. sw.js가 이 문구를 그대로 표시한다(가공 없음). 앱의 "알림 내용 완전
    숨김" 창립 원칙과는 이 카테고리에 한해 다른 결정임을 코드에 남겨둔다(재조사 시 참고).
  - 트리거: events는 title이 있는 문서의 createdAt(제목 없는 "스티커만" 문서는 제외 — saveSticker가
    만드는 문서라 "일정 등록"이 아님) + 편집으로 date/endDate가 실제로 바뀐 문서의 dateChangedAt
    (index.html addEvent()가 그때만 이 필드를 찍는다). diary는 authorUid 있는 문서의 createdAt만
    (수정 시 갱신되는 updatedAt은 보지 않음 → "수정·삭제는 알림 없음" 사양 그대로 지켜짐).
  - 세부 토글: pairs.notifyPrefs[받는사람uid] = {schedule, scheduleDateChange, diary}(각 bool, 기본
    ON — _pref_on() 참고). index.html의 myNotifyPrefs() 기본값과 반드시 같아야 한다.
  - 중복 방지: 메시지와 동일한 "저장된 커서(state[pid+':ev'|':evd'|':dy']) 이후만" 패턴(_poll_new) —
    최초 관측 시 백로그를 건너뛰고 커서만 지금 시각으로 세팅한다(배포 직후 기존 문서 알림 폭주 방지).
  - 묶음: 같은 스윕에서 같은 작성자의 신규 항목이 NOTIFY_BATCH_THRESHOLD(3)건을 넘으면 개별 알림
    대신 "○○님이 일정 4건 등록했어요" 식 1건으로 합친다(다이어리도 동일 규칙).
  - 폴링 주기 분리: 채팅(POLL_SEC=30s)보다 느슨한 EVDIARY_POLL_SEC(기본 120s)로 별도 실행 — 일정·
    다이어리는 채팅만큼 즉시성이 중요하지 않고, 폴링마다 읽기 1건씩 더 드는 비용(events+events(날짜
    변경)+diary = pair당 스윕당 +3 read)을 8/28 429 RCA 이후의 보수적 원칙대로 억제한다.
  - 딥링크: data.tab("schedule"|"diary")·data.id(문서 id, 묶음 알림엔 없음)를 함께 보낸다 — 내용이
    아니라 "탭하면 어디로 이동할지"이므로 위 내용-노출 결정과는 무관하게 항상 보낸다.
  - 무료 읽기 한도 보수적: paired 목록은 드물게(기본 5분) 갱신, 메시지 폴링은 기본 120초.
    ※ 비용 규칙(2026-08-28 RCA): Firestore는 결과 0건인 쿼리에도 문서 읽기 1건을 최소 과금한다.
      따라서 읽기 비용 = (폴링 대상 pair 수) x (스윕 횟수) 이며 메시지 양과는 무관하다.
      → 폴링 대상을 "FCM 토큰이 있는 paired"로 좁히고 폴링 간격을 늘리는 것이 유일한 비용 통제 수단.
  - 비밀(서비스계정 키)은 저장소 밖 경로에서 로드. 절대 커밋 금지.

필요 패키지:  pip install firebase-admin
서비스계정 키:  기본 경로 = C:/Users/NHNE/ogu-ogu-secrets/ogu-ogu-service-account.json
              (환경변수 OGU_SA_KEY 로 덮어쓸 수 있음)
실행:  python scripts/ogu_push_relay.py           (상시 폴링)
       python scripts/ogu_push_relay.py --once    (1회 스윕 후 종료, 점검용)
로그:  C:/Users/NHNE/ogu-ogu-secrets/ogu_push_relay.log
"""
import os, sys, json, time, logging, socket, atexit, uuid
from datetime import datetime, timezone

PROJECT_ID = "ogu-ogu-app-e4193"
SECRETS_DIR = os.environ.get("OGU_SECRETS_DIR", r"C:/Users/NHNE/ogu-ogu-secrets")
SA_KEY = os.environ.get("OGU_SA_KEY", os.path.join(SECRETS_DIR, "ogu-ogu-service-account.json"))
STATE_PATH = os.path.join(SECRETS_DIR, "ogu_push_relay_state.json")
LOCK_PATH = os.path.join(SECRETS_DIR, "ogu_push_relay.lock")
LOG_PATH = os.path.join(SECRETS_DIR, "ogu_push_relay.log")

# ⚠ 기본값 자체가 운영값이다. Startup(ogu-push-relay.vbs)은 환경변수를 주지 않고 기동하므로
#   env 의존 설정은 무인 실행에 반영되지 않는다(2026-08-28 RCA P1).
PAIRS_REFRESH_SEC = int(os.environ.get("OGU_PAIRS_REFRESH_SEC", "300"))   # 활성 pair 목록 갱신(보수적)
POLL_SEC = int(os.environ.get("OGU_POLL_SEC", "30"))                      # 메시지 폴링 간격(8/30 120→30 복귀: 지연 중앙값 73s 해소, read 21%)
EVDIARY_POLL_SEC = int(os.environ.get("OGU_EVDIARY_POLL_SEC", "120"))     # 일정·다이어리 폴링 간격(2026-09-30, 채팅보다 느슨 — 비용 억제)
MSG_LIMIT = 20                                                             # 한 번에 처리할 신규 메시지 상한
EVT_LIMIT = 50                                                             # 한 번에 처리할 신규/날짜변경 일정 상한
DIARY_LIMIT = 50                                                          # 한 번에 처리할 신규 다이어리 상한
NOTIFY_BATCH_THRESHOLD = 3                                                # 같은 작성자의 신규 항목이 이 수를 "넘으면" 묶음 문구로
FREE_READ_QUOTA = 50000                                                    # Spark 무료 일일 문서 읽기 한도(비교용)
ACK_WAIT_SEC = int(os.environ.get("OGU_ACK_WAIT_SEC", "300"))             # 2026-10-28: 발송 후 이 시간(초) 내 notif_ack 없으면 [notif][no_ack] 1줄
ACK_CHECK_SEC = int(os.environ.get("OGU_ACK_CHECK_SEC", "60"))            # ack 점검 주기(초) — 채팅 폴링과 별도, 느슨하게

# 2026-10-28 알림 결손 관측(10/22 진단: FCM 발송 성공 로그는 있는데 사장님 폰 미수신 — 서버 쪽은
# "발송 성공" 이후를 볼 수 없는 구조적 한계가 있었다). 발송마다 고유 nid를 같이 실어보내고,
# 클라이언트(sw.js/index.html)가 수신 시 notif_ack/{nid} 문서를 쓰면 여기서 그 존재를 확인한다.
# 이 dict는 프로세스 메모리에만 있다(재시작 시 비워짐 — 관측용이라 영속화 불필요, state.json과 무관).
_PENDING_ACKS = {}  # nid -> {"pid":str, "kind":str, "sent_ms":int}

os.makedirs(SECRETS_DIR, exist_ok=True)
_handlers = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
if sys.stdout is not None:  # pythonw(무창) 실행 시 stdout=None → StreamHandler 생략(로그는 파일로만)
    _handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=_handlers)
log = logging.getLogger("ogu_relay")


# ── 단일 인스턴스 락(중복 발송 사고 방지) ─────────────────────────────
def acquire_single_instance():
    if os.path.exists(LOCK_PATH):
        try:
            info = json.load(open(LOCK_PATH, encoding="utf-8"))
            pid = int(info.get("pid", -1))
            if _pid_alive(pid):
                log.error("이미 릴레이가 실행 중입니다(pid=%s, host=%s). 중복 실행 방지로 종료.", pid, info.get("host"))
                sys.exit(2)
        except Exception:
            pass  # 손상된 락은 덮어쓴다
    json.dump({"pid": os.getpid(), "host": socket.gethostname(), "at": _now_iso()},
              open(LOCK_PATH, "w", encoding="utf-8"))
    atexit.register(lambda: os.path.exists(LOCK_PATH) and os.remove(LOCK_PATH))


def _pid_alive(pid):
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import subprocess
            out = subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}"], stderr=subprocess.DEVNULL).decode("cp949", "ignore")
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


# ── 상태(중복 방지): pair별 마지막 처리한 메시지 시각(ms) ───────────────
def load_state():
    try:
        return json.load(open(STATE_PATH, encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    json.dump(state, open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, STATE_PATH)


def _ts_ms(v):
    # Firestore Timestamp(서버시간) → epoch ms. None/누락은 0.
    if v is None:
        return 0
    try:
        return int(v.timestamp() * 1000)
    except Exception:
        return 0


def _has_token(meta):
    """이 pair에 실제로 푸시를 받을 FCM 토큰이 하나라도 있는가(빈 문자열/None 제외)."""
    return any((meta.get("fcmTokens") or {}).values())


def _estimate_daily_reads(polled_pairs, total_pairs):
    """하루 예상 문서 읽기 = 폴링대상 x 스윕수 + 활성pair수 x 목록갱신수.
    (빈 쿼리도 1 read 최소 과금이므로 이 산술이 곧 비용이다.)
    목록 갱신은 '경과 >= PAIRS_REFRESH_SEC인 첫 스윕'에 발화 → 실효 주기는 POLL_SEC의 배수."""
    sweeps = 86400.0 / POLL_SEC
    eff_refresh = max(1, -(-PAIRS_REFRESH_SEC // POLL_SEC)) * POLL_SEC   # ceil 배수
    refreshes = 86400.0 / eff_refresh
    msg_reads = polled_pairs * sweeps
    list_reads = total_pairs * refreshes
    total = msg_reads + list_reads
    return ("약 %d건/일 = 폴링 %d개x%.0f스윕(%d) + 목록 %d개x%.0f회(%d) / 무료한도 %d의 %.0f%%"
            % (total, polled_pairs, sweeps, msg_reads, total_pairs, refreshes, list_reads,
               FREE_READ_QUOTA, total * 100.0 / FREE_READ_QUOTA))


def main():
    if not os.path.exists(SA_KEY):
        log.error("서비스계정 키가 없습니다: %s", SA_KEY)
        log.error("→ 사장님: Firebase 콘솔(u/1 tongshin2) → 프로젝트 설정 → 서비스 계정 → '새 비공개 키 생성'으로")
        log.error("  JSON을 받아 위 경로에 저장하세요(저장소 밖, 절대 커밋 금지).")
        sys.exit(3)

    import firebase_admin
    from firebase_admin import credentials, firestore, messaging

    acquire_single_instance()
    firebase_admin.initialize_app(credentials.Certificate(SA_KEY))
    db = firestore.client()
    log.info("릴레이 시작 (project=%s, poll=%ss, pairs_refresh=%ss)", PROJECT_ID, POLL_SEC, PAIRS_REFRESH_SEC)

    state = load_state()
    once = "--once" in sys.argv
    pairs = {}
    pairs_loaded_at = 0
    skip_logged = set()   # 토큰 없는 pair는 로그에 1회만 남긴다(로그 폭주 방지)
    last_estimate = None
    last_evdiary_poll = 0  # 2026-09-30: 일정·다이어리는 채팅보다 느슨한 별도 주기(EVDIARY_POLL_SEC)
    last_ack_check = 0     # 2026-10-28: notif_ack 점검(ACK_CHECK_SEC) — 채팅 폴링과 분리된 느슨한 주기

    while True:
        now = time.time()
        # 활성 pair 목록 갱신(보수적)
        if now - pairs_loaded_at >= PAIRS_REFRESH_SEC or not pairs:
            pairs = load_paired(db)
            pairs_loaded_at = now
            polled = sum(1 for m in pairs.values() if m.get("status") == "paired" and _has_token(m))
            log.info("활성 pair %d개 로드 (메시지 폴링 대상 %d개)", len(pairs), polled)
            est = (polled, len(pairs))
            if est != last_estimate:      # 규모가 바뀔 때만 1줄 — 사후 검증용 계측(RCA P2)
                last_estimate = est
                log.info("예상 일일 read %s", _estimate_daily_reads(polled, len(pairs)))

        do_evdiary = (now - last_evdiary_poll) >= EVDIARY_POLL_SEC

        sent = 0
        for pid, meta in list(pairs.items()):
            if meta.get("status") == "paired":
                # 푸시 보낼 토큰이 하나도 없는 pair는 조회 자체가 낭비다(빈 쿼리도 1 read 과금).
                # 테스트 잔재 pair가 여기서 걸러진다 — 2026-08-28 429 폭주의 근본 차단.
                if not _has_token(meta):
                    if pid not in skip_logged:
                        skip_logged.add(pid)
                        log.info("[%s] 구독 토큰 없음 → 메시지 폴링 스킵(이후 로그 생략)", pid)
                    continue
                sent += process_pair(db, messaging, pid, meta, state)          # 새 메시지 알림
                if do_evdiary:
                    sent += process_pair_events(db, messaging, pid, meta, state)  # 일정 등록·날짜변경 알림
                    sent += process_pair_diary(db, messaging, pid, meta, state)   # 다이어리 등록 알림
            elif meta.get("joinRequest"):
                sent += process_join_request(db, messaging, pid, meta, state)  # 합류 요청 알림
        if do_evdiary:
            last_evdiary_poll = now
        # 상태는 항상 저장한다 — 첫 관측(first-sight)으로 세팅한 lastSeen도 반드시 영속화해야
        # 다음 스윕에서 '그 이후' 메시지를 신규로 잡는다(sent==0일 때 미저장 시 매번 재초기화되어 발송 누락).
        save_state(state)
        if sent:
            log.info("이번 스윕 발송 %d건", sent)

        if now - last_ack_check >= ACK_CHECK_SEC:  # 2026-10-28: 발송 성공 이후(단말 도착) 관측, 발송 자체는 안 함
            try:
                check_notif_acks(db)
            except Exception as e:
                log.warning("[notif] ack 점검 실패(무해, 다음 주기 재시도): %s", type(e).__name__)
            last_ack_check = now

        if once:
            break
        time.sleep(POLL_SEC)


def load_paired(db):
    """활성 pair 로드: paired(메시지 알림) + solo/waiting(합류 요청 알림).
    {pid: {'status','members':[..],'fcmTokens':{uid:token},'lastReadAt':{uid:Timestamp},'joinRequest':{...}|None}}.
    lastReadAt(2026-09-01, "열람 중 알림 억제" 사장님 결정 A안): 이미 이 pair 문서를 읽는 김에
    같은 필드를 하나 더 꺼내는 것뿐이라 추가 read 비용 0 — index.html의 markRead()가 갱신하는
    pairs/{id}.lastReadAt[uid](케이스 D, firestore.rules 무접촉)를 그대로 재사용한다."""
    out = {}
    try:
        for d in db.collection("pairs").where("status", "in", ["paired", "solo", "waiting"]).stream():
            data = d.to_dict() or {}
            out[d.id] = {
                "status": data.get("status"),
                "members": data.get("memberUids", []),
                "fcmTokens": data.get("fcmTokens", {}) or {},
                "lastReadAt": data.get("lastReadAt", {}) or {},
                "joinRequest": data.get("joinRequest"),
                "profile": data.get("profile", {}) or {},          # 2026-09-30: partnerLabel(애칭)로 알림 문구 이름 조립
                "notifyPrefs": data.get("notifyPrefs", {}) or {},   # 2026-09-30: 받는사람별 일정/다이어리 알림 토글
            }
    except Exception as e:
        log.warning("활성 pair 조회 실패: %s", e)
    return out


def process_join_request(db, messaging, pid, meta, state):
    """solo/waiting pair에 합류 요청(joinRequest)이 있으면 초대한 멤버에게 "연결 요청" 푸시(1회).
    중복 방지: 요청 식별키(요청자 uid + at)를 상태에 기록해 같은 요청은 재발송 안 함."""
    jr = meta.get("joinRequest") or {}
    req_uid = jr.get("uid")
    if not req_uid:
        return 0
    at = _ts_ms(jr.get("at")) or 0
    key = "req:" + pid
    seen = state.get(key)
    sig = str(req_uid) + ":" + str(at)
    if seen == sig:
        return 0  # 이미 이 요청은 알림 보냄
    tokens = meta.get("fcmTokens", {})
    sent = 0
    for uid in meta.get("members", []):  # 초대한 멤버(요청자 아님)에게만
        if uid == req_uid:
            continue
        token = tokens.get(uid)
        if token and _send(messaging, token, pid, kind="req"):
            sent += 1
    state[key] = sig
    return sent


def _already_read(last_read_map, uid, msg_ts_ms):
    """수신자가 이 메시지를 이미 읽었는가 — pairs.lastReadAt[uid](이동 포인터, ms) >= 메시지 시각(ms).
    2026-09-01 사장님 결정 A안: "실시간 확인 시에는 알림 안 뜨게" → 열람 중이 아니어도, 이미 읽고
    나간 경우까지 포함해 '기읽음'이면 스킵한다(엄밀히는 지시의 부분집합이지만 rules 무접촉 제약 하의
    최선 근사 — 기록 문서 참조).
    last_read_map: pair.lastReadAt 맵({uid: Firestore Timestamp}) 또는 없음/빈 dict.
    필드 자체가 없거나 해당 uid 키가 없으면 False(미읽음으로 취급 → 안전하게 발송, 억제가 아니라
    발송 쪽으로 기본값을 잡아 "못 받는" 사고를 피한다)."""
    if not last_read_map:
        return False
    v = last_read_map.get(uid)
    if v is None:
        return False
    last_ms = _ts_ms(v)
    return last_ms > 0 and last_ms >= msg_ts_ms


def _fresh_last_read_ms(db, pid, uid):
    """캐시(meta.lastReadAt, load_paired()가 최대 PAIRS_REFRESH_SEC=300초에 한 번만 갱신)만으로
    '미읽음'이라 판단해 발송을 확정하기 직전에만, 그 pair 문서를 지금 이 순간 다시 읽어(1건) 최신
    lastReadAt으로 한 번 더 확인한다.
    2026-09-01 사장님 실사용 보고("메시지 보고 있는데도 알림이 뜨네") E항목 근본수정 — 최초 가설은
    "클라이언트가 방을 보는 중 새 메시지 렌더 시점에 markRead를 안 부른다"였으나, index.html의
    watchMessages() onSnapshot 핸들러(오늘 첫 백업 dev/index.html.bak_20260901 시점부터 이미 존재,
    이번 세션 신규 코드 아님 — 정적 diff로 재확인)는 스냅샷이 올 때마다(신규 메시지 렌더 포함)
    "잠금 아님 + screenChat 활성 + visible"이면 이미 markRead()를 호출해 lastReadAt을 갱신하고
    있었다. 즉 클라이언트 쪽은 무죄 — 진짜 구멍은 릴레이가 그 최신 값을 못 보는 것이었다:
    load_paired()가 pair 목록(lastReadAt 포함)을 5분에 한 번만 새로 읽어오므로, 그 사이 상대가
    방금 읽었어도 캐시는 최대 5분 전 값을 들고 있어 '미읽음'으로 오판해 발송했다.
    실패(권한/네트워크 등)는 None을 돌려주고, 호출부는 안전측으로 '발송 진행'을 유지한다
    (읽음 재확인 실패가 알림 전달 자체를 막으면 안 된다 — 발송 쪽으로 기본값)."""
    try:
        doc = db.collection("pairs").document(pid).get()
        if not doc.exists:
            return None
        data = doc.to_dict() or {}
        v = (data.get("lastReadAt") or {}).get(uid)
        return _ts_ms(v) if v is not None else None
    except Exception as e:
        log.warning("[%s] lastReadAt 재확인 실패(uid=%s): %s", pid, uid[:6], e)
        return None


def process_pair(db, messaging, pid, meta, state):
    """pair의 신규 메시지를 감지 → 상대 멤버 토큰으로 data-only 푸시. 발송 건수 반환.
    비용 핵심(2026-08-28 정정): 빈 쿼리도 Firestore는 문서 읽기 1건을 최소 과금한다.
    구 주석의 "신규 메시지가 없으면 읽기 0건"은 오기였고, 이 오해가 429 폭주의 뿌리였다.
    실제 비용 = (폴링 대상 pair 수) x (스윕 횟수)이며 메시지 양과 무관 → 호출부에서
    토큰 없는 pair를 걸러내는 것(_has_token)이 유일한 비용 통제 수단이다.
    lastSeen '이후'(ts >) 필터는 여전히 유효하나, 절약 효과는 '0건'이 아니라 '1건'까지다.
    첫 관측 pair는 백로그를 알림 없이 건너뛰고 기준시각만 now로 세팅(과거 메시지 무더기 발송/읽기 방지)."""
    from firebase_admin import firestore as _fs
    from datetime import datetime, timezone
    sent = 0
    if pid not in state:
        state[pid] = _now_ms()  # 첫 관측: 백로그 스킵(이 시점 이후 메시지만 알림)
        return 0
    last = int(state[pid])
    try:
        after = datetime.fromtimestamp(last / 1000.0, timezone.utc)
        q = (db.collection("pairs").document(pid).collection("messages")
             .where("ts", ">", after).order_by("ts").limit(MSG_LIMIT))
        docs = list(q.get())  # 신규가 없어도 최소 1 read 과금(빈 쿼리 = 0건이 아니다)
    except Exception as e:
        log.warning("[%s] 메시지 조회 실패: %s", pid, e)
        return 0

    # 최신 fcmTokens 확보(캐시가 오래됐을 수 있어 pair 문서 재조회는 비용이라, 스윕 시작분 meta 사용).
    tokens = meta.get("fcmTokens", {})
    last_read_map = meta.get("lastReadAt") or {}  # 2026-09-01 A안: 스윕 시작분 meta 재사용, 추가 read 0.
    max_ts = last
    for d in docs:
        m = d.to_dict() or {}
        ts = _ts_ms(m.get("ts"))
        if ts <= last:
            continue
        max_ts = max(max_ts, ts)
        sender = m.get("sender")
        # 받는 쪽 = 보낸 사람 아닌 멤버(들)
        for uid in meta.get("members", []):
            if uid == sender:
                continue  # 본인에겐 미발송
            if _already_read(last_read_map, uid, ts):
                log.info("[notif][skip_read] [%s] [스킵] 기읽음 (uid=%s)", pid, uid[:6])
                continue  # 이미 읽고 나감(또는 실시간 확인 중 markRead 갱신) — 알림 불필요
            token = tokens.get(uid)
            if not token:
                continue  # 상대가 알림 미구독
            # E항목(2026-09-01): 캐시(last_read_map)로는 '미읽음'이지만 실제 발송 확정 직전 순간에만
            # pair를 1건 재조회해 최신 lastReadAt으로 한 번 더 확인 — 5분 캐시 창을 실질적으로 "폴링
            # 주기(≤30s)"까지 좁힌다. 발송 확정 직전(=이 시점)에만 도는 read라 스윕당 비용이 아니라
            # "발송 시도 건수"에 비례(process_pair 함수 docstring의 기존 비용 모델과 다른 축).
            fresh_ms = _fresh_last_read_ms(db, pid, uid)
            if fresh_ms is not None and fresh_ms >= ts:
                log.info("[notif][skip_read] [%s] [스킵] 기읽음(재확인, uid=%s)", pid, uid[:6])
                continue
            if _send(messaging, token, pid):
                sent += 1
    if max_ts > last:
        state[pid] = max_ts
    return sent


# ── 일정·다이어리 등록 알림(2026-09-30) ──────────────────────────────────────
def _pref_on(prefs, uid, key):
    """받는사람(uid) 본인의 알림 세부설정. 값이 아예 없으면(신규 배포 직후 기존 pair 전부가 이 상태)
    기본 ON — index.html의 myNotifyPrefs()와 반드시 같은 기본값을 써야 한다(어긋나면 '껐는데 오는'
    또는 '켰는데 안 오는' 사고)."""
    u = (prefs or {}).get(uid) or {}
    v = u.get(key)
    return True if v is None else bool(v)


def _display_name(profile, recipient_uid):
    """받는사람(recipient_uid) 화면에 '누가' 등록했는지 — index.html nameOf()와 동일 계산:
    상대의 이름 = 상대가 partnerLabel[recipient_uid]에 적어둔 값(= profile.partnerLabel[받는사람uid]).
    아직 서로 이름을 안 지었으면 stickerWhoLabel() 폴백과 같은 '짝'."""
    pl = (profile or {}).get("partnerLabel") or {}
    return pl.get(recipient_uid) or "짝"


def _fmt_md(date_str):
    """'YYYY-MM-DD' → 'M/D'(선행 0 없음, 문구 예시 "10/3 일정" 그대로)."""
    try:
        y, m, d = str(date_str).split("-")
        return "%d/%d" % (int(m), int(d))
    except Exception:
        return date_str or ""


def _diary_snippet(text, limit=20):
    """다이어리 알림 문구용 — 첫 줄을 limit자로 자른다(요청 사양: '제목 또는 첫 줄 20자').
    사진만 있고 글이 없는 항목은 "(사진)"."""
    text = (text or "").strip()
    if not text:
        return "(사진)"
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), text)
    return (first[:limit] + "…") if len(first) > limit else first


def _poll_new(db, pid, subcol, ts_field, state, state_key, limit):
    """pairs/{pid}/{subcol}에서 ts_field > 커서(state[state_key])인 새 문서를 반환.
    최초 관측(state_key가 아직 없음)은 백로그를 건너뛰고 커서만 지금 시각으로 세팅한다 —
    process_pair()의 '첫 관측 pair는 과거 메시지 무더기 발송 방지' 원칙과 동일(신규 배포 직후
    기존 이벤트/다이어리 수백 건이 한꺼번에 알림으로 터지는 사고를 막는 핵심 안전장치).
    반환: (새 문서 dict 목록(각 dict에 'id' 포함), 이번 스윕의 새 커서값(ms))."""
    if state_key not in state:
        state[state_key] = _now_ms()
        return [], state[state_key]
    last = int(state[state_key])
    try:
        after = datetime.fromtimestamp(last / 1000.0, timezone.utc)
        q = (db.collection("pairs").document(pid).collection(subcol)
             .where(ts_field, ">", after).order_by(ts_field).limit(limit))
        docs = list(q.get())  # 빈 쿼리도 최소 1 read 과금(process_pair와 동일 비용 모델)
    except Exception as e:
        log.warning("[%s] %s 조회 실패: %s", pid, subcol, e)
        return [], last
    out = []
    max_ts = last
    for d in docs:
        m = d.to_dict() or {}
        ts = _ts_ms(m.get(ts_field))
        if ts <= last:
            continue  # 재확인 안전망(쿼리 자체가 이미 '>'로 걸렀지만, 시계 오차 등 방어)
        max_ts = max(max_ts, ts)
        m["id"] = d.id
        out.append(m)
    return out, max_ts


def _plan_event_notifications(new_created, new_date_changed, members, tokens, prefs, profile):
    """일정 신규 등록 + 날짜변경 문서 목록으로 '보낼 알림 계획'을 만든다(부작용 없음 — FCM 호출도
    Firestore 조회도 하지 않아 테스트가 쉽다). 반환: [{'uid','token','kind','body','extra'}, ...]."""
    plan = []
    by_creator = {}
    for m in new_created:
        creator = m.get("createdBy")
        if not creator or not (m.get("title") or "").strip():
            continue  # 제목 없는 문서 = saveSticker()의 "스티커만" 문서 → 일정 등록 알림 대상 아님
        by_creator.setdefault(creator, []).append(m)
    for creator, items in by_creator.items():
        for uid in members:
            if uid == creator or not _pref_on(prefs, uid, "schedule"):
                continue
            token = tokens.get(uid)
            if not token:
                continue
            name = _display_name(profile, uid)
            if len(items) > NOTIFY_BATCH_THRESHOLD:
                plan.append({"uid": uid, "token": token, "kind": "sch_batch",
                             "body": "%s님이 일정 %d건 등록했어요" % (name, len(items)),
                             "extra": {"tab": "schedule"}})
            else:
                for m in items:
                    title = (m.get("title") or "").strip()
                    plan.append({"uid": uid, "token": token, "kind": "sch",
                                 "body": "%s님이 %s 일정을 등록했어요: %s" % (name, _fmt_md(m.get("date")), title),
                                 "extra": {"tab": "schedule", "id": m.get("id")}})
    # 날짜변경은 문서 1건당 "알림 1회"(사양 ②) — 등록과 달리 묶지 않는다(애초에 흔치 않은 이벤트).
    for m in new_date_changed:
        changer = m.get("dateChangedBy")
        if not changer:
            continue
        for uid in members:
            if uid == changer or not _pref_on(prefs, uid, "scheduleDateChange"):
                continue
            token = tokens.get(uid)
            if not token:
                continue
            name = _display_name(profile, uid)
            title = (m.get("title") or "").strip() or "(제목 없음)"
            plan.append({"uid": uid, "token": token, "kind": "sch_date",
                         "body": "%s님이 %s 일정 날짜를 바꿨어요: %s" % (name, _fmt_md(m.get("date")), title),
                         "extra": {"tab": "schedule", "id": m.get("id")}})
    return plan


def _plan_diary_notifications(new_created, members, tokens, prefs, profile):
    """다이어리 신규 등록 문서 목록으로 알림 계획을 만든다(수정/삭제는 애초에 이 함수에 안 들어옴 —
    process_pair_diary가 createdAt만 커서로 보고 updatedAt은 보지 않는다). 부작용 없음."""
    plan = []
    by_author = {}
    for m in new_created:
        author = m.get("authorUid")
        if not author:
            continue
        by_author.setdefault(author, []).append(m)
    for author, items in by_author.items():
        for uid in members:
            if uid == author or not _pref_on(prefs, uid, "diary"):
                continue
            token = tokens.get(uid)
            if not token:
                continue
            name = _display_name(profile, uid)
            if len(items) > NOTIFY_BATCH_THRESHOLD:
                plan.append({"uid": uid, "token": token, "kind": "diary_batch",
                             "body": "%s님이 다이어리 %d건 썼어요" % (name, len(items)),
                             "extra": {"tab": "diary"}})
            else:
                for m in items:
                    plan.append({"uid": uid, "token": token, "kind": "diary",
                                 "body": "%s님이 다이어리를 썼어요: %s" % (name, _diary_snippet(m.get("text"))),
                                 "extra": {"tab": "diary", "id": m.get("id")}})
    return plan


def process_pair_events(db, messaging, pid, meta, state):
    """일정 등록·날짜변경 알림 발송. 계획(_plan_event_notifications)만 순수 함수로 떼어내고,
    여기서는 조회(_poll_new)·발송(_send)·커서 저장만 담당."""
    members = meta.get("members", [])
    if len(members) != 2:
        return 0
    tokens = meta.get("fcmTokens", {})
    profile = meta.get("profile") or {}
    prefs = meta.get("notifyPrefs") or {}
    new_created, max_ts = _poll_new(db, pid, "events", "createdAt", state, pid + ":ev", EVT_LIMIT)
    new_dc, max_dc = _poll_new(db, pid, "events", "dateChangedAt", state, pid + ":evd", EVT_LIMIT)
    plan = _plan_event_notifications(new_created, new_dc, members, tokens, prefs, profile)
    sent = 0
    for p in plan:
        extra = dict(p["extra"]); extra["body"] = p["body"]
        if _send(messaging, p["token"], pid, kind=p["kind"], extra=extra):
            sent += 1
    state[pid + ":ev"] = max_ts
    state[pid + ":evd"] = max_dc
    return sent


def process_pair_diary(db, messaging, pid, meta, state):
    """다이어리 신규 등록 알림 발송(수정·삭제는 무알림). 구조는 process_pair_events와 동일."""
    members = meta.get("members", [])
    if len(members) != 2:
        return 0
    tokens = meta.get("fcmTokens", {})
    profile = meta.get("profile") or {}
    prefs = meta.get("notifyPrefs") or {}
    new_created, max_ts = _poll_new(db, pid, "diary", "createdAt", state, pid + ":dy", DIARY_LIMIT)
    plan = _plan_diary_notifications(new_created, members, tokens, prefs, profile)
    sent = 0
    for p in plan:
        extra = dict(p["extra"]); extra["body"] = p["body"]
        if _send(messaging, p["token"], pid, kind=p["kind"], extra=extra):
            sent += 1
    state[pid + ":dy"] = max_ts
    return sent


def _send(messaging, token, pid, kind="m", extra=None):
    """FCM data-only 발송(공용 경로 — 채팅/합류요청/일정/다이어리 전부 여기를 지난다).
    kind: 'm'=새 메시지 / 'req'=합류요청(내용 미노출, 고정 문구) / 'sch'·'sch_batch'·'sch_date'·
    'diary'·'diary_batch'=2026-09-30 신설(내용 노출, extra['body']가 곧 사장님이 지정한 알림 문구).
    extra: {'body':str, 'tab':str, 'id':str} 중 있는 것만 문자열로 실어 보낸다(id는 묶음 알림엔 없음).
    2026-10-28: data.nid(난수)를 같이 실어보내 클라이언트가 notif_ack/{nid}를 쓸 수 있게 한다
    (알림 내용과 무관한 관측용 식별자 — 내용 미노출 원칙과 충돌 없음). 반환값은 성공 시 nid(진리값
    True와 동등), 실패 시 None — 기존 호출부의 `if _send(...): sent+=1` 패턴은 그대로 동작한다."""
    nid = uuid.uuid4().hex[:16]
    try:
        data = {"t": kind, "nid": nid}
        if extra:
            for k, v in extra.items():
                if v:
                    data[k] = str(v)
        msg = messaging.Message(
            token=token,
            data=data,
            webpush=messaging.WebpushConfig(
                headers={"Urgency": "high", "TTL": "1800"},
                # notification 블록은 여전히 안 씀 — 표시는 항상 sw.js onBackgroundMessage가 담당(플랫폼
                # 기본 렌더에 맡기지 않아야 'm'/'req' 카테고리의 내용 미노출 원칙을 sw.js 쪽에서 강제할 수 있다).
            ),
        )
        messaging.send(msg)
        _PENDING_ACKS[nid] = {"pid": pid, "kind": kind, "sent_ms": _now_ms()}
        log.info("[notif][sent] pid=%s kind=%s nid=%s", pid, kind, nid)
        return nid
    except Exception as e:
        name = type(e).__name__
        # 무효/만료 토큰 등은 조용히 스킵(다음 구독 갱신 때 정리됨). 내용은 로그에도 안 남김.
        log.info("[%s] 발송 스킵(%s)", pid, name)
        return None


def check_notif_acks(db):
    """2026-10-28: 발송 후 ACK_WAIT_SEC(기본 5분)이 지났는데 notif_ack/{nid} 문서가 없는 건을
    [notif][no_ack] 1줄로 남긴다. ★관측만 — 추가 알림 발송·재시도는 전혀 하지 않는다(지시사항).
    ack 문서 조회 실패(권한/네트워크 등)는 판정을 유보하고 다음 점검 때 재시도(여기서 삭제 안 함)."""
    if not _PENDING_ACKS:
        return
    now = _now_ms()
    for nid, meta in list(_PENDING_ACKS.items()):
        age_ms = now - meta["sent_ms"]
        if age_ms < ACK_WAIT_SEC * 1000:
            continue
        try:
            doc = db.collection("notif_ack").document(nid).get()
            if doc.exists:
                del _PENDING_ACKS[nid]  # 수신 확인됨 — 더 볼 필요 없음(조용히 종료, 로그는 no_ack만 남김)
                continue
        except Exception as e:
            log.warning("[notif] ack 조회 실패(nid=%s): %s — 다음 점검에 재시도", nid, type(e).__name__)
            continue  # 판정 보류, pending 유지
        log.info("[notif][no_ack] pid=%s kind=%s nid=%s age_s=%d", meta["pid"], meta["kind"], nid, age_ms // 1000)
        del _PENDING_ACKS[nid]  # 같은 건을 매 점검마다 다시 로그하지 않는다(1회 보고)


if __name__ == "__main__":
    main()
