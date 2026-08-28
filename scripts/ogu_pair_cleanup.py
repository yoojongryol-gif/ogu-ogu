# -*- coding: utf-8 -*-
"""
5959(오구오구) 잔재 pair 정리 도구 — 관리자 전용 (2026-08-28)

왜 필요한가 (2026-08-28 Firestore 429 폭주 RCA):
  dev/rules-live.js 테스트가 만든 pair 중 11개가 dissolve에 실패해 활성(paired/solo/waiting)으로
  영구 잔류했다. 푸시 릴레이는 활성 pair를 주기 폴링하는데, Firestore는 결과 0건인 쿼리에도
  문서 읽기 1건을 최소 과금한다. 즉 죽은 테스트 pair 1개가 하루 수백~수천 read를 영구히 먹는다.
  → 잔재를 status="dissolved"로 되돌리면 릴레이 쿼리에서 빠진다. (삭제가 아니라 상태 전환)

사용법:
  python scripts/ogu_pair_cleanup.py --list                  # 전수 조회 + 실사용 판정 근거 출력
  python scripts/ogu_pair_cleanup.py --list --quick          # 메시지 조회 생략(읽기 절반)
  python scripts/ogu_pair_cleanup.py --dissolve ID,ID,...    # 지정 pair만 dissolved 전환(확인 프롬프트)
  python scripts/ogu_pair_cleanup.py --dissolve ID --yes     # 프롬프트 생략(무인)
  python scripts/ogu_pair_cleanup.py --selftest              # 목 데이터 자가검증(Firestore 미접촉)

안전장치:
  - 보호 목록(PROTECTED_PAIR_IDS)은 어떤 옵션으로도 dissolve 되지 않는다.
    · L0EZ1zeNtIbP7pYKy3ai = 8/25 주문코드 5959-NNDV-L0JU 해금 + 8/28 실푸시 발송 이력 = 실사용.
  - 8/5 릴레이 최초 가동 배치 7개(초기 실사용 의심)는 --i-know-legacy 없이는 거부한다.
  - 토큰이 살아있는 pair는 기본 거부(--force로만 통과) — 푸시 구독 중 = 사람이 쓰는 중일 수 있다.
  - dissolve 전 대상 목록을 라이브에서 다시 읽어 눈으로 확인시킨 뒤에만 PATCH 한다.
  - 집행 기록: C:/종합상사/logs/live_ops.log  +  C:/Users/NHNE/ogu-ogu-secrets/ogu_pair_cleanup.log

읽기 비용: --list 는 pairs 전수(약 23 read) + pair당 메시지 1건 프로브(약 23 read).
          --quick 이면 프로브 생략. 무료 한도 50,000 대비 무시 가능하나, 429 상태에서는 실패한다.
          (쿼터 리셋 = 미국 태평양시 자정 = KST 16:00)

필요 패키지: pip install firebase-admin
서비스계정 키: C:/Users/NHNE/ogu-ogu-secrets/ogu-ogu-service-account.json (환경변수 OGU_SA_KEY)
"""
import os, sys, json, argparse
from datetime import datetime, timezone, timedelta

for _s in (sys.stdout, sys.stderr):  # cp949 콘솔에서 한글·기호로 죽지 않게(grant_premium.py와 동일 관례)
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ID = "ogu-ogu-app-e4193"
SECRETS_DIR = os.environ.get("OGU_SECRETS_DIR", r"C:/Users/NHNE/ogu-ogu-secrets")
SA_KEY = os.environ.get("OGU_SA_KEY", os.path.join(SECRETS_DIR, "ogu-ogu-service-account.json"))
RELAY_STATE = os.path.join(SECRETS_DIR, "ogu_push_relay_state.json")
AUDIT_LOG = os.path.join(SECRETS_DIR, "ogu_pair_cleanup.log")
LIVE_OPS_LOG = r"C:/종합상사/logs/live_ops.log"

ACTIVE_STATUSES = ["paired", "solo", "waiting"]   # 릴레이가 폴링하는 상태(load_paired와 동일)
KST = timezone(timedelta(hours=9))

