/* 미리(MIRI) 5탭 셸 라우터 — venture-frontend.
   classic script(모듈 아님): index.html 인라인 스크립트와 같은 전역 렉시컬 스코프를 공유한다.
   → STATE, FEED_OBSERVER, renderFeed, renderFilter, feedSwap, passFilter, cardHTML,
     attachSegToggle, loadMezz, closeDetail, closeSheet, jget, esc, valPick, WLSTATE 등을
     '맨이름'으로 직접 참조/대입한다(window.X 아님 — let/const 전역은 window 속성이 아니므로).
   해시 라우터는 '탭 해시(#today/#watch/#ranking/#calendar/#settings)'만 소유한다.
   종목상세(#detail)는 전역 모달로 자기 pushState 뒤로가기 트랩을 그대로 유지(무접촉). */
(function(){
  'use strict';
  var TABS=['today','watch','ranking','calendar','settings'];
  var TAB_SET={}; TABS.forEach(function(t){TAB_SET[t]=true;});
  var TAB_TITLES={today:null,watch:'관심종목',ranking:'랭킹',calendar:'캘린더',settings:'설정'};
  window.CUR_TAB='today';

  var _todayLoaded=false;
  var WATCH_SEG='feed';           // 'feed'(관심피드) | 'inbox'(알림함)
  var RANK_SEG='cap', RANK_DATA=[];
  var TOP100_DATA=[], _top100Loaded=false;   // 트랙2: 시총 Top100 캐시
  // 트랙1 QA 폴백: /api/analyst 404 시 렌더 보장용 프로토 인라인 픽스처(주경로는 API). 백엔드 준비되면 미사용.
  var ANALYST_FIXTURE={"005930":{"name":"삼성전자","current":262000,"avg_tp":416333,"n_total":33,"n_tp":15,"window_start":"2026-02-25","prices":[["2026-02-25",205500],["2026-02-26",223000],["2026-02-27",217500],["2026-03-03",186000],["2026-03-04",181600],["2026-03-05",193500],["2026-03-06",185800],["2026-03-09",171500],["2026-03-10",189700],["2026-03-11",189200],["2026-03-12",188400],["2026-03-13",184000],["2026-03-16",191000],["2026-03-17",194800],["2026-03-18",209500],["2026-03-19",198700],["2026-03-20",199800],["2026-03-23",185300],["2026-03-24",190700],["2026-03-25",191700],["2026-03-26",180200],["2026-03-27",176800],["2026-03-30",177000],["2026-03-31",166700],["2026-04-01",188100],["2026-04-02",175600],["2026-04-03",185800],["2026-04-06",196800],["2026-04-07",192700],["2026-04-08",213000],["2026-04-09",203000],["2026-04-10",207000],["2026-04-13",200500],["2026-04-14",209000],["2026-04-15",212000],["2026-04-16",217500],["2026-04-17",218000],["2026-04-20",215500],["2026-04-21",221000],["2026-04-22",217500],["2026-04-23",222500],["2026-04-24",219000],["2026-04-27",225000],["2026-04-28",220000],["2026-04-29",226500],["2026-04-30",222500],["2026-05-04",230000],["2026-05-06",274000],["2026-05-07",270500],["2026-05-08",276500],["2026-05-11",285500],["2026-05-12",272500],["2026-05-13",286500],["2026-05-14",293000],["2026-05-15",273500],["2026-05-18",281500],["2026-05-19",273500],["2026-05-20",280500],["2026-05-21",295000],["2026-05-22",293000],["2026-05-26",302500],["2026-05-27",313500],["2026-05-28",295500],["2026-05-29",318500],["2026-06-01",355000],["2026-06-02",362500],["2026-06-04",342000],["2026-06-05",329000],["2026-06-08",303000],["2026-06-09",327000],["2026-06-10",297500],["2026-06-11",306000],["2026-06-12",324500],["2026-06-15",341500],["2026-06-16",343000],["2026-06-17",343000],["2026-06-18",363500],["2026-06-19",350500],["2026-06-22",356500],["2026-06-23",310500],["2026-06-24",339500],["2026-06-25",359500],["2026-06-26",339000],["2026-06-29",325000],["2026-06-30",333000],["2026-07-01",316000],["2026-07-02",290500],["2026-07-03",314500],["2026-07-06",320000],["2026-07-07",291000],["2026-07-08",267000],["2026-07-09",282500],["2026-07-10",286500],["2026-07-13",257500],["2026-07-14",268000],["2026-07-15",273500],["2026-07-16",253500],["2026-07-20",251500],["2026-07-21",263500],["2026-07-22",262000]],"reports":[{"date":"2026-03-03","title":"","target_price":265000,"opinion":"N/A","broker":"iM증권"},{"date":"2026-04-01","title":"","target_price":280000,"opinion":"Buy","broker":"iM증권"},{"date":"2026-04-08","title":"","target_price":300000,"opinion":"Buy","broker":"한화투자증권"},{"date":"2026-04-08","title":"","target_price":350000,"opinion":"매수","broker":"IBK투자증권"},{"date":"2026-04-08","title":"","target_price":300000,"opinion":"Buy","broker":"iM증권"},{"date":"2026-04-15","title":"","target_price":270000,"opinion":"Buy","broker":"LS증권"},{"date":"2026-05-18","title":"","target_price":400000,"opinion":"Buy","broker":"iM증권"},{"date":"2026-06-22","title":"","target_price":480000,"opinion":"Buy","broker":"iM증권"},{"date":"2026-06-25","title":"","target_price":560000,"opinion":"Buy","broker":"대신증권"},{"date":"2026-07-03","title":"","target_price":480000,"opinion":"Buy","broker":"iM증권"},{"date":"2026-07-06","title":"","target_price":500000,"opinion":"Buy","broker":"메리츠증권"},{"date":"2026-07-06","title":"","target_price":560000,"opinion":"Strong Buy","broker":"유진투자증권"},{"date":"2026-07-08","title":"","target_price":480000,"opinion":"Buy","broker":"iM증권"},{"date":"2026-07-08","title":"","target_price":460000,"opinion":"매수","broker":"IBK투자증권"},{"date":"2026-07-08","title":"","target_price":560000,"opinion":"Strong Buy","broker":"유진투자증권"}]},"010130":{"name":"고려아연","current":997000,"avg_tp":1866667,"n_total":13,"n_tp":3,"window_start":"2026-02-25","prices":[["2026-02-25",2115000],["2026-03-16",1620000],["2026-04-01",1515000],["2026-04-17",1687000],["2026-05-08",1560000],["2026-06-01",1375000],["2026-06-15",1270000],["2026-07-01",1093000],["2026-07-16",1009000],["2026-07-22",997000]],"reports":[{"date":"2026-04-17","title":"","target_price":1950000,"opinion":"Buy","broker":"iM증권"},{"date":"2026-05-08","title":"","target_price":1950000,"opinion":"Buy","broker":"iM증권"},{"date":"2026-06-15","title":"","target_price":1700000,"opinion":"Buy","broker":"iM증권"}]}};

  // 브랜드 상단바 원본(오늘 탭용) 캡처
  var _tb=document.getElementById('topbar');
  var BRAND=_tb?_tb.innerHTML:'';

  function updateTopbar(tab){
    var tb=document.getElementById('topbar'); if(!tb)return;
    if(tab==='today'||!TAB_TITLES[tab]){ tb.innerHTML=BRAND; }
    else { tb.innerHTML='<span class="htitle">'+TAB_TITLES[tab]+'</span>'+
      '<span class="live"><span class="d"></span>실시간</span>'; }
  }

  function setBadge(id,n){
    var el=document.getElementById(id); if(!el)return;
    if(n>0){ el.textContent=(n>99?'99+':String(n)); el.hidden=false; }
    else { el.hidden=true; }
  }
  /* ── 확인('seen') 집합: 관심 신규 이슈를 확인(알림함 진입/상세 오픈)하면 rcept_no를
        localStorage('miri-seen')에 영속 → 관심 빨간점에서 제외(소멸). NEW=백엔드 is_new(최근3일창). ── */
  var SEEN_KEY='miri-seen';
  function _seenSet(){ try{var v=JSON.parse(localStorage.getItem(SEEN_KEY)||'[]');return (v&&v.length)?v:[];}catch(e){return [];} }
  function _seenHas(set,id){ return id!=null&&set.indexOf(String(id))>=0; }
  function _seenSave(set){ try{localStorage.setItem(SEEN_KEY,JSON.stringify(set.slice(-500)));}catch(e){} } // 최근 500건 상한(무한증식 방지)
  // 관심 여부는 a.is_watched(index.html가 매김, stale/타이밍으로 빈 목록에도 샐 수 있음) 대신 WLSTATE.stocks 에서
  // 직접 파생(항목17). 빈 워치리스트면 ws 비어 → 관심 카운트 0 보장.
  function _watchedSet(){
    var ws={}; (typeof WLSTATE!=='undefined'&&WLSTATE.stocks?WLSTATE.stocks:[]).forEach(function(s){ ws[String(s.stock_code)]=1; });
    return ws;
  }
  // 관심 신규(미확인)를 seen 처리. codeFilter 주면 해당 종목만(상세 오픈), 없으면 전부(알림함 진입).
  function markSeenWatchedNew(codeFilter){
    var items=(typeof STATE!=='undefined'&&STATE.items)?STATE.items:[];
    var ws=_watchedSet(), set=_seenSet(), changed=false;
    items.forEach(function(a){
      if(codeFilter!=null&&String(a.stock_code)!==String(codeFilter))return;
      if(a.is_new&&a.stock_code&&ws[String(a.stock_code)]&&a.rcept_no&&!_seenHas(set,a.rcept_no)){ set.push(String(a.rcept_no)); changed=true; }
    });
    if(changed)_seenSave(set);
    return changed;
  }
  /* 항목46: 오늘 배지 = 밤사이 신규 수(overnight.count)와 정합. renderToday 로드 시 _overnightCnt 확정,
     로드 전 폴백=오늘자 is_new 수. 99+ 상시포화 제거 + 브리핑 "밤사이 N건"과 숫자 일치. */
  var _overnightCnt=null;
  function _todayYmd(){ var d=new Date(); return d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2); }
  function _isTodayItem(a){ if(!a)return false; var raw=a.date||a.rcept_dt||''; return !!raw&&_fmtYmd(raw)===_todayYmd(); }
  /* 항목9(배지 미소멸 버그): 오늘 배지 = '미확인 밤사이 신규'. 메인 탭 진입+피드 노출 시 확인처리(당일 서명 저장)
     → 배지 소멸. 이후 같은 날 새 공시가 더 들어오면(현재수 > 확인시점 수) 그 델타만큼 재점등. 날짜 바뀌면 서명 무효. */
  var OVSEEN_KEY='miri-today-seen';
  function _rawTodayCnt(){
    var items=(typeof STATE!=='undefined'&&STATE.items)?STATE.items:[];
    return (_overnightCnt!=null)?_overnightCnt:items.filter(function(a){return a.is_new&&_isTodayItem(a);}).length;
  }
  function _ovSeenGet(){ try{return JSON.parse(localStorage.getItem(OVSEEN_KEY)||'null')||null;}catch(e){return null;} }
  function markTodaySeen(){   // 메인 탭 확인 = 배지 소멸 + 당일 확인시점 수 저장
    try{localStorage.setItem(OVSEEN_KEY,JSON.stringify({ymd:_todayYmd(),count:_rawTodayCnt()}));}catch(e){}
    setBadge('badgeToday',0);
  }
  function _todayBadgeCnt(){   // 미확인 밤사이 = 현재수 - 당일 확인시점 수(다른날/미확인=0 기준)
    var raw=_rawTodayCnt(), s=_ovSeenGet();
    var seenCnt=(s&&s.ymd===_todayYmd())?(s.count||0):0;
    return Math.max(0, raw-seenCnt);
  }
  window.markTodaySeen=markTodaySeen;
  /* 오늘 배지=미확인 밤사이 신규(항목9) · 관심 배지=관심(WLSTATE 직접파생) 신규 중 '미확인'만(!seen). */
  function updateTabBadges(){
    var items=(typeof STATE!=='undefined'&&STATE.items)?STATE.items:[];
    var seen=_seenSet(), ws=_watchedSet();
    var inboxCnt=items.filter(function(a){return a.is_new&&a.stock_code&&ws[String(a.stock_code)]&&!_seenHas(seen,a.rcept_no);}).length;
    // 메인 탭을 보고 있고 오늘 데이터 확정이면 = 확인상태(배지 0 + 서명). 아니면 미확인 델타 표시.
    if(window.CUR_TAB==='today'&&_todayLoaded)markTodaySeen();
    else setBadge('badgeToday',_todayBadgeCnt());
    setBadge('badgeWatch',inboxCnt);
    // 오늘 탭이 활성인데 /api/today 미확정이면 폴백 브리핑을 최신 STATE.items 로 재구성
    if(window.CUR_TAB==='today'&&!_todayLoaded)loadToday(true);
  }
  window.updateTabBadges=updateTabBadges;
  window.__miriRenderToday=function(){ loadToday(true); };   // 항목16-b: valmode 변경 시 오늘 큐레이션 강제 재렌더(frontend index.html:1860 훅)

  function markWatched(arr){
    var ws={}; (typeof WLSTATE!=='undefined'?(WLSTATE.stocks||[]):[]).forEach(function(s){ws[String(s.stock_code)]=1;});
    return (arr||[]).map(function(a){ a.is_watched=!!a.stock_code&&!!ws[String(a.stock_code)]; return a; });
  }

  /* ---------- ① 오늘 (GET /api/today, 폴백=최근 STATE.items) ---------- */
  function loadToday(force){
    if(_todayLoaded&&!force)return;
    var host=document.getElementById('todayBody'); if(!host)return;
    jget(API+'/today').then(function(d){ renderToday(host,d); })
      .catch(function(){ renderToday(host,null); });
  }
  /* 항목26 후속: 날짜 하이픈 통일(frontend fmtYmd 동형). ISO(2026-07-22T..)면 날짜부만, 8자리(YYYYMMDD)→YYYY-MM-DD, 이미 하이픈이면 그대로. */
  function _fmtYmd(s){
    s=String(s==null?'':s).trim(); if(!s)return '';
    if(s.indexOf('T')>0)s=s.split('T')[0];                 // ISO → 날짜부만
    if(typeof fmtYmd==='function')return fmtYmd(s);         // 전역(index.html) 재사용
    var m=s.match(/^(\d{4})(\d{2})(\d{2})$/); return m?(m[1]+'-'+m[2]+'-'+m[3]):s;
  }
  /* 항목53: 밤사이 공시 '더보기' 청크 — 초기 OV_CHUNK 건만 DOM 렌더, 버튼 클릭 시 추가(122건 전량 렌더 방지). */
  var OV_CHUNK=24, _ovItems=[], _ovShown=0;
  function _renderOvChunk(){
    if(!_ovItems.length)return '<div class="empty" style="padding:18px 10px">밤사이 신규 공시가 없습니다.</div>';
    var html=_ovItems.slice(0,_ovShown).map(function(a){return cardHTML(a,'');}).join('');
    var rem=_ovItems.length-_ovShown;
    if(rem>0)html+='<button type="button" class="morebtn" id="ovMore" data-ovmore="1" aria-label="밤사이 공시 더보기">더보기 <span class="num">'+rem+'</span>건</button>';
    return html;
  }
  /* 항목37: 밴드(brief hero-am)는 #todayBrief 로, 섹션(큐레이션+밤사이/폴백목록)은 #todayBody(host) 로 분리 주입.
     최종 화면순서 = #todayBrief(밴드) → 정적 대표값세그 → #todayBody(섹션). #todayBrief 없으면 구캐시 graceful. */
  function renderToday(host,d){
    var brief='';   // → #todayBrief
    var body='';    // → #todayBody(host)
    if(d&&(d.overnight||d.curation)){
      var ov=(d.overnight&&d.overnight.items)||[];
      var ovCnt=(d.overnight&&typeof d.overnight.count==='number')?d.overnight.count:ov.length;
      _overnightCnt=ovCnt;   // 항목46/9: 밤사이 수 확정. 메인 탭 노출 중이면 확인처리(배지 소멸), 아니면 미확인 델타.
      if(window.CUR_TAB==='today')markTodaySeen(); else setBadge('badgeToday',_todayBadgeCnt());
      var cur=(d.curation&&d.curation.items)||[];
      var curStatus=(d.curation&&d.curation.status)||'';
      var dist=d.type_distribution||{};
      var distTop=Object.keys(dist).sort(function(a,b){return dist[b]-dist[a];}).slice(0,3)
        .map(function(k){return esc(k)+' '+dist[k];}).join(' · ');
      // 트랙4: 밴드를 버튼화 → 탭하면 밤사이 공시 섹션(#ovSecH)으로 스크롤. 카운트 로직(항목46)은 불변.
      brief+='<button type="button" class="brief hero-am" data-ov-jump="1" aria-label="밤사이 공시 '+ovCnt+'건 자세히 보기" '+
            'style="display:block;width:100%;text-align:left;border:none;font-family:inherit;cursor:pointer">'+
            '<div class="bl"><b style="white-space:nowrap;flex:none">밤사이 공시 '+ovCnt+'건</b>'+
            (distTop?('<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+distTop+'</span>'):'<span style="flex:1"></span>')+
            '<span class="brief-cta" aria-hidden="true" style="flex:none;color:var(--blue);font-weight:700;font-size:12.5px">자세히 ›</span></div>'+
            '<div class="bl">'+esc(d.market_scope||'코스피·코스닥')+' · 기준 '+esc(_fmtYmd(d.dataset_as_of||d.generated_at||''))+'</div></button>';
      body+='<div class="sec-h"><span class="st">오늘의 큐레이션</span><span class="ss">MIRI 선별</span></div>';
      if(cur.length&&curStatus!=='pending_contract'){
        body+=markWatched(cur).map(function(a){return cardHTML(a,'');}).join('');
      }else{
        body+='<div class="empty" style="padding:22px 10px">큐레이션 준비중 — 곧 오늘의 핵심 공시를 선별해 보여드립니다.</div>';
      }
      body+='<div class="sec-h" id="ovSecH"><span class="st">밤사이 공시</span><span class="ss">'+ov.length+'건</span></div>';
      // 항목53: 밤사이 공시는 청크 렌더(초기 OV_CHUNK, 나머지는 더보기). ovWrap 컨테이너만 재렌더로 확장.
      _ovItems=markWatched(ov); _ovShown=Math.min(OV_CHUNK,_ovItems.length);
      window.__OV_ITEMS=_ovItems;   // 항목5: 밴드 탭 시 상세 오버레이에 담을 밤사이 공시 전량
      body+='<div id="ovWrap">'+_renderOvChunk()+'</div>';
      if(d.disclaimer)body+='<p class="disc">'+esc(d.disclaimer)+'</p>';
      _todayLoaded=true;
    }else{
      // 폴백: /api/today 미가동 → 최근 공시(STATE.items)로 브리핑 대체(빈화면 방지). 밴드성 요약→brief, 카드목록→body
      var recent=(typeof STATE!=='undefined'?(STATE.items||[]):[]).slice(0,12);
      brief+='<div class="brief hero-am"><div class="bl"><b>오늘 브리핑 준비중</b> — 최근 공시로 대체 표시 중</div></div>';
      body+='<div class="sec-h"><span class="st">최근 공시</span><span class="ss">'+recent.length+'건</span></div>';
      body+= recent.length ? recent.map(function(a){return cardHTML(a,'');}).join('')
                           : '<div class="empty" style="padding:30px 10px">표시할 공시가 없습니다. 잠시 후 다시 시도해 주세요.</div>';
      // _todayLoaded 유지 false → 엔드포인트 가동 시 다음 진입/폴에서 수렴
    }
    var briefHost=document.getElementById('todayBrief');
    if(briefHost){
      briefHost.innerHTML=brief; host.innerHTML=body;
      if(typeof attachSegToggle==='function'){ attachSegToggle(briefHost); attachSegToggle(host); } // 두 컨테이너 모두 후처리
    }else{
      host.innerHTML=brief+body;   // 구캐시 graceful: #todayBrief 없으면 #todayBody 한 곳에 합쳐 주입(기존 순서)
      if(typeof attachSegToggle==='function')attachSegToggle(host);
    }
  }

  /* ---------- ② 관심 서브뷰 세그(관심피드 / 알림함) — 단일 #feed 공유 ---------- */
  function applyWatchSeg(){
    var seg=document.getElementById('watchSeg');
    if(seg)seg.querySelectorAll('button').forEach(function(b){
      var on=b.dataset.seg===WATCH_SEG; b.classList.toggle('on',on); b.setAttribute('aria-selected',String(on));
    });
    STATE.filter=(WATCH_SEG==='inbox')?'inbox':'watch';
    renderWatchRows();   // 항목8: 관심종목 상시 행(관심피드에서만 노출)
    // 항목55: 대표값 세그(관심탭 #valmode)는 관심피드에서만 노출. 알림함(inbox)에선 숨김(대표값 무관 뷰).
    var _vm=document.getElementById('valmode');
    var _vw=(_vm&&_vm.closest)?_vm.closest('.valwrap'):null;
    if(_vw)_vw.hidden=(WATCH_SEG==='inbox');
    // 알림함 진입 = 신규 이슈 확인 → seen 처리 후 관심 빨간점 갱신(소멸)
    if(WATCH_SEG==='inbox'&&markSeenWatchedNew())updateTabBadges();
    if(typeof renderFilter==='function')renderFilter();
    function doRender(){
      if(typeof renderFeed==='function')renderFeed();
      if(WATCH_SEG==='inbox'){
        var cnt=(STATE.items||[]).filter(function(a){return passFilter(a,'inbox');}).length;
        if(!cnt){ var fe=document.getElementById('feed');
          if(fe)fe.innerHTML='<div class="empty">아직 새 알림이 없어요.<br>관심종목에 신규 공시가 뜨면 여기에 모여요.</div>'; }  // 45c: frontend renderFeed 문구 통일
      }
    }
    if(STATE.items&&STATE.items.length&&typeof feedSwap==='function')feedSwap(doRender);
    else doRender();
  }

  /* ---------- 항목8: 관심종목 상시 행(최근 공시 유무 무관). 탭 → openAnalyst(컨센서스/종가 그래프) ---------- */
  function renderWatchRows(){
    var host=document.getElementById('watchRows'); if(!host)return;
    var stocks=(typeof WLSTATE!=='undefined'&&WLSTATE.stocks)?WLSTATE.stocks:[];
    if(WATCH_SEG!=='feed'||!stocks.length){ host.hidden=true; host.innerHTML=''; return; }
    host.hidden=false;
    var rows=stocks.map(function(s){
      var code=String(s.stock_code||''), nm=esc(s.name||code);
      var rank=(window.__TOP100_RANK&&window.__TOP100_RANK[code])?window.__TOP100_RANK[code]:0;
      // 시총 순위는 좌측 보조라인으로, 우측 끝은 관심 제외(✕) 버튼 (President 지정 레이아웃)
      return '<div style="display:flex;align-items:stretch">'+
        '<button class="rk-row" type="button" style="flex:1;min-width:0" data-analyst="'+esc(code)+'" data-nm="'+esc(s.name||'')+'">'+
        '<span class="rk-info"><span class="rk-nm">'+nm+'<span class="cd num">'+esc(code)+'</span></span>'+
        '<span class="rk-sub">컨센서스 그래프'+(rank?(' · 시총 '+rank+'위'):'')+'</span></span>'+
        '<span class="rk-go" aria-hidden="true">›</span></button>'+
        '<button type="button" data-wl-del="'+esc(code)+'" aria-label="'+nm+' 관심 제외" title="관심 제외" '+
        'style="flex:none;border:none;background:none;color:var(--t3);font-size:15px;padding:0 14px;cursor:pointer">✕</button></div>';
    }).join('');
    host.innerHTML='<div class="sec-h"><span class="st">관심종목</span><span class="ss">'+stocks.length+'</span></div>'+
      '<div class="rank">'+rows+'</div>';
  }
  window.renderWatchRows=renderWatchRows;

  /* ---------- ③ 랭킹 (GET /api/ranking · cardHTML/행 재사용, price_signal null-graceful) ---------- */
  function loadRanking(){
    // 매 진입마다 재조회: price_signal 콜드(null) 후 워밍값이 다음 진입에 수렴하도록(서버측 캐시라 저렴).
    jget(API+'/ranking').then(function(d){ RANK_DATA=(d&&d.items)||[]; renderRanking(); })
      .catch(function(){ if(!RANK_DATA.length)renderRanking(); });
  }
  // 항목11: 랭킹 실시간 갱신 — 시총 Top100(cap, 정적)만 제외하고 공시순·급등락은 폴링 틱마다 재조회.
  //  index.html 경량 폴링(85s, 화면활성·오버레이닫힘 가드)이 랭킹 탭일 때 호출. 별도 타이머/폴 주기 신설 없음.
  window.__miriRankRefresh=function(){ if(RANK_SEG!=='cap')loadRanking(); };
  function rankValue(it){
    var ps=it.price_signal;   // {change_pct, price, prev_close, volume, source, as_of} | null (null-graceful)
    if(ps&&typeof ps.change_pct==='number')return ps.change_pct;
    return null;
  }
  function rankRowHTML(it,i){
    var rank=i+1;   // 표시 순번 = 화면(정렬 후) 위치. it.rank(원본 disc 순위) 쓰면 정렬 뷰에서 1,5,3… 뒤섞임(결함C)
    var code=String(it.stock_code||'');
    var v=rankValue(it), valHTML='';
    if(v!=null){ var cls=v>0?'up':(v<0?'down':'flat'); valHTML='<span class="rk-val '+cls+' num">'+(v>0?'+':'')+v.toFixed(1)+'%</span>'; }
    var n=(it.impact&&(it.impact.n||it.impact.count))||'';
    var sub=esc(it.report_nm||'')+(n?(' · '+n+'건'):'');
    return '<button class="rk-row" type="button" data-detail="'+esc(code)+'" data-nm="'+esc(it.corp_name||'')+'">'+
      '<span class="rk-no'+(rank<=3?' top':'')+' num">'+rank+'</span>'+
      '<span class="rk-info"><span class="rk-nm">'+esc(it.corp_name||code)+'<span class="cd num">'+esc(code)+'</span></span>'+
      '<span class="rk-sub">'+sub+'</span></span>'+valHTML+'</button>';
  }
  /* 트랙2: 시총 Top100 (GET /api/top100). 행 = 순위+종목명+코드+시총(cap_label). 행 탭 → 애널 그래프(openAnalyst). */
  var _t100Refreshed=false;
  function _buildTop100Rank(){   // 항목7: code→시총순위 맵(피드 카드 배지 조회용). 전역 노출(index.html cardHTML 참조).
    var m={}; TOP100_DATA.forEach(function(it,i){ var c=String(it.stock_code||it.code||''); if(c)m[c]=(it.rank||i+1); });
    window.__TOP100_RANK=m;
  }
  function _refreshFeedsForBadge(){   // 맵 확정 후 현재 탭 목록에 배지 1회 반영(스크롤 중이면 방해 않고 다음 자연 렌더에 위임)
    if((window.pageYOffset||window.scrollY||0)>300)return;
    if(window.CUR_TAB==='today'&&_todayLoaded&&typeof loadToday==='function'){loadToday(true);return;}
    if(window.CUR_TAB==='watch'&&typeof renderFeed==='function'){try{renderFeed();}catch(e){}}
  }
  function loadTop100(){
    jget(API+'/top100').then(function(d){ TOP100_DATA=(d&&d.items)||[]; _top100Loaded=true; _buildTop100Rank();
        if(RANK_SEG==='cap')renderTop100(document.getElementById('rankBody'));
        if(!_t100Refreshed){_t100Refreshed=true;_refreshFeedsForBadge();} })
      .catch(function(){ _top100Loaded=true; if(RANK_SEG==='cap')renderTop100(document.getElementById('rankBody')); });
  }
  function top100RowHTML(it,i){
    var rank=i+1;
    var code=String(it.stock_code||it.code||'');
    var nm=esc(it.corp_name||it.name||code);
    var cap=esc(it.cap_label||'');
    return '<button class="rk-row" type="button" data-analyst="'+esc(code)+'" data-nm="'+esc(it.corp_name||it.name||'')+'">'+
      '<span class="rk-no'+(rank<=3?' top':'')+' num">'+rank+'</span>'+
      '<span class="rk-info"><span class="rk-nm">'+nm+'<span class="cd num">'+esc(code)+'</span></span>'+
      '<span class="rk-sub">시가총액</span></span>'+
      (cap?'<span class="rk-val flat num">'+cap+'</span>':'')+'</button>';
  }
  function renderTop100(host){
    if(!host)return;
    var cap=document.getElementById('rankCap'); if(cap)cap.textContent='시가총액 상위 100 종목';
    if(!_top100Loaded&&!TOP100_DATA.length){ host.innerHTML='<div class="empty" style="padding:30px 10px">시총 상위 종목 불러오는 중…</div>'; loadTop100(); return; }
    if(!TOP100_DATA.length){ host.innerHTML='<div class="empty" style="padding:30px 10px">시총 데이터 준비중입니다.</div>'; return; }
    host.innerHTML='<div class="rank">'+TOP100_DATA.map(top100RowHTML).join('')+'</div>';
  }
  function renderRanking(){
    var host=document.getElementById('rankBody'); if(!host)return;
    if(RANK_SEG==='cap'){ renderTop100(host); return; }   // 트랙2: 시총 세그는 별도 소스/렌더
    var items=RANK_DATA.slice();
    var moveBanner='';
    if(RANK_SEG==='move'){ // 급등락: price_signal 있는 항목 우선 정렬(null-graceful), 값 없으면 원순서
      items.sort(function(a,b){ var av=rankValue(a), bv=rankValue(b);
        if(av==null&&bv==null)return 0; if(av==null)return 1; if(bv==null)return -1; return Math.abs(bv)-Math.abs(av); });
      // 항목23: price_signal 데이터가 하나도 없으면(전부 null=TOSS 미연동) 정렬 불가 → '안 눌림' 오해 제거 배너.
      // 데이터 있으면 배너 없이 정상 재정렬(회귀). disc 세그엔 영향 없음.
      var hasSig=items.some(function(it){return rankValue(it)!=null;});
      if(!hasSig)moveBanner='<div class="empty" style="padding:12px 10px">급등락 데이터 준비 중 · 시세 연동 후 제공 <small>(현재는 정렬 불가, 아래는 공시 순서)</small></div>';
    }
    // 항목39-b: 세그 캡션(rankCap) 갱신 — items 비었을 때도 세팅되게 early-return 전에.
    var cap=document.getElementById('rankCap');
    if(cap)cap.textContent = RANK_SEG==='disc'?'오늘 반응 많은 공시 순' : RANK_SEG==='move'?(hasSig?'전일 대비 등락률 순':'') : '';
    if(!items.length){
      host.innerHTML='<div class="empty" style="padding:30px 10px">랭킹 집계 준비중 — 공시 반응 랭킹이 곧 제공됩니다.</div>';
      return;
    }
    host.innerHTML=moveBanner+'<div class="rank">'+items.map(rankRowHTML).join('')+'</div>';
  }

  /* ---------- ④ 캘린더 (메자닌 lazy-load, #mezzBody 승격) ---------- */
  function loadCalendar(){ if(typeof loadEarn==='function')loadEarn(); else if(typeof loadMezz==='function')loadMezz(); }   // 기본 탭=실적발표(President 지정)

  /* ---------- ⑤ 설정 — no-op (CTO 정정) ----------
     설정 컨트롤 바인딩(initSettings/applyTheme·miri-starttab 저장 등)은 frontend가 index.html에서
     소유(index.html 함수 의존 + #p-settings 마크업 소유). shell.js는 패널 show만 담당(그건 activateTab의
     .tabpanel 토글이 이미 처리) → 여기선 이중바인딩 방지 위해 아무것도 하지 않는다. 분기만 유지. */
  function loadSettings(){ /* no-op: 정적 패널, 바인딩은 frontend(index.html) 소유 */ }

  /* ============ 트랙1: 애널리스트 전망 오버레이 (openAnalyst/closeAnalyst — #detail 뒤로가기 트랩 미러) ============
     데이터=GET /api/analyst?code=. SVG: dots=증권사 목표가, 실선=종가, 점선=평균목표. 프로토 이식 + President 확정:
     ①의견 한글화 ②평균 점선 라벨 좌측 배치(겹침 회피) ③추세선 2종 토글(N>=5시만) ④리스트 기본 접힘(자세히 보기). */
  var _anFmt=function(n){return n==null?'-':Number(n).toLocaleString('ko-KR');};
  var _anWon=function(n){return n==null?'-':(Math.abs(n)>=10000?Math.round(n/10000).toLocaleString('ko-KR')+'만':_anFmt(n));};
  var _anParseD=function(s){return new Date(String(s).slice(0,10)+'T00:00:00');};
  // 의견 한글화: 원문 영어 AND 이미 한글 모두 처리. 색 매핑 c: buy=up(빨강), hold=warn, sell=down(파랑), na=slate
  function _anOpinion(op){
    var s=String(op==null?'':op).trim();
    if(!s||/^(n\/?a|na|없음|의견\s*없음|-)$/i.test(s))return {t:'의견없음',c:'na'};
    if(/strong\s*buy|적극\s*매수|강력\s*매수/i.test(s))return {t:'적극매수',c:'buy'};
    if(/buy|매수|비중\s*확대|outperform|overweight/i.test(s))return {t:'매수',c:'buy'};
    if(/hold|neutral|중립|보유|market\s*perform/i.test(s))return {t:'중립',c:'hold'};
    if(/sell|매도|비중\s*축소|underperform|underweight/i.test(s))return {t:'매도',c:'sell'};
    return {t:s,c:'na'};
  }
  var _anState=null, _analystHistPushed=false;

  function openAnalyst(code,name){
    code=String(code||''); if(!code)return;
    var ov=document.getElementById('analyst'); if(!ov)return;
    var host=document.getElementById('analystBody');
    document.getElementById('anNm').textContent=name||code;
    document.getElementById('anCd').textContent=code;
    if(host)host.innerHTML='<div class="an-empty">애널리스트 전망 불러오는 중…</div>';
    ov.hidden=false; document.body.style.overflow='hidden';
    requestAnimationFrame(function(){requestAnimationFrame(function(){ov.classList.add('open');});});
    if(!_analystHistPushed){try{history.pushState({miriModal:'analyst'},'');_analystHistPushed=true;}catch(e){}}
    if(typeof track==='function')track('analyst_open',{code:code});
    jget(API+'/analyst?code='+encodeURIComponent(code)).then(function(d){
      if(!d||d.cached===false||(!d.prices&&!d.reports)){ _anEmpty(host); return; }
      _anRender(code,name,d);
    }).catch(function(){
      // QA 폴백: API 404/실패 → 프로토 005930 샘플 픽스처(주경로는 API)
      var fx=(typeof ANALYST_FIXTURE!=='undefined')?(ANALYST_FIXTURE[code]||ANALYST_FIXTURE['005930']):null;
      if(fx)_anRender(code,fx.name||name,fx); else _anEmpty(host);
    });
  }
  function _anEmpty(host){ if(host)host.innerHTML='<div class="an-empty">애널리스트 전망 준비중<br>증권사 리포트가 모이면 여기에 표시됩니다.</div>'; }
  function closeAnalyst(fromPop){
    var ov=document.getElementById('analyst'); if(!ov||ov.hidden)return;
    ov.classList.remove('open'); _anState=null;
    var done=function(){ ov.hidden=true;
      var anyS=document.querySelector('.sheet:not([hidden])'), dt=document.getElementById('detail');
      if(!anyS&&(!dt||dt.hidden))document.body.style.overflow=''; };
    if(typeof prefersReduce==='function'&&prefersReduce())done(); else setTimeout(done,300);
    if(_analystHistPushed){_analystHistPushed=false; if(!fromPop){try{history.back();}catch(e){}}}
    var tip=document.getElementById('anTip'); if(tip)tip.style.opacity=0;
  }
  window.openAnalyst=openAnalyst; window.closeAnalyst=closeAnalyst;

  function _anRender(code,name,d){
    document.getElementById('anNm').textContent=d.name||name||code;
    document.getElementById('anCd').textContent=code;
    var _pReg=false,_pCons=false;   // 추세선 토글 상태 영속(종목·세션 간 유지)
    try{_pReg=localStorage.getItem('miri-antrend-reg')==='1';_pCons=localStorage.getItem('miri-antrend-cons')==='1';}catch(e){}
    _anState={code:code,name:d.name||name,data:d,reg:_pReg,cons:_pCons,listOpen:false};
    var host=document.getElementById('analystBody'); if(!host)return;
    var reps=(d.reports||[]).filter(function(r){return r.target_price!=null;});
    var nTp=(typeof d.n_tp==='number')?d.n_tp:reps.length;
    var nTot=(typeof d.n_total==='number')?d.n_total:(d.reports?d.reports.length:0);
    var up=(d.avg_tp!=null&&d.current)?((d.avg_tp-d.current)/d.current*100):null;
    var showToggle=nTp>=5;   // ③ 토글은 리포트 N>=5일 때만 노출
    var noRep=(nTp===0);     // 항목8: 목표가 리포트 없음 → 종가 추이만 표시(리포트 없음 상태 명시)
    host.innerHTML=
      '<div class="an-sub">'+(noRep?'아직 증권사 목표주가 리포트가 없어요 · 종가 추이만 표시'
                                    :('증권사 리포트 '+nTot+'건 중 목표주가 제시 '+nTp+'건'))+'</div>'+
      '<div class="an-metrics">'+
        '<div class="an-metric"><div class="k">현재가</div><div class="v">'+_anFmt(d.current)+'원</div></div>'+
        '<div class="an-metric"><div class="k">평균 목표가</div><div class="v">'+(noRep?'—':(_anFmt(d.avg_tp)+'원'))+'</div></div>'+
        '<div class="an-metric"><div class="k">상승여력</div><div class="v '+(up==null?'':(up>=0?'up':'down'))+'">'+(up==null?'—':((up>=0?'+':'')+up.toFixed(1)+'%'))+'</div></div>'+
      '</div>'+
      (showToggle?('<div class="an-chips">'+
        '<button type="button" class="an-chip'+(_anState.reg?' on':'')+'" data-an-toggle="reg" aria-pressed="'+String(!!_anState.reg)+'">회귀 추세선</button>'+
        '<button type="button" class="an-chip'+(_anState.cons?' on':'')+'" data-an-toggle="cons" aria-pressed="'+String(!!_anState.cons)+'">컨센서스 추세선</button></div>'):'')+
      '<div class="an-card">'+
        '<div class="an-card-t"><span>'+(noRep?'실제 종가 추이':'목표주가 · 실제주가')+'</span><span class="cnt">'+(noRep?'최근 5개월':('리포트 '+nTp+'개 · 최근 5개월'))+'</span></div>'+
        '<div id="anChart"></div>'+
        '<div class="an-legend" id="anLegend"></div>'+
        '<button type="button" class="an-more" id="anMore" aria-expanded="false">자세히 보기 ›</button>'+
      '</div>'+
      '<div class="an-card an-list" id="anList" hidden></div>'+
      '<div class="an-disc">※ 증권사 전망을 정리한 참고 자료이며<br>투자 권유가 아닙니다. 목표주가는 각 증권사 리포트 기준.</div>';
    _anDrawChart(); _anDrawList(); _anFitMetrics();
  }
  /* 항목4: 지표카드 값(숫자+원)이 nowrap 상태에서 박스를 넘치면 폰트를 단계 축소해 한 줄 유지(줄넘김/잘림 방지). */
  function _anFitMetrics(){
    var vs=document.querySelectorAll('#analystBody .an-metric .v');
    for(var i=0;i<vs.length;i++){ var el=vs[i]; el.style.fontSize=''; var fs=17;
      while(el.scrollWidth>el.clientWidth+0.5 && fs>11){ fs-=0.5; el.style.fontSize=fs+'px'; } }
  }
  function _anRegLine(reports){   // ③(a) 직선 회귀선: 목표가 vs 날짜 선형회귀
    var pts=reports.map(function(r){return [_anParseD(r.date).getTime(), r.target_price];}).sort(function(a,b){return a[0]-b[0];});
    if(pts.length<2)return null;
    var base=pts[0][0], n=pts.length, sx=0,sy=0,sxy=0,sxx=0;
    pts.forEach(function(p){var x=(p[0]-base)/86400000, y=p[1]; sx+=x;sy+=y;sxy+=x*y;sxx+=x*x;});
    var den=n*sxx-sx*sx; if(!den)return null;
    var m=(n*sxy-sx*sy)/den, b=(sy-m*sx)/n, xa=pts[0][0], xb=pts[n-1][0];
    return {x0:xa,y0:b+m*(xa-base)/86400000,x1:xb,y1:b+m*(xb-base)/86400000};
  }
  function _anConsLine(reports){  // ③(b) 이동 컨센서스: 각 리포트 시점 기준 최근 3개월 목표가 평균의 시간축 라인
    var pts=reports.map(function(r){return [_anParseD(r.date).getTime(), r.target_price];}).sort(function(a,b){return a[0]-b[0];});
    var out=[], win=90*86400000;
    pts.forEach(function(p){ var lo=p[0]-win, s=0,c=0;
      pts.forEach(function(q){ if(q[0]<=p[0]&&q[0]>=lo){s+=q[1];c++;} }); if(c)out.push([p[0],s/c]); });
    return out;
  }
  function _anDrawChart(){
    var st=_anState; if(!st)return; var d=st.data;
    var el=document.getElementById('anChart'); if(!el)return;
    var prices=d.prices||[], reports=(d.reports||[]).filter(function(r){return r.target_price!=null;});
    if(!prices.length){ el.innerHTML='<div class="an-empty" style="padding:24px 8px">가격 데이터 준비중</div>'; return; }
    var W=390,H=230,PL=52,PR=14,PT=16,PB=26, iw=W-PL-PR, ih=H-PT-PB, i;
    var xs=prices.map(function(p){return _anParseD(p[0]).getTime();});
    var x0=Math.min.apply(null,xs), x1=Math.max.apply(null,xs);
    var vals=prices.map(function(p){return p[1];}).concat(reports.map(function(r){return r.target_price;}));
    if(d.avg_tp!=null)vals.push(d.avg_tp); if(d.current!=null)vals.push(d.current);
    var y0=Math.min.apply(null,vals), y1=Math.max.apply(null,vals);
    var pad=(y1-y0)*0.12||1; y0-=pad; y1+=pad;
    var X=function(t){return PL+(t-x0)/((x1-x0)||1)*iw;};
    var Y=function(v){return PT+(1-(v-y0)/((y1-y0)||1))*ih;};
    var grid='', steps=4;
    for(i=0;i<=steps;i++){ var gv=y0+(y1-y0)*i/steps, gy=Y(gv);
      grid+='<line x1="'+PL+'" y1="'+gy.toFixed(1)+'" x2="'+(W-PR)+'" y2="'+gy.toFixed(1)+'" stroke="var(--line)" stroke-width="1"/>';
      grid+='<text x="'+(PL-8)+'" y="'+(gy+4).toFixed(1)+'" fill="var(--t3)" font-size="11" text-anchor="end">'+_anWon(Math.round(gv))+'</text>'; }
    var xlab='', seen={};
    prices.forEach(function(p){ var dt=_anParseD(p[0]), key=dt.getMonth();
      if(!seen[key]&&dt.getDate()<=6){ seen[key]=1; var x=X(dt.getTime());
        xlab+='<text x="'+x.toFixed(1)+'" y="'+(H-8)+'" fill="var(--t3)" font-size="11" text-anchor="middle">'+(dt.getMonth()+1)+'월</text>'; } });
    var path=prices.map(function(p,ix){return (ix?'L':'M')+X(_anParseD(p[0]).getTime()).toFixed(1)+' '+Y(p[1]).toFixed(1);}).join(' ');
    // ② 평균 목표 점선 — 라벨을 좌측(text-anchor start, x=PL+4)에 배치해 우측 '현재…' 라벨/그리드와 겹침 회피
    var avg='';
    if(d.avg_tp!=null){ var ay=Y(d.avg_tp);
      avg='<line x1="'+PL+'" y1="'+ay.toFixed(1)+'" x2="'+(W-PR)+'" y2="'+ay.toFixed(1)+'" stroke="#ff5b64" stroke-width="1.5" stroke-dasharray="5 4" opacity="0.85"/>'+
        '<text x="'+(PL+4)+'" y="'+(ay-6).toFixed(1)+'" fill="#ff5b64" font-size="11" text-anchor="start" font-weight="700">평균 '+_anWon(d.avg_tp)+'</text>'; }
    var trend='';
    if(st.reg){ var rl=_anRegLine(reports); if(rl)trend+='<line x1="'+X(rl.x0).toFixed(1)+'" y1="'+Y(rl.y0).toFixed(1)+'" x2="'+X(rl.x1).toFixed(1)+'" y2="'+Y(rl.y1).toFixed(1)+'" stroke="#2dd48a" stroke-width="2" opacity="0.9"/>'; }
    if(st.cons){ var cl=_anConsLine(reports); if(cl.length>1)trend+='<path d="'+cl.map(function(p,ix){return (ix?'L':'M')+X(p[0]).toFixed(1)+' '+Y(p[1]).toFixed(1);}).join(' ')+'" fill="none" stroke="#4d94ff" stroke-width="2" stroke-dasharray="2 3" opacity="0.9"/>'; }
    var dots='';
    reports.forEach(function(r,ix){ var x=X(_anParseD(r.date).getTime()), y=Y(r.target_price);
      dots+='<circle cx="'+x.toFixed(1)+'" cy="'+y.toFixed(1)+'" r="3.6" fill="#fbbf24" fill-opacity="0.95" stroke="var(--card)" stroke-width="1.1" data-i="'+ix+'" class="an-pt" style="cursor:pointer"/>'; });
    var lastx=X(x1), lasty=Y(d.current);
    var curm=(d.current!=null)?('<circle cx="'+lastx.toFixed(1)+'" cy="'+lasty.toFixed(1)+'" r="3.5" fill="var(--t1)"/>'+
      '<text x="'+(lastx-6).toFixed(1)+'" y="'+(lasty+4).toFixed(1)+'" fill="var(--t1)" font-size="11" text-anchor="end" font-weight="700">현재 '+_anWon(d.current)+'</text>'):'';
    el.innerHTML='<svg viewBox="0 0 '+W+' '+H+'">'+grid+xlab+avg+trend+
      '<path d="'+path+'" fill="none" stroke="var(--t1)" stroke-width="2" stroke-linejoin="round" opacity="0.95"/>'+curm+dots+'</svg>';
    var leg=(reports.length?'<span><i style="width:9px;height:9px;border-radius:50%;background:#fbbf24"></i>증권사 목표가</span>':'')+
      '<span><i style="width:16px;height:0;border-top:2px solid var(--t1)"></i>실제 종가</span>'+
      (d.avg_tp!=null?'<span><i style="width:16px;height:0;border-top:2px dashed var(--up)"></i>평균 목표가</span>':'');
    if(st.reg)leg+='<span><i style="width:16px;height:0;border-top:2px solid #2dd48a"></i>회귀 추세선</span>';
    if(st.cons)leg+='<span><i style="width:16px;height:0;border-top:2px dashed #4d94ff"></i>컨센서스 추세선</span>';
    var lg=document.getElementById('anLegend'); if(lg)lg.innerHTML=leg;
    _anBindDots(reports);
  }
  function _anBindDots(reports){
    var tip=document.getElementById('anTip'); if(!tip)return;
    var hide=function(){tip.style.opacity=0;};
    document.querySelectorAll('#anChart .an-pt').forEach(function(elp){
      var show=function(ev){ if(ev)ev.stopPropagation(); var r=reports[+elp.dataset.i]; if(!r)return;
        var op=_anOpinion(r.opinion);
        tip.innerHTML='<div class="tb">'+esc(r.broker||'')+'</div><div class="tp">'+_anFmt(r.target_price)+'원</div>'+
          '<div class="to">'+esc(op.t)+' · '+esc(r.date||'')+'</div>';
        var b=elp.getBoundingClientRect();
        tip.style.left=Math.max(8,Math.min(window.innerWidth-140,b.left+8))+'px';
        tip.style.top=Math.max(8,b.top-70)+'px'; tip.style.opacity=1; };
      elp.addEventListener('mouseenter',show); elp.addEventListener('click',show); elp.addEventListener('mouseleave',hide);
    });
  }
  function _anDrawList(){
    var st=_anState; if(!st)return; var listEl=document.getElementById('anList'); if(!listEl)return;
    var rows=(st.data.reports||[]).slice().sort(function(a,b){return (String(b.date||''))<(String(a.date||''))?-1:1;});
    listEl.innerHTML=rows.map(function(r){ var op=_anOpinion(r.opinion);
      return '<div class="an-row"><span class="dt">'+esc(String(r.date||'').slice(2))+'</span>'+
        '<span class="br">'+esc(r.broker||'')+'</span>'+
        '<span class="an-op '+op.c+'">'+esc(op.t)+'</span>'+
        '<span class="an-tpv">'+_anWon(r.target_price)+'</span></div>'; }).join('');
  }

  /* ---------- 탭 전환 ---------- */
  var _scrollY={};   // 항목34: 탭별 마지막 스크롤 위치(복귀 복원용)
  function activateTab(name){
    if(!TAB_SET[name])name='today';
    var _prevTab=window.CUR_TAB;
    // 항목34: 탭을 '떠날 때' 현재 스크롤 저장(탭이 실제로 바뀔 때만). 같은 탭 재클릭은 저장 안 함(top 유도).
    if(_prevTab&&_prevTab!==name)_scrollY[_prevTab]=(window.pageYOffset||window.scrollY||0);
    // 공존 규칙: 탭 전환 시 열린 상세 모달/시트 먼저 닫기(body 스크롤락·history 꼬임 방지)
    var dv=document.getElementById('detail');
    if(dv&&!dv.hidden&&typeof closeDetail==='function')closeDetail(true);
    var av=document.getElementById('analyst');   // 트랙1: 탭 전환 시 열린 애널리스트 오버레이도 닫기
    if(av&&!av.hidden)closeAnalyst(true);
    document.querySelectorAll('.sheet:not([hidden])').forEach(function(s){ if(typeof closeSheet==='function')closeSheet(s.id); });
    // 관심 탭을 떠나면 피드 옵저버 해제(숨은 탭 sentinel 오관측 방지)
    if(window.CUR_TAB==='watch'&&name!=='watch'&&typeof FEED_OBSERVER!=='undefined'&&FEED_OBSERVER){
      try{FEED_OBSERVER.disconnect();}catch(e){} FEED_OBSERVER=null;
    }
    window.CUR_TAB=name;
    document.querySelectorAll('.tabpanel').forEach(function(p){ p.classList.toggle('on',p.id==='p-'+name); });
    document.querySelectorAll('.tabitem').forEach(function(b){
      var on=b.dataset.tab===name; b.classList.toggle('on',on); b.setAttribute('aria-selected',String(on));
    });
    updateTopbar(name);
    // 항목34: 탭전환 복귀=저장 위치 복원(패널 내용은 토글이라 대개 잔존 → 동기 복원 가능). 같은탭 재클릭/최초 진입=top.
    var _ry=(_prevTab!==name&&typeof _scrollY[name]==='number')?_scrollY[name]:0;
    window.scrollTo(0,_ry);
    if(_ry)try{requestAnimationFrame(function(){window.scrollTo(0,_ry);});}catch(e){} // 레이아웃 확정 후 한번 더(async 렌더 보정)
    if(name==='today'){ loadToday(false); markTodaySeen(); }   // 항목9: 메인 탭 진입 = 밤사이 확인 → 배지 소멸
    else if(name==='watch')applyWatchSeg();
    else if(name==='ranking')loadRanking();
    else if(name==='calendar')loadCalendar();
    else if(name==='settings')loadSettings();
  }
  window.__miriActivateTab=activateTab;

  var _coldLaunch=true;   // 항목32: 첫 applyRoute(=콜드런치) 판정용
  function _isStandalone(){
    try{ return (window.matchMedia&&window.matchMedia('(display-mode: standalone)').matches)
      ||(window.navigator&&window.navigator.standalone)||false; }catch(e){ return false; }
  }
  function applyRoute(){
    var raw=(location.hash||'').replace(/^#/,'');
    // 항목32: TWA/PWA 콜드런치 첫 진입에서 해시가 없거나 기본값(today)이면 저장 시작탭 우선 적용.
    //   (잔여 #today 로 시작탭이 밀리던 문제). 명시적 비루트 딥링크(#watch 등)는 아래 raw 분기가 존중.
    //   standalone 게이트 → 브라우저에서 #today 북마크 직접연 경우는 today 그대로.
    var coldDefault=_coldLaunch&&_isStandalone()&&(!raw||raw==='today');
    _coldLaunch=false;
    if(raw&&!coldDefault){ activateTab(TAB_SET[raw]?raw:'today'); return; }  // 명시 해시(딥링크) 우선
    // 빈 해시 or 콜드런치 기본해시 → 설정 시작탭(miri-starttab, frontend가 저장) 존중. 유효 TAB만, 아니면 today.
    var start=''; try{start=localStorage.getItem('miri-starttab')||'';}catch(e){}
    activateTab(TAB_SET[start]?start:'today');
  }

  /* ---------- 바인딩 ---------- */
  document.querySelectorAll('.tabitem').forEach(function(b){
    b.addEventListener('click',function(){
      var t=b.dataset.tab;
      if(('#'+t)===location.hash)activateTab(t);   // 같은 해시 재클릭 → hashchange 안 뜨므로 직접
      else location.hash=t;
      if(typeof track==='function')track('tab_switch',{tab:t});
    });
  });
  var wseg=document.getElementById('watchSeg');
  if(wseg)wseg.addEventListener('click',function(e){
    var b=e.target.closest('button[data-seg]'); if(!b)return;
    WATCH_SEG=b.dataset.seg; applyWatchSeg();
  });
  var rseg=document.getElementById('rankSeg');
  if(rseg)rseg.addEventListener('click',function(e){
    var b=e.target.closest('button[data-rseg]'); if(!b)return;
    // 항목39-a: 준비중 세그(aria-disabled)는 비활성 무반응 대신 토스트로 상태 안내
    if(b.getAttribute('aria-disabled')==='true'){ if(typeof showToast==='function')showToast('화제 랭킹은 준비 중이에요'); return; }
    RANK_SEG=b.dataset.rseg;
    rseg.querySelectorAll('button').forEach(function(x){var on=x.dataset.rseg===RANK_SEG;x.classList.toggle('on',on);x.setAttribute('aria-selected',String(on));});
    renderRanking();
  });

  // 상세 오픈 = 해당 종목 신규 이슈 확인 → seen 처리(관심 빨간점 소멸). openDetail(index.html) 무접촉,
  // capture 리스너로 병렬 관측만(중복닫힘/충돌 없음). data-detail = stock_code.
  document.addEventListener('click',function(e){
    var t=e.target.closest('[data-detail]'); if(!t)return;
    if(markSeenWatchedNew(t.dataset.detail))updateTabBadges();
  },true);
  // 항목53: 밤사이 공시 '더보기' → 다음 청크만큼 확장, ovWrap 컨테이너만 재렌더(재fetch 없음).
  document.addEventListener('click',function(e){
    if(!e.target.closest('[data-ovmore]'))return;
    _ovShown=Math.min(_ovShown+OV_CHUNK,_ovItems.length);
    var w=document.getElementById('ovWrap');
    if(w){ w.innerHTML=_renderOvChunk(); if(typeof attachSegToggle==='function')attachSegToggle(w); }
  });

  // 항목5: 밤사이 밴드(data-ov-jump) 탭 → 검색결과 클릭과 동일한 상세 오버레이/시트에 밤사이 공시만 담아 표시.
  //  (구: 섹션 스크롤 이동 → 폐기). openDetailList 미로드(구캐시)면 섹션 스크롤로 graceful 폴백.
  document.addEventListener('click',function(e){
    if(!e.target.closest('[data-ov-jump]'))return;
    var items=(window.__OV_ITEMS||[]);
    if(typeof openDetailList==='function'){ openDetailList('밤사이 공시', items); return; }
    var sec=document.getElementById('ovSecH'); if(!sec)return;
    try{ sec.scrollIntoView({behavior:(typeof prefersReduce==='function'&&prefersReduce())?'auto':'smooth',block:'start'}); }
    catch(_){ sec.scrollIntoView(); }
  });

  // 트랙2→트랙1: 시총 Top100 행(data-analyst) 탭 → 애널리스트 그래프 화면. (data-detail 아님 → 상세 모달과 충돌 없음)
  document.addEventListener('click',function(e){
    var del=e.target.closest('[data-wl-del]');   // 관심 행 ✕ → 관심종목 제외(전역 delStock 재사용)
    if(del){ if(typeof delStock==='function')delStock(del.getAttribute('data-wl-del')); return; }
    var t=e.target.closest('[data-analyst]'); if(!t)return;
    openAnalyst(t.dataset.analyst,t.dataset.nm||'');
  });

  // 트랙1: 애널리스트 오버레이 바인딩(닫기·추세선 토글·자세히 보기·Escape·자체 popstate 트랩)
  var _anEl=document.getElementById('analyst');
  if(_anEl){
    var _anX=document.getElementById('analystX'); if(_anX)_anX.addEventListener('click',function(){closeAnalyst();});
    var _anB=document.getElementById('analystBack'); if(_anB)_anB.addEventListener('click',function(){closeAnalyst();});
    var _anBody=document.getElementById('analystBody');
    if(_anBody)_anBody.addEventListener('click',function(e){
      var tg=e.target.closest('[data-an-toggle]');
      if(tg&&_anState){ var k=tg.getAttribute('data-an-toggle'); _anState[k]=!_anState[k];
        try{localStorage.setItem('miri-antrend-'+k,_anState[k]?'1':'0');}catch(err){}   // 상태 영속(다른 종목에도 유지)
        tg.classList.toggle('on',_anState[k]); tg.setAttribute('aria-pressed',String(_anState[k])); _anDrawChart(); return; }
      var mb=e.target.closest('#anMore');
      if(mb&&_anState){ _anState.listOpen=!_anState.listOpen;
        var lst=document.getElementById('anList'); if(lst)lst.hidden=!_anState.listOpen;
        mb.setAttribute('aria-expanded',String(_anState.listOpen));
        mb.textContent=_anState.listOpen?'접기 ‹':'자세히 보기 ›'; return; }
    });
    document.addEventListener('keydown',function(e){ if(e.key==='Escape'){var a=document.getElementById('analyst'); if(a&&!a.hidden)closeAnalyst();} });
    window.addEventListener('popstate',function(){   // 뒤로가기=오버레이만 닫기(앱 종료 아님). detail popstate와 독립(서로 hidden 가드)
      var a=document.getElementById('analyst'); if(a&&!a.hidden){_analystHistPushed=false;closeAnalyst(true);}
    });
  }

  /* ── 시트 뒤로가기 트랩(항목7·CTO정정3): openSheet(index.html)는 history를 안 쌓아
        back이 밑의 탭 엔트리를 소비(탭이동 유발)한다. 시트가 '열릴 때' 동일-해시 가드
        엔트리를 대신 push해 → back=시트만 닫힘(탭 유지). detail/mezz는 자체 트랩 보유 → 무접촉.
        __miriP 플래그로 팝-경유(닫힘 이미 소비) vs 수동닫힘(X/백드롭/Esc, 엔트리 되돌림) 구분. ── */
  try{
    var _mo=new MutationObserver(function(muts){
      for(var i=0;i<muts.length;i++){
        var t=muts[i].target;
        if(!t||t.nodeType!==1||!t.classList||!t.classList.contains('sheet'))continue;
        var open=!t.hasAttribute('hidden');
        if(open&&!t.__miriP){                    // 시트 open 감지 → 대응 엔트리 1개 push(해시 불변=탭유지)
          t.__miriP=true; try{history.pushState({miriSheet:1},'');}catch(_){}
        }else if(!open&&t.__miriP){               // 시트 close 감지
          t.__miriP=false;
          if(!t.__miriPop){ try{history.back();}catch(_){} }  // 수동닫힘 → 쌓아둔 엔트리 되돌림(detail 패턴)
          t.__miriPop=false;
        }
      }
    });
    _mo.observe(document.body,{attributes:true,attributeFilter:['hidden'],subtree:true});
  }catch(_){}

  /* ================= 안드로이드/TWA 하드웨어 뒤로가기 (항목7 · P0 재설계) =================
     [불변] 종료 아밍/토스트는 '오직' 뒤로가기가 앱 최하단(부트) 경계를 소비할 때만.
     탭 전진클릭·탭↔탭 back 은 절대 arm 안 함 — 이를 '기전 무관'하게 보장하려고
     '전용 종료센티넬(부트 엔트리)'에 착지했을 때만 arm 한다(기법B). 부트 엔트리는 boot 시
     replaceState 로 {miriBoot} 태깅 → 이 상태는 '뒤로가기로 최하단에 도달'해야만 popstate
     e.state 로 관측된다. 탭 내비가 만드는 엔트리는 null 또는 {miriRoot}/{miriSheet} 이라
     e.state.miriBoot 로는 절대 안 옴 → 전진클릭이 유발하는 popstate(하네스 특성)도 오발화 0.
     핸들러 순서: (a)#detail/#mezz=자체 리스너 → 무개입. (b)시트=자체리스너無 → 최상단 직접 닫기.
     (c)부트경계(miriBoot)+루트 착지 시에만 double-back-to-exit. 그 외 popstate=무개입(arm 절대 없음). */
  function _rootTab(){ var r=(location.hash||'').replace(/^#/,''); return TAB_SET[r]?r:'today'; }
  var _exitArmed=false,_exitT=null;
  window.addEventListener('popstate',function(e){
    // (a) 상세/메자닌 = 자체 리스너 보유 → 이중닫힘 금지, 개입 안 함
    var dv=document.getElementById('detail'); if(dv&&!dv.hidden)return;
    var mz=document.getElementById('mezz');   if(mz&&!mz.hidden)return;
    var an=document.getElementById('analyst');if(an&&!an.hidden)return;   // 트랙1: 애널리스트=자체 popstate 트랩 → 개입 안 함
    // (b) 열린 시트(자체 리스너 없음) → 최상단만 직접 닫고 종료. __miriPop=true 로 옵저버 close-branch의 back 억제
    var sheets=document.querySelectorAll('.sheet:not([hidden])');
    if(sheets.length){
      var top=sheets[sheets.length-1];
      top.__miriPop=true;                       // 이 닫힘은 back이 이미 엔트리 소비함 → 옵저버는 history.back 하지 말 것
      if(typeof closeSheet==='function')closeSheet(top.id); else top.hidden=true;
      return;
    }
    // (c) 종료 경계: 부트 센티넬(miriBoot)에 루트에서 착지했을 때만. 탭 전진/back 엔트리(null·miriRoot)는 여기 못 옴 → 오발화 0
    if(!(e&&e.state&&e.state.miriBoot)||_rootTab()!=='today')return;
    if(_exitArmed){                             // 2차 back = 실제 종료
      _exitArmed=false; if(_exitT){clearTimeout(_exitT);_exitT=null;}
      try{history.back();}catch(_){}             // TWA: 부트 엔트리 이탈 → 액티비티 종료
      return;
    }
    _exitArmed=true;                            // 1차 back = 종료 아밍
    try{history.pushState({miriRoot:true},'');}catch(_){}   // 가드 재-push(첫 back 소비, pos=가드 복귀). state≠miriBoot 라 재-arm 안 됨
    if(typeof showToast==='function')showToast('한 번 더 누르면 종료');
    _exitT=setTimeout(function(){_exitArmed=false;_exitT=null;},2000);
  });

  // 부트(최하단) 엔트리를 종료 센티넬로 태깅(replaceState). shell.js 는 index.html 인라인 이후 로드되므로
  // (initDeviceId 의 replaceState 포함) 마지막 태깅이 유효.
  try{history.replaceState({miriBoot:true},'');}catch(_){}
  window.addEventListener('hashchange',applyRoute);
  applyRoute();               // 초기 해시 반영(빈 해시 → 시작탭 miri-starttab, 딥링크 #watch 등 존중)
  // 루트(today) 진입 시에만 작업 가드 1개 push(첫 back이 즉시 부트경계로 안 떨어지게 완충). 딥링크 비루트는 미설치.
  if(_rootTab()==='today'){ try{history.pushState({miriRoot:true},'');}catch(_){} }
  updateTabBadges();          // 부팅 시 이미 absorb 됐을 수 있음(멱등)
  loadTop100();               // 항목7: 시총 배지용 랭크맵을 부트에 선로딩(랭킹 탭 진입 전에도 피드 배지 표시)
})();
