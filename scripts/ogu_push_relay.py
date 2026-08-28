# -*- coding: utf-8 -*-
"""
5959(오구오구) 웹푸시 릴레이 — 서버 없는 구조의 "PC 릴레이".
Firestore의 신규 대화 메시지를 감지해, 그 메시지를 "받는 쪽(상대 멤버)"의 FCM 토큰으로만 푸시를 보낸다.
내용은 절대 싣지 않는다(data-only) → 브라우저/서비스워커가 고정 문구 "새 소식이 도착했어요"만 표시한다.

설계 원칙(사장님 승인 라운드):
  - 본인 메시지엔 미발송(보낸 사람에게는 알림 X). 상대 멤버 토큰으로만.
  - 내용 미노출: notification 블록을 절대 넣지 않는다(data-only). 문구는 sw.js onBackgroundMessage가 고정.
  - 중복 발송 방지: (a) 프로세스 단일 인스턴스 락, (b) 메시지별 처리 상태(state.json)로 재발송 차단.
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
import os, sys, json, time, logging, socket, atexit
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
POLL_SEC = int(os.environ.get("OGU_POLL_SEC", "120"))                      # 메시지 폴링 간격(30 → 120, 읽기 1/4)
MSG_LIMIT = 20                                                             # 한 번에 처리할 신규 메시지 상한
FREE_READ_QUOTA = 50000                                                    # Spark 무료 일일 문서 읽기 한도(비교용)

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
            elif meta.get("joinRequest"):
                sent += process_join_request(db, messaging, pid, meta, state)  # 합류 요청 알림
        # 상태는 항상 저장한다 — 첫 관측(first-sight)으로 세팅한 lastSeen도 반드시 영속화해야
        # 다음 스윕에서 '그 이후' 메시지를 신규로 잡는다(sent==0일 때 미저장 시 매번 재초기화되어 발송 누락).
        save_state(state)
        if sent:
            log.info("이번 스윕 발송 %d건", sent)

        if once:
            break
        time.sleep(POLL_SEC)


def load_paired(db):
    """활성 pair 로드: paired(메시지 알림) + solo/waiting(합류 요청 알림).
    {pid: {'status','members':[..],'fcmTokens':{uid:token},'joinRequest':{...}|None}}."""
    out = {}
    try:
        for d in db.collection("pairs").where("status", "in", ["paired", "solo", "waiting"]).stream():
            data = d.to_dict() or {}
            out[d.id] = {
                "status": data.get("status"),
                "members": data.get("memberUids", []),
                "fcmTokens": data.get("fcmTokens", {}) or {},
                "joinRequest": data.get("joinRequest"),
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
            token = tokens.get(uid)
            if not token:
                continue  # 상대가 알림 미구독
            if _send(messaging, token, pid):
                sent += 1
    if max_ts > last:
        state[pid] = max_ts
    return sent


def _send(messaging, token, pid, kind="m"):
    """data-only 푸시(내용 미노출). notification 블록 없음 → sw.js가 종류(t)별 고정 문구 표시.
    kind: 'm'=새 메시지 / 'req'=합류 요청. t는 카테고리일 뿐 내용이 아니다."""
    try:
        msg = messaging.Message(
            token=token,
            data={"t": kind},  # 내용 없음(종류 신호만)
            webpush=messaging.WebpushConfig(
                headers={"Urgency": "high", "TTL": "1800"},
                # notification 의도적으로 미포함 — 내용 노출 방지(문구는 서비스워커가 고정).
            ),
        )
        messaging.send(msg)
        return True
    except Exception as e:
        name = type(e).__name__
        # 무효/만료 토큰 등은 조용히 스킵(다음 구독 갱신 때 정리됨). 내용은 로그에도 안 남김.
        log.info("[%s] 발송 스킵(%s)", pid, name)
        return False


if __name__ == "__main__":
    main()