# ── 보호 목록: 절대 dissolve 금지 ──────────────────────────────────────
PROTECTED_PAIR_IDS = {
    # 8/25 18:21 주문코드 5959-NNDV-L0JU 로 응원팩 해금 + 8/28 09:35 실제 메시지 푸시 발송 = 실사용 확정.
    "L0EZ1zeNtIbP7pYKy3ai": "실사용(8/25 주문코드 해금 · 8/28 실푸시)",
}
# ── 8/5 릴레이 최초 가동 시점부터 존재한 배치: 실사용 의심 → --i-know-legacy 필요 ──
LEGACY_2026_08_05 = {
    "7HqyMrwy3uYUYB39x8V4", "CTlBjlb7exv40vPSIYpW", "IOve3ESTWtAR8DFZ2yW1",
    "REYa2rs8Pd0vbJYgWrm8", "fkHeWRrcMpGHPSFjnXc0", "sOPRyrrjjuRHwLlWRi7Y",
    "7BIpSQA6vJQiSTvTXG50",
}
# ── rules-live.js 3회 실행(8/27 13:10~13:12)의 잔재로 릴레이가 처음 관측한 시각대 ──
LEAK_WINDOW = (datetime(2026, 8, 27, 13, 0, tzinfo=KST), datetime(2026, 8, 27, 13, 30, tzinfo=KST))


def _kst(dt):
    if dt is None:
        return "-"
    try:
        return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(dt)


def load_relay_first_seen():
    """릴레이 state.json = pair를 처음 본 시각(ms). 라이브를 읽지 않는 무료 근거."""
    out = {}
    try:
        raw = json.load(open(RELAY_STATE, encoding="utf-8"))
    except Exception:
        return out
    for k, v in raw.items():
        if k.startswith("req:"):
            continue
        try:
            out[k] = datetime.fromtimestamp(int(v) / 1000.0, timezone.utc)
        except Exception:
            pass
    return out


def classify(pid, info):
    """실사용/정리후보 판정 + 근거. info = dict(status, tokens, members, created, first_seen, last_msg, msg_count)."""
    reasons = []
    if pid in PROTECTED_PAIR_IDS:
        return "보호", [PROTECTED_PAIR_IDS[pid]]
    if pid in LEGACY_2026_08_05:
        reasons.append("8/5 최초배치(실사용 의심)")
    if info.get("tokens"):
        reasons.append("FCM 토큰 %d개(푸시 구독 중)" % info["tokens"])
    if info.get("msg_count"):
        reasons.append("메시지 있음(마지막 %s)" % _kst(info.get("last_msg")))
    created = info.get("created") or info.get("first_seen")
    in_window = bool(created and LEAK_WINDOW[0] <= created.astimezone(KST) <= LEAK_WINDOW[1])
    if in_window:
        reasons.append("8/27 13:1x 생성(rules-live 잔재 시간대)")

    if pid in LEGACY_2026_08_05 or info.get("tokens") or info.get("msg_count"):
        return "실사용/보류", reasons or ["판단 근거 부족"]
    if in_window:
        return "정리후보", reasons
    return "보류", reasons or ["토큰·메시지 없음이나 잔재 시간대 아님 → 사람 눈 확인 필요"]


def dissolve_guard(pid, info, force=False, allow_legacy=False):
    """dissolve 허용 여부. Firestore 미접촉 순수 함수 = 셀프테스트 대상."""
    if pid in PROTECTED_PAIR_IDS:
        return False, "보호 목록 — " + PROTECTED_PAIR_IDS[pid]
    if info.get("missing"):
        return False, "문서 없음"
    if info.get("status") not in ACTIVE_STATUSES:
        return False, "이미 비활성(status=%s) — 릴레이가 폴링하지 않음" % info.get("status")
    if pid in LEGACY_2026_08_05 and not allow_legacy:
        return False, "8/5 최초배치 — 확신하면 --i-know-legacy"
    if info.get("tokens") and not force:
        return False, "FCM 토큰 %d개 살아있음(구독 중) — 확신하면 --force" % info["tokens"]
    return True, "ok"


