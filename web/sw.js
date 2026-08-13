/* 미리(MIRI) service worker — 앱셸 캐시 + 오프라인 폴백.
   전략: 정적 자산은 cache-first, API(/api/*)는 network-only(항상 실시간 공시).
   설치 가능 요건(manifest + fetch 핸들러 + HTTPS/localhost)을 충족한다. */
const CACHE = 'miri-v45';   // v44→v45: 메인 탭 상단 개편 3건 + 진단 줄 제거(President 지시 2026-08-14).
                            //   ①대표값(평균/중앙/보정) 선택 UI 를 메인 탭 상단 → «설정 탭»(#setValmode)으로 이설.
                            //     기존 설정 관용구(.set-group/.set-row/.set-seg + data-key) 그대로 재사용. 진실원본은
                            //     여전히 전역 VALMODE + localStorage['miri-valmode2'] 하나. ★setValmode 가 이제
                            //     __miriRenderToday() 를 «조건 없이» 부른다 — 종전엔 「오늘 탭이 활성일 때만」이었는데
                            //     선택 UI 가 설정 탭으로 간 순간 그 조건은 «영원히 거짓»이 되고, 나중에 메인 탭에 가도
                            //     loadToday(false) 가 _todayLoaded 가드로 조기 return 해 옛 수치가 그대로 남는다.
                            //   ②새로고침(#btnRefresh) 을 대표값 세그 우측 → 「종목명·코드 검색」 입력줄 «우측»으로.
                            //     .searchrow(flex) 안에 .search-mount(flex:1;min-width:0) + 버튼(34px 고정). 버튼은
                            //     .searchwrap «밖»에 둔다 — #searchWrap 은 단일 노드를 mountSearch 가 탭 사이로 «옮기는»
                            //     구조라 안에 넣으면 관심·캘린더 탭까지 따라간다(메인 탭 전용 계약 유지).
                            //   ③비워진 자리(대표값 세그 자리)에 띠 광고 320x50 — ★«순증이 아니라 이동»이다.
                            //     이미 4개(최상단 320x50 · 카드7 300x250 · 카드14 320x100 · 카드21 300x250)라
                            //     5번째를 넣으면 페이지당 4개 상한(운영정책 + SDK maxAdUnitCount=4)을 넘는다.
                            //     SDK 는 warn 만 하고 넘어가 «조용히» 위반되므로 코드로 지켜야 한다. → #adSlotTop 을
                            //     헤더 아래에서 「밤사이 밴드 아래 / 큐레이션 위」로 내렸다. 총 4개 그대로.
                            //     ★맞닿는 조작요소가 1개(검색입력) → 2개(밤사이 밴드 버튼 / 카드 ★·DART·공유)로 늘었다.
                            //       .adslot.band 의 ±8px 로 12px→20px 만 벌려 완화. President 확인 대상.
                            //   ④진단 줄(AD_DIAG · #adDiag · PerformanceObserver 계측 · NO-AD 콜백 래핑) «전량 삭제».
                            //     원인 규명 완료 — 우리 코드·계정 문제가 아니다(같은 폰에서 크롬 4개 정상/사파리·PWA 0,
                            //     요청은 셋 다 4건). ★<head> 의 «정지(suspend) 1회 정리» 블록은 진단과 독립이라 «존치»
                            //     (VER='v44' 도 그대로 — 올리면 정리가 한 번 더 돌아 정당한 정지까지 지운다).
                            //   index.html·shell.js 가 SHELL precache 라 bump 필수(안 올리면 구셸 영구 고착).
                            // (이전) v43→v44: ★설치 PWA 광고 0 «진짜 원인» 규명 완료 + 수정 3건(2026-08-12).
                            //          원인 = 애드핏 unit 이 «정지(suspend)» 상태로 localStorage['adfit.ba.adUnitSuspendItems']
                            //          에 박혀 있었다. SDK(4.41.0)는 요청 «첫 줄»에서 isSuspended 면 네트워크 이전에
                            //          throw Qe("AD unit is suspended") → 요청 0건 + NO-AD 콜백 즉시 발화. iOS 홈화면 설치
                            //          PWA 는 사파리와 localStorage 파티션이 분리돼 PWA 쪽에만 기록이 남는다 → 사파리만 정상.
                            //          ①suspend 기록 «1회» 정리(플래그 miri-adfitSuspendCleared=v44 로 잠금 — 상시 무력화 아님.
                            //            서버가 다시 400 주면 다시 정지되는 게 정상) ②진단 req 카운터 호스트 수정 —
                            //            실제 호스트는 serv.ds.kakao.com/sdk/banner 인데 종전 목록은 «아무것도 매치 안 돼»
                            //            광고가 떠도 req=0 이 찍혔다(오판의 뿌리). 신/구를 따로 센다 ③진단 줄에 susp= 상시 표시.
                            //          ★기각된 가설 2건(재현 실험으로 죽였다): UA 에서 Safari//Version/ 토큰이 빠져서가 «아니다»
                            //            (PWA UA 로도 광고 4개 정상). v43 의 「숨은 탭」도 «아니다» — SDK 의 그 경로는 onfail 을
                            //            아예 안 부르거나(load() 의 부모 비가시 스킵) 응답 이후라 req>0 이다. 증상과 불일치.
                            //          index.html 이 SHELL precache 라 bump 필수.
                            // (이전) v42→v43: ★숨은 탭 NO-AD 가드 — 설치 PWA 콜드런치가 시작탭을 적용해 #p-today 가 숨은 채
                            //          시작하면 AdFit 이 "Cannot visible ad on screen" 으로 NO-AD 를 부르고, 종전 콜백이 슬롯을 «영구 제거»했다.
                            //          → 숨은 탭에서 온 NO-AD 는 제거하지 않고 보류했다가 탭이 보이면 재요청. + 진단 줄(AD_DIAG) 유지.
                            //          규명 끝나면 AD_DIAG=false + 진단 블록·#adDiag 제거하고 다시 범프할 것.
                            // v40→v41: 「새 공시 N건 보기」가 관심탭 이동 → 밤사이와 «같은 시트»로(President 지시).
                            //          + 가로스크롤 원인 2건(공백없는 긴 공시제목 keep-all / 글자크기 lg 의 zoom×고정폭 광고) 수정.
                            // v39→v40: 광고 4개 배치 — 최상단 띠 320x50 + 인피드 300x250(카드7)·320x100(카드14)·300x250(카드21).
                            //   하단 고정광고 제거. 페이지당 4개 상한(운영정책·SDK 둘 다) 정확히 충족 — President 최종안(2026-08-11).
                            //   인피드는 광고 노드를 «떼지 않는» 제자리 교체(MIRI_ADS.setHTML)라 renderToday 재렌더에도 iframe 재로드 0.
