# -*- coding: utf-8 -*-
"""
5959(오구오구) "응원팩" 해금 스크립트 — 관리자 전용 (2026-08-19)

왜 스크립트인가:
  결제는 앱 밖(토스 송금)에서 일어난다. 앱은 커플 고유 "주문코드"를 보여주고 사용자가
  "결제 완료했어요"를 누르면 purchase_requests/{pairId}에 접수 1건만 남긴다.
  그 접수 문서에는 해금 권한이 없다 — pairs/{pairId}.premium 은 firestore.rules가
  클라이언트 쓰기를 create/update 양쪽에서 전면 차단한 필드다.
  즉 "돈 안 내고 앱만 조작해서 프리미엄이 켜지는" 경로는 구조적으로 존재하지 않으며,
  입금을 눈으로 확인한 사람이 이 스크립트(Admin SDK = rules 우회)를 돌려야만 켜진다.

사용법:
  python scripts/grant_premium.py list                      # 접수 목록(대기/완료) 확인
  python scripts/grant_premium.py grant 5959-XXXX-XXXX      # 주문코드로 해금
  python scripts/grant_premium.py grant --pair-id <pairId>  # 커플 id 직접 지정(점검·테스트용)
  python scripts/grant_premium.py revoke 5959-XXXX-XXXX     # 되돌리기(오적용·테스트 정리)
  python scripts/grant_premium.py status 5959-XXXX-XXXX     # 현재 상태만 조회
  (공통 옵션) --yes   확인 프롬프트 생략(무인 실행)

입금 확인 → 해금 절차:
  1) 토스 알림에서 입금자 메시지의 주문코드(5959-XXXX-XXXX)를 읽는다.
  2) python scripts/grant_premium.py list  로 그 코드의 접수 건이 있는지 대조한다.
  3) python scripts/grant_premium.py grant <주문코드>
  4) 앱은 실시간 스냅샷으로 즉시 반영된다(사용자 새로고침 불필요).

필요 패키지: pip install firebase-admin   (ogu_push_relay.py와 동일)
서비스계정 키: C:/Users/NHNE/ogu-ogu-secrets/ogu-ogu-service-account.json
              (환경변수 OGU_SA_KEY 로 덮어쓸 수 있음 — 저장소 밖 보관, 절대 커밋 금지)
감사 로그:     C:/Users/NHNE/ogu-ogu-secrets/premium_grants.log
"""
import os, sys, json, argparse
from datetime import datetime, timezone

# 출력 인코딩 고정: 안내 문구에 한글·이모지(✅⏳)가 있어 기본 cp949 콘솔/파이프에서는 UnicodeEncodeError로
# 스크립트가 통째로 죽는다(무인 실행·테스트 하네스에서 exit 1로 관측). 표준 스트림을 UTF-8로 재설정한다.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ID = "ogu-ogu-app-e4193"
SECRETS_DIR = os.environ.get("OGU_SECRETS_DIR", r"C:/Users/NHNE/ogu-ogu-secrets")
SA_KEY = os.environ.get("OGU_SA_KEY", os.path.join(SECRETS_DIR, "ogu-ogu-service-account.json"))
AUDIT_LOG = os.path.join(SECRETS_DIR, "premium_grants.log")
PRODUCT_ID = "support_pack_v1"
PRICE = 1900  # 2026-08-25 18:41 사장님 확정 인하(구 4900) — 표시/로그 전용, 결제 검증에는 안 쓰임


def audit(line):
    """해금·회수는 돈이 걸린 조작이라 반드시 흔적을 남긴다(누가 언제 무엇을)."""
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    try:
        os.makedirs(SECRETS_DIR, exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (stamp, line))
    except Exception as e:
        print("  (감사 로그 기록 실패: %s)" % e)


def db():
    if not os.path.exists(SA_KEY):
        print("서비스계정 키를 찾을 수 없습니다: %s" % SA_KEY)
        print("→ Firebase 콘솔(계정 scjulsan) > 프로젝트 설정 > 서비스 계정 > 새 비공개 키 생성 후 위 경로에 저장하세요.")
        sys.exit(2)
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
    except ImportError:
        print("firebase-admin 패키지가 필요합니다:  pip install firebase-admin")
        sys.exit(2)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(SA_KEY), {"projectId": PROJECT_ID})
    return firestore.client()


def norm_code(code):
    """사장님이 손으로 옮겨 적는 값이라 대소문자·공백·구분자 흔들림을 흡수한다."""
    c = (code or "").strip().upper().replace(" ", "")
    if c and not c.startswith("5959-"):
        c = "5959-" + c.lstrip("-")
    return c


def find_request(fs, code):
    """주문코드 → 접수 문서. 문서 id는 pairId이므로 orderCode 필드로 조회한다."""
    rows = list(fs.collection("purchase_requests").where("orderCode", "==", code).limit(5).stream())
    return rows


def fmt_ts(v):
    try:
        return v.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(v) if v else "-"