def _audit(line):
    stamp = datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S%z")
    row = "%s  [ogu_pair_cleanup] %s\n" % (stamp, line)
    for path in (AUDIT_LOG, LIVE_OPS_LOG):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(row)
        except Exception as e:
            print("  ⚠ 로그 기록 실패(%s): %s" % (path, e))
    print("  · 기록: " + row.strip())


# ── 라이브 접근 ────────────────────────────────────────────────────────
def _client():
    if not os.path.exists(SA_KEY):
        print("서비스계정 키가 없습니다: %s" % SA_KEY)
        sys.exit(2)
    import firebase_admin
    from firebase_admin import credentials, firestore
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(SA_KEY))
    return firestore.client()


def _probe_messages(db, pid):
    """메시지 1건만 최신순으로 프로브(존재여부 + 마지막 활동). pair당 최소 1 read."""
    from firebase_admin import firestore as _fs
    try:
        docs = list(db.collection("pairs").document(pid).collection("messages")
                    .order_by("ts", direction=_fs.Query.DESCENDING).limit(1).get())
        if not docs:
            return 0, None
        ts = (docs[0].to_dict() or {}).get("ts")
        return 1, ts
    except Exception as e:
        print("  ⚠ [%s] 메시지 프로브 실패: %s" % (pid, e))
        return None, None


def _read_pair(db, pid, snap=None, quick=False):
    if snap is None:
        snap = db.collection("pairs").document(pid).get()
        if not snap.exists:
            return {"missing": True, "status": None, "tokens": 0, "members": []}
    data = snap.to_dict() or {}
    tokens = {k: v for k, v in (data.get("fcmTokens") or {}).items() if v}
    info = {
        "missing": False,
        "status": data.get("status"),
        "tokens": len(tokens),
        "members": data.get("memberUids", []) or [],
        "created": getattr(snap, "create_time", None),
        "updated": getattr(snap, "update_time", None),
        "premium": data.get("premium"),
        "msg_count": None,
        "last_msg": None,
    }
    if not quick and info["status"] in ACTIVE_STATUSES:
        info["msg_count"], info["last_msg"] = _probe_messages(db, pid)
    return info


def cmd_list(args):
    db = _client()
    first_seen = load_relay_first_seen()
    print("[pairs 전수 조회] project=%s  (활성 %s + 그 외 전부)" % (PROJECT_ID, "/".join(ACTIVE_STATUSES)))
    reads = 0
    rows = []
    for snap in db.collection("pairs").stream():
        reads += 1
        info = _read_pair(db, snap.id, snap=snap, quick=args.quick)
        if info["msg_count"] is not None and not args.quick:
            reads += 1
        info["first_seen"] = first_seen.get(snap.id)
        verdict, reasons = classify(snap.id, info)
        rows.append((snap.id, info, verdict, reasons))

    rows.sort(key=lambda r: (r[2] != "정리후보", str(r[1].get("created"))))
    print("\n%-24s %-9s %-19s %-19s %-5s %-5s %-11s %s" %
          ("pairId", "status", "생성(KST)", "릴레이 첫관측", "토큰", "메시지", "판정", "근거"))
    print("-" * 150)
    for pid, info, verdict, reasons in rows:
        print("%-24s %-9s %-19s %-19s %-5s %-5s %-11s %s" % (
            pid, info["status"], _kst(info.get("created")), _kst(info.get("first_seen")),
            info["tokens"], "-" if info["msg_count"] is None else info["msg_count"],
            verdict, " / ".join(reasons)))

    active = [r for r in rows if r[1]["status"] in ACTIVE_STATUSES]
    cand = [r[0] for r in rows if r[2] == "정리후보"]
    polled = [r for r in active if r[1]["status"] == "paired" and r[1]["tokens"]]
    print("-" * 150)
    print("전체 %d개 / 활성 %d개 / 릴레이 메시지폴링 대상(paired+토큰) %d개 / 정리후보 %d개"
          % (len(rows), len(active), len(polled), len(cand)))
    print("소비한 읽기: 약 %d건 (무료 한도 50,000)" % reads)
    if cand:
        print("\n정리 명령(집행 전 위 표를 눈으로 재확인):")
        print("  python scripts/ogu_pair_cleanup.py --dissolve %s" % ",".join(cand))
    else:
        print("\n정리후보 없음 — dissolve 불필요.")
    print("보호 목록(항상 제외): %s" % ", ".join(PROTECTED_PAIR_IDS))