// v34→v35: 운영 주체 표기 추가 — 카카오 애드핏 «매체심사 보류» 대응(2026-08-11 08:23). 보류 사유 = 「매체와 계정의 소유관계가 불명확」. index.html 고지 문구 아래에 「운영 리미 · 대표 권우림 · 사업자등록번호 532-08-03363 · 문의 rimismiri0305@gmail.com」 한 줄, privacy.html §10 을 「운영 주체 및 문의처」로 확장(상호·대표자·사업자등록번호·보호책임자). ★주소는 «넣지 않는다» — 자택 주소라 비공개(President 지시). 애드핏 사유는 소유관계 확인이지 통신판매업 표기가 아니고 재화 판매도 없다. privacy.html 에 남아 있던 내부 메모(「운영 주체 확정 후 보강… 배포하세요」)도 제거 — 그 문구 자체가 «주인 없는 사이트»로 읽혔다. index.html 이 SHELL precache 라 bump 필수
// v33→v34: 설치 PWA(standalone)에서 로딩 화면을 «하나»로 — President 지시(2026-08-11). 기존엔 파란 부트로더(#bootloader, z300) 페이드아웃 «뒤» 스플래시(#splash, z200, 아이콘+이메일+가짜 진행바)가 또 떠서 로딩이 두 번 보였다. standalone 이면 #splash 를 즉시 remove 하고, 완료 판정(.card 1장 존재 · 최소 0.9s · 상한 6s)을 부트로더가 «그대로 승계»한다 — load 직후 숨기면 데이터 도착 전이라 빈 화면이 잠깐 보이기 때문. 일반 브라우저는 #bootloader 가 display:none 이라 스플래시가 유일한 로딩 화면으로 남고 «동작 변화 없음». index.html 이 SHELL precache 라 bump 없으면 기존 설치 유저에게 구셸이 고착된다 → bump 필수
// v32→v33: 카카오 애드핏 배너(300x250, DAN-A7S2FVHoJYfOb2U2) 1개 설치 — President 지시(2026-08-10). 위치=오늘탭 #todayBody «뒤»·.social 앞(큐레이션+밤사이 공시 목록을 전부 지난 자리 → 공시 확인을 가리지 않음. AdFit 운영정책 5.3 "콘텐츠를 덮거나 가리는 영역에 광고 배치 금지"). ★목록 «안»이 아니라 «형제»로 둔 이유: renderToday(shell.js)가 #todayBrief·#todayBody 의 innerHTML 을 통째로 덮는데, AdFit 공식문서에 SPA 재호출 방법이 «없다» → 목록 안에 넣으면 매 갱신마다 슬롯이 소실된다. 폴백은 try/catch 가 아니라 공식 API data-ad-onfail(onAdFitNoAdToday) — ins 가 display:none 으로 시작하므로 실패해도 안 보일 뿐이고 콜백은 flex gap 잔여여백만 회수한다. privacy.html §3·§4 에 카카오 병기(기존 문구가 사실과 달라지므로). ads.txt 는 무변경(AdFit 공식문서 4종 전량에 ads.txt 조항 «없음» — 실측). index.html 이 SHELL precache 라 bump 없으면 기존 설치 유저에게 구셸이 고착돼 광고가 영영 안 내려간다 → bump 필수
// (이전) v31→v32: 새로고침 버튼(#btnRefresh) 아이콘 교체 — President 지시(2026-07-30) "화살표 말고 보편적으로 쓰이는 동그라미 로딩 표시". 비대칭 글리프 ↻ 를 통째로 회전시키던 miriSpin 폐기(돌면 흔들려 어지럽다) → 유휴=SVG 원형 화살표(Feather rotate-cw, MIT / 미니앱 TodayScreen 과 동일 path), 로딩=원호 스피너(.scan i 관용구·@keyframes sp 재사용). 히트영역 34x34·색(--t2)·JS busy 계약 전부 무변경. index.html 이 SHELL precache 라 bump 없으면 기존 설치 유저에게 구셸이 고착돼 안 내려간다 → bump 필수
// (이전) v30→v31: Google Ads 전환태그(gtag.js, AW-18355483264) 를 index.html·privacy.html 의 </head> 직전에 삽입. Google Ads 가 "사이트에서 Google 태그를 찾지 못했다"고 통지 → 전환측정·캠페인 최적화 불가 상태였음. 스니펫은 Google 제공 원문 그대로(async 유지, AdSense 지연로딩 블록은 무접촉). index.html 이 SHELL precache 라 bump 없으면 기존 설치 유저에게 구셸이 고착돼 태그가 영영 안 내려간다 → bump 필수
// (이전) v29→v30: ①뒤로가기 먹통 근본수정 — detailX/detailBack 의 addEventListener('click',closeDetail) 가 MouseEvent 를 1번 인자 fromPop(truthy)으로 넘겨 history.back() 이 통째로 건너뛰어짐 → openDetail 이 쌓은 엔트리가 스택에 유령으로 남아 닫은 뒤 뒤로가기 1회가 헛돌았다. 무인자 래퍼(()=>closeDetail())로 전환(Esc·closeMezz·closeAnalyst 는 이미 래퍼라 정상, 탭전환 closeDetail(true,true) 는 의도적 유지) ②간격체계 Phase4 — .tabpanel gap 하드코딩 12px → var(--sp-3). 5탭 섹션 간격이 표시밀도(6/12/18)에 편입돼 밀도 기능이 카드+섹션 전체에 작동(md=12px 동일 → 기본 밀도 픽셀 무변화). SHELL(index.html·shell.js) precache라 bump 필수(구셸 고착 방지)
// (이전) v28→v29: ①파란 세로선 잔상 근본수정 — .card.watched·.brief.hero-am 의 outset box-shadow(레이아웃 박스 밖 1.5px 띠 = 랭킹탭에선 아무 엘리먼트도 점유 안 하는 죽은 영역 → iOS 재도색 주체 없음)를 inset 으로 전환 ②간격체계 Phase0~2 — --sp-1~6/--inset-x/--pad-x 토큰 도입(--stack-gap=--sp-3 별칭, 밀도 6/12/18 렌더 불변), #formmsg:empty 가드, 세로 마이크로 패딩 제거(.pushrow·.rk-seg·.valwrap·.chips·.wl-grouprow) → 알람켜기↔관심피드 34px→12px. SHELL(index.html·shell.js) precache라 bump 필수(구셸 고착 방지)
// (이전) v27→v28: 랭킹 화면 파란 세로선 결함 4건 수정(①body/html 포커스링 차단 ②탭전환 시 상세·애널·시트 오버레이 즉시 언마운트(잔상 겹침) ③터치 포커스 잔존 차단(mousedown preventDefault, 키보드 링은 유지) ④.rk-row:focus-visible 명시). SHELL(index.html·shell.js) precache라 bump 필수(구셸 고착 방지)
// (이전) v26→v27: 애널리스트 종가 표기에 기준일 병기('7/24 종가 249,500원' — 지표카드 라벨·차트 라벨·안내문구 3곳 통일, current_asof 기준·연도 다르면 YY/M/D). SHELL(index.html·shell.js) precache라 bump 필수(구셸 고착 방지)
const DATA_CACHE = 'miri-data-v1';   // 읽기 API(/api/alerts·today·ranking·mezzanine) 응답 캐시(앱셸과 분리 → activate 정리에서 보존)
/* 41-a iOS 스플래시(11종) — 재방문·오프라인 즉시 렌더용 precache */
const SPLASH = [
  '/splash/splash-640x1136.png', '/splash/splash-750x1334.png', '/splash/splash-828x1792.png',
  '/splash/splash-1125x2436.png', '/splash/splash-1170x2532.png', '/splash/splash-1179x2556.png',
  '/splash/splash-1206x2622.png', '/splash/splash-1242x2688.png', '/splash/splash-1284x2778.png',
  '/splash/splash-1290x2796.png', '/splash/splash-1320x2868.png'
];
const SHELL = ['/', '/index.html', '/manifest.json', '/app/shell.js',
  '/icon.svg', '/icon-192.png', '/icon-512.png', '/icon-maskable-192.png', '/icon-maskable-512.png',
  '/apple-touch-icon.png'].concat(SPLASH);