def cmd_list(fs, args):
    rows = list(fs.collection("purchase_requests").stream())
    if not rows:
        print("접수된 건이 없습니다.")
        return 0
    rows.sort(key=lambda d: str(d.to_dict().get("requestedAt") or ""))
    print("접수 %d건  (상품 %s, %s원)" % (len(rows), PRODUCT_ID, format(PRICE, ",")))
    print("-" * 76)
    print("%-16s %-24s %-17s %s" % ("주문코드", "커플(pairId)", "요청시각", "해금"))
    print("-" * 76)
    pending = 0
    for d in rows:
        r = d.to_dict()
        pair = fs.collection("pairs").document(d.id).get()
        prem = bool(pair.to_dict().get("premium")) if pair.exists else None
        mark = "✅ 완료" if prem else ("⏳ 대기" if prem is False else "⚠️ 커플없음")
        if prem is False:
            pending += 1
        print("%-16s %-24s %-17s %s" % (r.get("orderCode", "-"), d.id, fmt_ts(r.get("requestedAt")), mark))
    print("-" * 76)
    print("대기 %d건 → python scripts/grant_premium.py grant <주문코드>" % pending)
    return 0


def resolve_pair(fs, args, need_request):
    """주문코드 또는 --pair-id 로 대상 커플을 특정한다."""
    if args.pair_id:
        return args.pair_id, None
    code = norm_code(args.code)
    if not code:
        print("주문코드 또는 --pair-id 가 필요합니다.")
        return None, None
    rows = find_request(fs, code)
    if not rows:
        print("주문코드 %s 로 접수된 건을 찾지 못했습니다." % code)
        print("→ python scripts/grant_premium.py list  로 실제 접수 코드를 확인하세요.")
        print("→ 사용자가 '결제 완료했어요'를 아직 안 눌렀을 수도 있습니다(입금은 됐는데 접수만 없는 경우:")
        print("   앱 설정 > 5959 응원하기 에서 코드를 확인해 --pair-id 로 직접 지정할 수 있습니다).")
        return None, None
    if len(rows) > 1:
        print("⚠️ 같은 주문코드의 접수가 %d건입니다(코드 충돌 가능). 수동 확인 후 --pair-id 로 지정하세요:" % len(rows))
        for d in rows:
            print("   pairId=%s  요청=%s" % (d.id, fmt_ts(d.to_dict().get("requestedAt"))))
        return None, None
    return rows[0].id, rows[0]


def show_pair(fs, pair_id):
    snap = fs.collection("pairs").document(pair_id).get()
    if not snap.exists:
        print("커플 문서를 찾을 수 없습니다: pairs/%s" % pair_id)
        return None
    p = snap.to_dict()
    print("  커플      : %s" % pair_id)
    print("  상태      : %s (멤버 %d명)" % (p.get("status"), len(p.get("memberUids") or [])))
    print("  사진      : %s장" % p.get("photoCount", 0))
    print("  응원팩    : %s" % ("✅ 적용됨 (%s)" % fmt_ts(p.get("premiumAt")) if p.get("premium") else "❌ 미적용"))
    return p


def cmd_status(fs, args):
    pair_id, _ = resolve_pair(fs, args, need_request=False)
    if not pair_id:
        return 1
    return 0 if show_pair(fs, pair_id) else 1


def set_premium(fs, args, on):
    verb = "해금" if on else "회수"
    pair_id, req = resolve_pair(fs, args, need_request=on)
    if not pair_id:
        return 1
    p = show_pair(fs, pair_id)
    if p is None:
        return 1
    if p.get("status") == "dissolved":
        print("⚠️ 정리된(dissolved) 공간입니다. %s를 중단합니다." % verb)
        return 1
    if bool(p.get("premium")) == on:
        print("이미 %s 상태입니다. 변경 없음." % ("적용" if on else "미적용"))
        return 0
    if not args.yes:
        ans = input("\n위 커플에 응원팩을 %s할까요? (y/N) " % verb).strip().lower()
        if ans != "y":
            print("취소했습니다.")
            return 1
    patch = {"premium": bool(on)}
    if on:
        patch["premiumAt"] = datetime.now(timezone.utc)
        patch["premiumSource"] = PRODUCT_ID
    fs.collection("pairs").document(pair_id).update(patch)
    # 접수 문서에도 처리 결과를 남긴다(관리자 대사용 — 클라이언트는 이 필드를 쓸 수 없다).
    if req is not None or args.pair_id:
        ref = fs.collection("purchase_requests").document(pair_id)
        if ref.get().exists:
            ref.update({"grantedAt": datetime.now(timezone.utc) if on else None,
                        "grantState": "granted" if on else "revoked"})
    code = (req.to_dict().get("orderCode") if req is not None else (args.code or "-"))
    print("\n✅ 응원팩 %s 완료 — pairs/%s.premium = %s" % (verb, pair_id, on))
    print("   앱은 실시간 스냅샷으로 즉시 반영됩니다(사용자 새로고침 불필요).")
    audit("%s pair=%s code=%s premium=%s" % (verb, pair_id, code, on))
    return 0


def main():
    ap = argparse.ArgumentParser(description="5959 응원팩 해금(관리자 전용)")
    ap.add_argument("action", choices=["list", "grant", "revoke", "status"])
    ap.add_argument("code", nargs="?", help="주문코드 (5959-XXXX-XXXX)")
    ap.add_argument("--pair-id", dest="pair_id", help="커플 id 직접 지정(점검·테스트용)")
    ap.add_argument("--yes", action="store_true", help="확인 프롬프트 생략")
    args = ap.parse_args()
    fs = db()
    if args.action == "list":
        return cmd_list(fs, args)
    if args.action == "status":
        return cmd_status(fs, args)
    return set_premium(fs, args, on=(args.action == "grant"))


if __name__ == "__main__":
    sys.exit(main())
