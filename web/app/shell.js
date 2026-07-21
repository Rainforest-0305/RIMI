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
  var RANK_SEG='disc', RANK_DATA=[];

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
  // 관심 신규(미확인)를 seen 처리. codeFilter 주면 해당 종목만(상세 오픈), 없으면 전부(알림함 진입).
  function markSeenWatchedNew(codeFilter){
    var items=(typeof STATE!=='undefined'&&STATE.items)?STATE.items:[];
    var set=_seenSet(), changed=false;
    items.forEach(function(a){
      if(codeFilter!=null&&String(a.stock_code)!==String(codeFilter))return;
      if(a.is_new&&a.is_watched&&a.rcept_no&&!_seenHas(set,a.rcept_no)){ set.push(String(a.rcept_no)); changed=true; }
    });
    if(changed)_seenSave(set);
    return changed;
  }
  /* 오늘 배지=밤사이 신규(is_new) · 관심 배지=관심 신규 중 '미확인'만(is_new&&is_watched&&!seen). 신규 API 불요. */
  function updateTabBadges(){
    var items=(typeof STATE!=='undefined'&&STATE.items)?STATE.items:[];
    var seen=_seenSet();
    var newCnt=items.filter(function(a){return a.is_new;}).length;
    var inboxCnt=items.filter(function(a){return a.is_new&&a.is_watched&&!_seenHas(seen,a.rcept_no);}).length;
    setBadge('badgeToday',newCnt);
    setBadge('badgeWatch',inboxCnt);
    // 오늘 탭이 활성인데 /api/today 미확정이면 폴백 브리핑을 최신 STATE.items 로 재구성
    if(window.CUR_TAB==='today'&&!_todayLoaded)loadToday(true);
  }
  window.updateTabBadges=updateTabBadges;

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
  function renderToday(host,d){
    var html='';
    if(d&&(d.overnight||d.curation)){
      var ov=(d.overnight&&d.overnight.items)||[];
      var ovCnt=(d.overnight&&typeof d.overnight.count==='number')?d.overnight.count:ov.length;
      var cur=(d.curation&&d.curation.items)||[];
      var curStatus=(d.curation&&d.curation.status)||'';
      var dist=d.type_distribution||{};
      var distTop=Object.keys(dist).sort(function(a,b){return dist[b]-dist[a];}).slice(0,3)
        .map(function(k){return esc(k)+' '+dist[k];}).join(' · ');
      html+='<div class="brief hero-am"><div class="bl"><b>밤사이 공시 '+ovCnt+'건</b>'+(distTop?(' — '+distTop):'')+'</div>'+
            '<div class="bl">'+esc(d.market_scope||'코스피·코스닥')+' · 기준 '+esc(d.dataset_as_of||d.generated_at||'')+'</div></div>';
      html+='<div class="sec-h"><span class="st">오늘의 큐레이션</span><span class="ss">MIRI 선별</span></div>';
      if(cur.length&&curStatus!=='pending_contract'){
        html+=markWatched(cur).map(function(a){return cardHTML(a,'');}).join('');
      }else{
        html+='<div class="empty" style="padding:22px 10px">큐레이션 준비중 — 곧 오늘의 핵심 공시를 선별해 보여드립니다.</div>';
      }
      html+='<div class="sec-h"><span class="st">밤사이 공시</span><span class="ss">'+ov.length+'건</span></div>';
      html+= ov.length ? markWatched(ov).map(function(a){return cardHTML(a,'');}).join('')
                       : '<div class="empty" style="padding:18px 10px">밤사이 신규 공시가 없습니다.</div>';
      if(d.disclaimer)html+='<p class="disc">'+esc(d.disclaimer)+'</p>';
      _todayLoaded=true;
    }else{
      // 폴백: /api/today 미가동 → 최근 공시(STATE.items)로 브리핑 대체(빈화면 방지)
      var recent=(typeof STATE!=='undefined'?(STATE.items||[]):[]).slice(0,12);
      html+='<div class="brief hero-am"><div class="bl"><b>오늘 브리핑 준비중</b> — 최근 공시로 대체 표시 중</div></div>';
      html+='<div class="sec-h"><span class="st">최근 공시</span><span class="ss">'+recent.length+'건</span></div>';
      html+= recent.length ? recent.map(function(a){return cardHTML(a,'');}).join('')
                           : '<div class="empty" style="padding:30px 10px">표시할 공시가 없습니다. 잠시 후 다시 시도해 주세요.</div>';
      // _todayLoaded 유지 false → 엔드포인트 가동 시 다음 진입/폴에서 수렴
    }
    host.innerHTML=html;
    if(typeof attachSegToggle==='function')attachSegToggle(host);
  }

  /* ---------- ② 관심 서브뷰 세그(관심피드 / 알림함) — 단일 #feed 공유 ---------- */
  function applyWatchSeg(){
    var seg=document.getElementById('watchSeg');
    if(seg)seg.querySelectorAll('button').forEach(function(b){
      var on=b.dataset.seg===WATCH_SEG; b.classList.toggle('on',on); b.setAttribute('aria-selected',String(on));
    });
    STATE.filter=(WATCH_SEG==='inbox')?'inbox':'watch';
    // 알림함 진입 = 신규 이슈 확인 → seen 처리 후 관심 빨간점 갱신(소멸)
    if(WATCH_SEG==='inbox'&&markSeenWatchedNew())updateTabBadges();
    if(typeof renderFilter==='function')renderFilter();
    function doRender(){
      if(typeof renderFeed==='function')renderFeed();
      if(WATCH_SEG==='inbox'){
        var cnt=(STATE.items||[]).filter(function(a){return passFilter(a,'inbox');}).length;
        if(!cnt){ var fe=document.getElementById('feed');
          if(fe)fe.innerHTML='<div class="empty">새 알림이 없어요 · 관심종목에 신규 공시가 뜨면 여기 모입니다.</div>'; }
      }
    }
    if(STATE.items&&STATE.items.length&&typeof feedSwap==='function')feedSwap(doRender);
    else doRender();
  }

  /* ---------- ③ 랭킹 (GET /api/ranking · cardHTML/행 재사용, price_signal null-graceful) ---------- */
  function loadRanking(){
    // 매 진입마다 재조회: price_signal 콜드(null) 후 워밍값이 다음 진입에 수렴하도록(서버측 캐시라 저렴).
    jget(API+'/ranking').then(function(d){ RANK_DATA=(d&&d.items)||[]; renderRanking(); })
      .catch(function(){ if(!RANK_DATA.length)renderRanking(); });
  }
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
  function renderRanking(){
    var host=document.getElementById('rankBody'); if(!host)return;
    var items=RANK_DATA.slice();
    if(RANK_SEG==='move'){ // 급등락: price_signal 있는 항목 우선 정렬(null-graceful), 값 없으면 원순서
      items.sort(function(a,b){ var av=rankValue(a), bv=rankValue(b);
        if(av==null&&bv==null)return 0; if(av==null)return 1; if(bv==null)return -1; return Math.abs(bv)-Math.abs(av); });
    }
    if(!items.length){
      host.innerHTML='<div class="empty" style="padding:30px 10px">랭킹 집계 준비중 — 공시 반응 랭킹이 곧 제공됩니다.</div>';
      return;
    }
    host.innerHTML='<div class="rank">'+items.map(rankRowHTML).join('')+'</div>';
  }

  /* ---------- ④ 캘린더 (메자닌 lazy-load, #mezzBody 승격) ---------- */
  function loadCalendar(){ if(typeof loadMezz==='function')loadMezz(); }

  /* ---------- ⑤ 설정 — no-op (CTO 정정) ----------
     설정 컨트롤 바인딩(initSettings/applyTheme·miri-starttab 저장 등)은 frontend가 index.html에서
     소유(index.html 함수 의존 + #p-settings 마크업 소유). shell.js는 패널 show만 담당(그건 activateTab의
     .tabpanel 토글이 이미 처리) → 여기선 이중바인딩 방지 위해 아무것도 하지 않는다. 분기만 유지. */
  function loadSettings(){ /* no-op: 정적 패널, 바인딩은 frontend(index.html) 소유 */ }

  /* ---------- 탭 전환 ---------- */
  function activateTab(name){
    if(!TAB_SET[name])name='today';
    // 공존 규칙: 탭 전환 시 열린 상세 모달/시트 먼저 닫기(body 스크롤락·history 꼬임 방지)
    var dv=document.getElementById('detail');
    if(dv&&!dv.hidden&&typeof closeDetail==='function')closeDetail(true);
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
    window.scrollTo(0,0);
    if(name==='today')loadToday(false);
    else if(name==='watch')applyWatchSeg();
    else if(name==='ranking')loadRanking();
    else if(name==='calendar')loadCalendar();
    else if(name==='settings')loadSettings();
  }
  window.__miriActivateTab=activateTab;

  function applyRoute(){
    var raw=(location.hash||'').replace(/^#/,'');
    if(raw){ activateTab(TAB_SET[raw]?raw:'today'); return; }  // 명시 해시(딥링크) 우선
    // 빈 해시 = 앱 시작 → 설정 시작탭(miri-starttab, frontend가 저장) 존중. 유효 TAB만, 아니면 today.
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
    var b=e.target.closest('button[data-rseg]'); if(!b||b.disabled)return;
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
})();