self.addEventListener('install', (e) => {
  // 개별 add + catch: 스플래시 1종이 없어도 install 이 브릭되지 않게(구셸 고착 방지, 가드레일③)
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => {}))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  const keep = [CACHE, DATA_CACHE];
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => keep.indexOf(k) === -1).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

/* /api/alerts 변경 감지용 시그니처: 접수번호 목록만 비교(generated_at 등 휘발 필드 무시). */
function alertsSig(text) {
  try { const d = JSON.parse(text); return (d.alerts || []).map((a) => a.rcept_no).join(','); }
  catch (_) { return text; }
}
async function notifyClients(msg) {
  const list = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  for (const c of list) { try { c.postMessage(msg); } catch (_) {} }
}
/* stale-while-revalidate: 캐시가 있으면 즉시 반환하고 백그라운드로 갱신.
   갱신본이 캐시와 다르면(신규/삭제 공시) 클라이언트에 알림 → 조용히 교체. */
async function swrAlerts(request) {
  const cache = await caches.open(DATA_CACHE);
  const cached = await cache.match(request);
  const netP = fetch(request).then(async (res) => {
    if (res && res.ok) {
      const toStore = res.clone();
      let changed = true;
      if (cached) {
        try { changed = alertsSig(await cached.clone().text()) !== alertsSig(await res.clone().text()); }
        catch (_) { changed = true; }
      }
      await cache.put(request, toStore);
      if (cached && changed) notifyClients({ type: 'alerts-updated' });
    }
    return res;
  }).catch(() => null);
  if (cached) { netP.catch(() => {}); return cached; }        // 재방문: 캐시 즉시 표시
  const res = await netP;
  if (res) return res;                                         // 최초 방문: 네트워크 대기
  return new Response(JSON.stringify({ alerts: [], offline: true, errors: [] }),
    { status: 200, headers: { 'Content-Type': 'application/json' } });  // 오프라인+캐시없음
}
/* 버그D: /api/today 변경 감지용 시그니처. 전체 바디 비교는 무거우니 안정 필드만
   조합(dataset_as_of + generated_at + overnight.count) — alertsSig와 동일 취지. */