def cmd_dissolve(args):
    ids = [x.strip() for x in args.dissolve.split(",") if x.strip()]
    if not ids:
        print("대상 id가 없습니다."); sys.exit(1)
    db = _client()
    first_seen = load_relay_first_seen()

    print("[집행 전 재확인] 대상 %d건을 라이브에서 다시 읽습니다." % len(ids))
    plan = []
    for pid in ids:
        info = _read_pair(db, pid, quick=args.quick)
        info["first_seen"] = first_seen.get(pid)
        verdict, reasons = classify(pid, info) if not info["missing"] else ("없음", ["문서 없음"])
        allowed, why = dissolve_guard(pid, info, force=args.force, allow_legacy=args.i_know_legacy)
        plan.append((pid, info, verdict, reasons, allowed, why))
        print("  %s  status=%s 토큰=%s 메시지=%s 생성=%s 판정=%s → %s%s" % (
            pid, info["status"], info["tokens"],
            "-" if info["msg_count"] is None else info["msg_count"],
            _kst(info.get("created")), verdict,
            "집행" if allowed else "거부", "" if allowed else " (%s)" % why))

    go = [p for p in plan if p[4]]
    if not go:
        print("\n집행 대상 0건 — 종료(변경 없음).")
        return
    print("\n집행 대상 %d건: %s" % (len(go), ", ".join(p[0] for p in go)))
    print("동작: status → \"dissolved\" (문서 삭제 아님, 되돌릴 수 있음)")
    if not args.yes:
        try:
            if input("진행할까요? (yes 입력): ").strip().lower() != "yes":
                print("취소했습니다(변경 없음)."); return
        except EOFError:
            print("비대화 실행 — --yes 없이는 집행하지 않습니다."); return

    from firebase_admin import firestore as _fs
    done, failed = [], []
    for pid, info, _v, _r, _a, _w in go:
        try:
            db.collection("pairs").document(pid).update({
                "status": "dissolved",
                "dissolvedAt": _fs.SERVER_TIMESTAMP,
                "dissolvedBy": "ogu_pair_cleanup",
            })
            done.append(pid)
            print("  ✅ %s dissolved" % pid)
        except Exception as e:
            failed.append((pid, type(e).__name__))
            print("  ❌ %s 실패: %s" % (pid, e))
    _audit("dissolve 집행 성공=%d(%s) 실패=%d(%s) 거부=%d" % (
        len(done), ",".join(done) or "-", len(failed),
        ",".join("%s:%s" % f for f in failed) or "-", len(plan) - len(go)))
    print("\n완료: 성공 %d / 실패 %d / 거부 %d" % (len(done), len(failed), len(plan) - len(go)))
    print("릴레이는 다음 pairs 갱신(최대 5분) 때 이 pair들을 폴링 대상에서 제외합니다.")


