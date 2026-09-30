/* 오구오구(5959) PWA 서비스워커 — 앱 셸 캐시 + 자동 업데이트 (팀톡 sw.js 패턴 재사용)
   2단계 A: FCM 백그라운드 알림 핸들러 추가 — 알림 내용 완전 숨김 훅.
   ⚠️정직한 범위: 이 핸들러는 payload에 무엇이 실려오든(릴레이가 실수로 본문을 넣더라도)
   화면에는 항상 고정 문구만 띄운다. 단 실제 발송 인프라(VAPID+릴레이)는 index.html 상단
   주석 참고 — 콘솔 작업(사장님) 전까지는 이 핸들러가 트리거될 발송 자체가 없다. */
const VER = "oguogu-v0.8.31"; // v0.8.31(2026-09-30): 일정·다이어리 등록 알림 문구 표시 + notificationclick 딥링크(postMessage/openWindow)
const SHELL = ["./","./index.html","./manifest.json","./icon-512.png"];

self.addEventListener("install", e=>{
  self.skipWaiting();
  e.waitUntil(caches.open(VER).then(c=>c.addAll(SHELL).catch(()=>{})));
});
self.addEventListener("activate", e=>{
  e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==VER).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));
});
/* 버전 조회 응답 — 설정 화면이 "지금 페이지를 제어 중인 SW 버전"을 읽어 표시(환경 확인용). */
self.addEventListener("message", e=>{
  if(e.data && e.data.type==="version" && e.ports && e.ports[0]){ e.ports[0].postMessage(VER); }
});
self.addEventListener("fetch", e=>{
  const req=e.request;
  if(req.method!=="GET") return;
  const url=new URL(req.url);
  // 같은 출처의 앱 셸만 캐시. Firebase/외부는 항상 네트워크.
  if(url.origin!==location.origin) return;
  if(req.mode==="navigate" || url.pathname.endsWith("index.html") || url.pathname.endsWith("/")){
    e.respondWith(fetch(req).then(r=>{const cp=r.clone();caches.open(VER).then(c=>c.put(req,cp));return r;}).catch(()=>caches.match(req).then(m=>m||caches.match("./index.html"))));
    return;
  }
  e.respondWith(caches.match(req).then(m=>m||fetch(req).then(r=>{const cp=r.clone();caches.open(VER).then(c=>c.put(req,cp));return r;})));
});

/* ----- FCM 백그라운드 알림: 내용 절대 미노출 (payload와 무관하게 고정 문구) ----- */
try{
  importScripts("https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js");
  importScripts("https://www.gstatic.com/firebasejs/10.12.2/firebase-messaging-compat.js");
  firebase.initializeApp({
    apiKey: "AIzaSyDa_6d_j2wW0FZ2wFOsbB9hXgD_u1SLKGw",
    authDomain: "ogu-ogu-app-e4193.firebaseapp.com",
    projectId: "ogu-ogu-app-e4193",
    storageBucket: "ogu-ogu-app-e4193.firebasestorage.app",
    messagingSenderId: "663259886074",
    appId: "1:663259886074:web:dfa2eecf74034bfe923443"
  });
  const messaging = firebase.messaging();
  messaging.onBackgroundMessage((payload) => {
    // 채팅(m)·합류요청(req)은 그대로 본문·발신자 절대 미노출(고정 문구) — 앱 창립 원칙.
    // 일정(sch*)·다이어리(diary*)는 2026-09-30 사장님 지시로 예외: 릴레이가 이름·날짜·제목/첫줄을
    //   이미 문구로 조립해 data.body에 실어 보낸다(scripts/ogu_push_relay.py _send 참고) — 그 문구를
    //   그대로 보여준다. data.tab/data.id는 알림을 탭했을 때 열 화면(딥링크)일 뿐 내용이 아니다.
    const d = (payload && payload.data) || {};
    const t = d.t;
    let body, tag;
    if(t === "req"){ body="연결 요청이 도착했어요"; tag="oguogu-req"; }
    else if(t === "sch" || t === "sch_batch" || t === "sch_date"){ body = d.body || "일정 소식이 있어요"; tag="oguogu-sch"; }
    else if(t === "diary" || t === "diary_batch"){ body = d.body || "다이어리 소식이 있어요"; tag="oguogu-diary"; }
    else { body = "새 소식이 도착했어요"; tag="oguogu-msg"; } // 기본(m 포함 미지정) — 내용 미노출 유지
    self.registration.showNotification("5959", {
      body,
      icon: "./icon-512.png",
      badge: "./icon-512.png",
      tag, // 종류별 1개로 합쳐 볼륨 노출 방지(기존 메시지/요청과 동일 원칙)
      data: { tab: d.tab || "", id: d.id || "" },
    });
  });
}catch(e){ /* 구형 브라우저 등 importScripts 실패 시 캐시 SW 동작만 유지 */ }

self.addEventListener("notificationclick", e=>{
  e.notification.close();
  const nd = e.notification.data || {};
  const tab = nd.tab || "", id = nd.id || "";
  e.waitUntil(clients.matchAll({type:"window", includeUncontrolled:true}).then(list=>{
    for(const c of list){
      if("focus" in c){
        if(tab) c.postMessage({type:"deeplink", tab, id});
        return c.focus();
      }
    }
    let url="./";
    if(tab) url += "?tab="+encodeURIComponent(tab)+(id?"&id="+encodeURIComponent(id):"");
    return clients.openWindow(url);
  }));
});