function dataSig(text) {
  try {
    const d = JSON.parse(text);
    const ov = (d.overnight && typeof d.overnight.count === 'number') ? d.overnight.count : '';
    return String(d.dataset_as_of || '') + '|' + String(d.generated_at || '') + '|' + String(ov);
  } catch (_) { return text; }
}
/* 범용 SWR(today·ranking·mezzanine): 캐시 즉시 반환 + 백그라운드 revalidate로 캐시 갱신.
   ranking·mezzanine은 알림 불요(폴링 대상 아님) — 다음 진입/재조회에서 갱신본 수렴.
   /api/today 는 버그D(캐시 즉시반환이 낡은 브리핑을 보여줄 수 있음) 수정 대상이라 alerts와
   동일하게 캐시본↔네트워크본 시그니처가 다를 때만 notify한다(최초 방문·캐시 없음은 알림 제외). */
async function swrData(request, pathname) {
  const cache = await caches.open(DATA_CACHE);
  const cached = await cache.match(request);
  const netP = fetch(request).then(async (res) => {
    if (res && res.ok) {
      const toStore = res.clone();
      if (pathname === '/api/today' && cached) {
        let changed = true;
        try { changed = dataSig(await cached.clone().text()) !== dataSig(await res.clone().text()); }
        catch (_) { changed = true; }
        await cache.put(request, toStore);
        if (changed) notifyClients({ type: 'data-updated', path: pathname });
      } else {
        await cache.put(request, toStore).catch(() => {});
      }
    }
    return res;
  }).catch(() => null);
  if (cached) { netP.catch(() => {}); return cached; }         // 재방문: 캐시 즉시 표시(+백그라운드 갱신)
  const res = await netP;
  if (res) return res;                                          // 최초 방문: 네트워크 대기
  return new Response(JSON.stringify({ offline: true }),
    { status: 200, headers: { 'Content-Type': 'application/json' } });
}
/* SWR 대상 읽기 API 화이트리스트(개인화 없는 공용 데이터만). watchlist 등은 절대 불포함. */
const SWR_DATA_PATHS = { '/api/today': 1, '/api/ranking': 1, '/api/mezzanine': 1 };

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;          // Umami 등 외부 트래픽은 SW 미개입
  if (e.request.method !== 'GET') return;              // 등록/삭제(POST/DELETE)는 통과
  // ⚠️ 개인화 API는 절대 캐시 금지(가드레일②): watchlist 는 항상 네트워크(no-store)
  if (url.pathname.startsWith('/api/watchlist')) return;
  if (url.pathname === '/api/alerts') {                // 피드: SWR + 변경 notify(재방문 즉시표시+오프라인)
    e.respondWith(swrAlerts(e.request));
    return;
  }
  if (SWR_DATA_PATHS[url.pathname]) {                   // today·ranking·mezzanine: 범용 SWR(캐시 즉시+백그라운드 갱신)
    e.respondWith(swrData(e.request, url.pathname));
    return;
  }
  if (url.pathname.startsWith('/api/')) return;        // 그 외 API(개인화·기타)는 항상 네트워크(실시간)
  // HTML/내비게이션은 network-first(항상 최신 UI), 실패 시에만 캐시
  const isHTML = e.request.mode === 'navigate' || url.pathname === '/' || url.pathname.endsWith('.html');
  if (isHTML) {
    e.respondWith(
      fetch(e.request).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      }).catch(() => caches.match(e.request).then((h) => h || caches.match('/index.html')))
    );
    return;
  }
  // 그 외 정적 자산(아이콘 등)은 cache-first
  e.respondWith(
    caches.match(e.request).then((hit) =>
      hit || fetch(e.request).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      }).catch(() => caches.match('/index.html'))
    )
  );
});

/* ---------- 웹푸시(관심종목 신규 공시) ----------
   서버가 {title, body, url, rcept} JSON 을 payload 로 보낸다. 관심종목 공시만
   발송(브로드캐스트 아님). 클릭 시 앱을 연다(외부 링크 아님). */
self.addEventListener('push', (e) => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; }
  catch (_) { data = { body: (e.data && e.data.text) ? e.data.text() : '' }; }
  const title = data.title || 'MIRI 공시 알림';
  const opts = {
    body: data.body || '',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    data: { url: data.url || '/' },
    tag: data.rcept || undefined,   // 같은 공시 중복 알림 접힘
  };
  e.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        // 이미 열린 앱 창이 있으면 포커스(중복 탭 방지)
        if ('focus' in c) { if (c.navigate && url !== '/') { try { c.navigate(url); } catch (_) {} } return c.focus(); }
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});