# ── 셀프테스트(목 데이터, Firestore 미접촉) ────────────────────────────
def cmd_selftest():
    fails, total = [], [0]

    def chk(name, cond, extra=""):
        total[0] += 1
        print(("  ✓ " if cond else "  ✗ FAIL ") + name + ("" if cond else "  → " + str(extra)))
        if not cond:
            fails.append(name)

    leak = {"missing": False, "status": "paired", "tokens": 0, "members": ["a", "b"],
            "created": datetime(2026, 8, 27, 13, 11, tzinfo=KST), "msg_count": 0, "last_msg": None}
    live = {"missing": False, "status": "paired", "tokens": 2, "members": ["a", "b"],
            "created": datetime(2026, 8, 20, 9, 0, tzinfo=KST), "msg_count": 1,
            "last_msg": datetime(2026, 8, 28, 9, 35, tzinfo=KST)}
    gone = {"missing": False, "status": "dissolved", "tokens": 0, "members": [],
            "created": datetime(2026, 8, 27, 13, 11, tzinfo=KST), "msg_count": 0, "last_msg": None}

    print("[selftest] 판정 로직")
    chk("잔재(8/27 13:1x·토큰0·메시지0) = 정리후보", classify("XXleakXX", leak)[0] == "정리후보")
    chk("토큰 있는 pair = 실사용/보류", classify("XXliveXX", live)[0] == "실사용/보류")
    chk("보호 pair는 판정도 '보호'", classify("L0EZ1zeNtIbP7pYKy3ai", live)[0] == "보호")
    chk("8/5 배치는 실사용/보류", classify("7HqyMrwy3uYUYB39x8V4", leak)[0] == "실사용/보류")

    print("[selftest] dissolve 안전장치")
    ok1, w1 = dissolve_guard("L0EZ1zeNtIbP7pYKy3ai", leak)
    chk("보호 pair는 기본 거부", not ok1, w1)
    ok2, w2 = dissolve_guard("L0EZ1zeNtIbP7pYKy3ai", leak, force=True, allow_legacy=True)
    chk("보호 pair는 --force --i-know-legacy 로도 거부", not ok2, w2)
    chk("잔재 pair는 허용", dissolve_guard("XXleakXX", leak)[0])
    chk("토큰 있는 pair는 기본 거부", not dissolve_guard("XXliveXX", live)[0])
    chk("토큰 있어도 --force면 허용", dissolve_guard("XXliveXX", live, force=True)[0])
    chk("8/5 배치는 기본 거부", not dissolve_guard("7HqyMrwy3uYUYB39x8V4", leak)[0])
    chk("8/5 배치는 --i-know-legacy면 허용", dissolve_guard("7HqyMrwy3uYUYB39x8V4", leak, allow_legacy=True)[0])
    chk("이미 dissolved면 거부(무의미한 쓰기 방지)", not dissolve_guard("XXgoneXX", gone)[0])
    chk("문서 없으면 거부", not dissolve_guard("XXnoneXX", {"missing": True})[0])

    print("[selftest] state.json 파싱(로컬 파일, 라이브 미접촉)")
    fs_map = load_relay_first_seen()
    chk("첫관측 맵 로드(%d개)" % len(fs_map), isinstance(fs_map, dict))
    chk("req: 키는 제외", not any(k.startswith("req:") for k in fs_map))

    print("\n결과: %d pass / %d fail (Firestore 접속 0회)" % (total[0] - len(fails), len(fails)))
    sys.exit(1 if fails else 0)


def main():
    ap = argparse.ArgumentParser(description="5959 잔재 pair 조회/정리 (dissolved 전환)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="pairs 전수 조회 + 실사용 판정 근거")
    g.add_argument("--dissolve", metavar="ID,ID", help="지정 pair를 status=dissolved 로 전환")
    g.add_argument("--selftest", action="store_true", help="목 데이터 자가검증(Firestore 미접촉)")
    ap.add_argument("--quick", action="store_true", help="메시지 프로브 생략(읽기 절반)")
    ap.add_argument("--yes", action="store_true", help="확인 프롬프트 생략")
    ap.add_argument("--force", action="store_true", help="FCM 토큰이 살아있어도 집행")
    ap.add_argument("--i-know-legacy", action="store_true", dest="i_know_legacy",
                    help="8/5 최초배치 pair도 집행(보호 목록은 여전히 불가)")
    args = ap.parse_args()
    if args.selftest:
        cmd_selftest()
    elif args.list:
        cmd_list(args)
    else:
        cmd_dissolve(args)


if __name__ == "__main__":
    main()
