/* 미리(MIRI) 5탭 셸 라우터 — venture-frontend.
   classic script(모듈 아님): index.html 인라인 스크립트와 같은 전역 렉시컬 스코프를 공유한다.
   → STATE, FEED_OBSERVER, renderFeed, renderFilter, feedSwap, passFilter, cardHTML,
     attachSegToggle, loadMezz, closeDetail, closeSheet, jget, esc, valPick, WLSTATE 등을
     '맨이름'으로 직접 참조/대입한다(window.X 아님 — let/const 전역은 window 속성이 아니므로).
   해시 라우터는 '탭 해시(#today/#watch/#ranking/#calendar/#search)'만 소유한다.
   종목상세(#detail)는 전역 모달로 자기 pushState 뒤로가기 트랩을 그대로 유지(무접촉). */
(function(){
  'use strict';
  var TABS=['today','watch','ranking','calendar','search'];
  var TAB_SET={}; TABS.forEach(function(t){TAB_SET[t]=true;});
  var TAB_TITLES={today:null,watch:'관심종목',ranking:'랭킹',calendar:'캘린더',search:'검색'};
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
  /* 오늘 배지=밤사이 신규(is_new) · 관심 배지=관심종목 신규(is_new&&is_watched). 신규 API 불요. */
  function updateTabBadges(){
    var items=(typeof STATE!=='undefined'&&STATE.items)?STATE.items:[];
    var newCnt=items.filter(function(a){return a.is_new;}).length;
    var inboxCnt=items.filter(function(a){return a.is_new&&a.is_watched;}).length;
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
    var rank=it.rank||(i+1);
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
  }
  window.__miriActivateTab=activateTab;

  function applyRoute(){
    var raw=(location.hash||'').replace(/^#/,'');
    activateTab(TAB_SET[raw]?raw:'today');
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

  window.addEventListener('hashchange',applyRoute);
  applyRoute();               // 초기 해시 반영(빈 해시 → today, 딥링크 #watch 등 존중)
  updateTabBadges();          // 부팅 시 이미 absorb 됐을 수 있음(멱등)
})();
