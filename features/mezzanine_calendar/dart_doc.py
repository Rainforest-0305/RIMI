# -*- coding: utf-8 -*-
"""WS-34 보강용 DART document.xml 원문 페처 + 로컬 캐시 (격리 모듈).

리픽싱(refixing.py)·전환청구(conversion.py) 가 공유하는 저수준 유틸.
- OpenDART document.xml (무료키) 만 호출. KIS API 미사용.
- rcept_no -> ZIP(PK) -> 단일 XML(HTML 테이블) 텍스트로 디코드해 반환.
- 받은 원문 HTML 은 bench_cache/<kind>/doc/<rcept_no>.html 로 캐시 → 재호출 0.
  (bench_cache 는 .gitignore 대상. 배포는 산출 스냅샷만 읽으므로 무관.)
- 실패는 조용히 None (백필 루프가 실패카운트로 집계).

주의: 이 모듈은 features/ 밖 코드를 수정하지 않는다(config 만 읽기).
"""
import io
import os
import time
import zipfile

import requests

try:
    import config  # 앱/스크립트가 repo 루트를 sys.path 에 두는 전제(dart_poll 과 동일)
except ImportError:  # pragma: no cover
    import sys
    sys.path.insert(0, os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)))
    import config

DOC_URL = "https://opendart.fss.or.kr/api/document.xml"

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))


def _cache_dir(kind: str) -> str:
    d = os.path.join(_REPO_ROOT, "bench_cache", kind, "doc")
    os.makedirs(d, exist_ok=True)
    return d


def cache_path(kind: str, rcept_no: str) -> str:
    return os.path.join(_cache_dir(kind), f"{rcept_no}.html")


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def fetch_document_html(rcept_no: str, kind: str = "refix",
                        use_cache: bool = True, max_retries: int = 3,
                        timeout: int = 30):
    """rcept_no -> 원문 HTML(str) | None.

    use_cache=True 면 캐시 히트 시 신규 콜 0. kind 는 캐시 하위폴더(refix/conv).
    반환: (html:str|None, source:'cache'|'fetch'|'fail').
    """
    rcept_no = str(rcept_no).strip()
    cp = cache_path(kind, rcept_no)
    if use_cache and os.path.exists(cp):
        try:
            with open(cp, encoding="utf-8") as f:
                return f.read(), "cache"
        except OSError:
            pass

    backoff = 1.0
    for _ in range(max_retries):
        try:
            r = requests.get(DOC_URL,
                             params={"crtfc_key": config.DART_API_KEY,
                                     "rcept_no": rcept_no},
                             timeout=timeout)
        except requests.RequestException:
            time.sleep(backoff); backoff *= 2; continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(backoff); backoff *= 2; continue
        if r.content[:2] != b"PK":
            # DART status XML (에러/유량초과 등). 유량이면 백오프 재시도.
            body = r.text[:200]
            if "020" in body:
                time.sleep(backoff); backoff *= 2; continue
            return None, "fail"
        try:
            z = zipfile.ZipFile(io.BytesIO(r.content))
            html = _decode(z.read(z.namelist()[0]))
        except Exception:
            return None, "fail"
        try:
            with open(cp, "w", encoding="utf-8") as f:
                f.write(html)
        except OSError:
            pass
        return html, "fetch"
    return None, "fail"
