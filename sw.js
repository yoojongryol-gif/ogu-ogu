/* 오구오구(5959) PWA 서비스워커 — 앱 셸 캐시 + 자동 업데이트 (팀톡 sw.js 패턴 재사용)
   2단계 A: FCM 백그라운드 알림 핸들러 추가 — 알림 내용 완전 숨김 훅.
   ⚠️정직한 범위: 이 핸들러는 payload에 무엇이 실려오든(릴레이가 실수로 본문을 넣더라도)
   화면에는 항상 고정 문구만 띄운다. 단 실제 발송 인프라(VAPID+릴레이)는 index.html 상단
   주석 참고 — 콘솔 작업(사장님) 전까지는 이 핸들러가 트리거될 발송 자체가 없다. */
const VER = "oguogu-v0.7.7";
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
    // 본문·발신자 절대 미노출. 오직 "종류 플래그"(t: 메시지 m / 합류요청 req)만 읽어 고정 문구를 고른다.
    //   t는 내용이 아니라 카테고리 → 내용 숨김 원칙 유지(문구는 항상 고정).
    const t = payload && payload.data && payload.data.t;
    const isReq = (t === "req");
    self.registration.showNotification("5959", {
      body: isReq ? "연결 요청이 도착했어요" : "새 소식이 도착했어요",
      icon: "./icon-512.png",
      badge: "./icon-512.png",
      tag: isReq ? "oguogu-req" : "oguogu-msg", // 종류별 1개로 합쳐 볼륨 노출 방지
    });
  });
}catch(e){ /* 구형 브라우저 등 importScripts 실패 시 캐시 SW 동작만 유지 */ }

self.addEventListener("notificationclick", e=>{
  e.notification.close();
  e.waitUntil(clients.matchAll({type:"window"}).then(list=>{
    for(const c of list){ if("focus" in c) return c.focus(); }
    return clients.openWindow("./");
  }));
});
