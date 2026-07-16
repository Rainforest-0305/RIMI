# MIRI 공시앱 — TWA (Trusted Web Activity) Play Store 빌드 절차서

대상 PWA: `https://rimi-s76t.onrender.com` (Render, singapore)
로컬 경로: `C:\Users\urimk\ventures\gongsi-alert` (`web\` = PWA 소스)
문서 갱신: 2026-07-16 (WS-24, 모바일 파트)

> 이 문서는 **재현 절차서**다. 실제로 실행해서 검증한 부분과, 게이트(승인 대기)로
> 문서로만 남긴 부분을 각 섹션에 명시한다.
> **배포·스토어 제출·키스토어 실등록·assetlinks 실배포·Play 콘솔 등록은 전부 Partner/President 게이트.**

---

## 0. 이 머신에서 실측된 환경 (2026-07-16)

| 항목 | 상태 |
|------|------|
| node | v24.18.0 (설치됨) |
| npm | 11.16.0 (설치됨) |
| java / keytool | **미설치** (시스템 PATH에 없음) |
| JAVA_HOME / ANDROID_HOME | 비어 있음 |
| Bubblewrap 자동설치 JDK 17 | **성공** → `C:\Users\urimk\.bubblewrap\jdk\jdk-17.0.11+9` |
| Bubblewrap 자동설치 Android SDK | 다운로드 진행/완료 → `C:\Users\urimk\.bubblewrap\android_sdk` |

핵심: **시스템에 JDK/keytool이 없어도 Bubblewrap CLI가 JDK 17과 Android SDK를 자동 다운로드**한다.
Bubblewrap가 설치한 JDK 안에 `keytool`이 포함되므로, 시스템 keytool 미설치여도
키스토어 생성은 그 JDK의 keytool로 가능하다 (아래 3장).

---

## 1. 패키지명 (Application ID)

### 최종 권장값: `kr.miri.gongsi`

- **역-도메인(reverse-DNS) 규칙**: `kr` (한국) . `miri` (브랜드) . `gongsi` (공시).
- **Play Store 전역 고유(unique)**: applicationId는 Google Play 전체에서 유일해야 한다.
  한 번 스토어에 올리면 **영구히 변경 불가**(변경 시 완전히 다른 앱으로 취급, 기존 설치·리뷰 승계 불가).
  그러므로 최초 선정이 중요하다.
- 대안 `kr.miri.alert`도 유효하나, 서비스 정체성이 "공시(gongsi)"이므로 `kr.miri.gongsi` 권장.
- Play Console 등록 전, 해당 ID가 이미 점유되지 않았는지 확인 권장
  (`https://play.google.com/store/apps/details?id=kr.miri.gongsi` 가 404여야 신규 가용).

이 값은 아래 모든 곳에서 **동일하게** 써야 한다:
- Bubblewrap `twa-manifest.json` 의 `packageId`
- `web/.well-known/assetlinks.json` 의 `package_name`
- Play Console 앱 생성 시 패키지명

---

## 2. AAB 생성 파이프라인 (Bubblewrap)

### 2.1 사전 준비 (한 번만)

```bash
# 어느 작업 폴더에서든 실행 가능. 예시는 스크래치 폴더.
npx --yes @bubblewrap/cli doctor
```

- **대화형(interactive) 프롬프트**가 뜬다:
  1. `Do you want Bubblewrap to install the JDK (recommended)? (Y/n)`
  2. `Do you want Bubblewrap to install the Android SDK (recommended)? (Y/n)`
  3. `Do you agree to the Android SDK terms and conditions ...? (y/N)`
- TTY가 없는 셸(자동화/CI)에서는 stdin EOF로 `ERR_USE_AFTER_CLOSE: readline was closed` 에러가 난다.
  → 해결: 대화형 터미널에서 직접 실행하거나, `Y` 입력을 파이프로 주입.
  (주의: `yes Y | ...` 는 프롬프트 통과에는 되지만 출력이 폭주하므로 실제 터미널 실행을 권장.)

설치 결과 경로:
- JDK 17: `C:\Users\urimk\.bubblewrap\jdk\jdk-17.0.11+9`
- Android SDK: `C:\Users\urimk\.bubblewrap\android_sdk`
- 설정: `C:\Users\urimk\.bubblewrap\config.json`
  (`{"jdkPath":"...jdk-17.0.11+9","androidSdkPath":"...android_sdk"}`)

### 2.2 프로젝트 초기화 (init)

빈 작업 폴더(예: `C:\Users\urimk\ventures\gongsi-alert\playstore\twa`)에서:

```bash
mkdir -p "C:/Users/urimk/ventures/gongsi-alert/playstore/twa"
cd "C:/Users/urimk/ventures/gongsi-alert/playstore/twa"
npx --yes @bubblewrap/cli init --manifest https://rimi-s76t.onrender.com/manifest.json
```

- **대화형**: manifest를 읽어 기본값을 채운 뒤 여러 항목을 물어본다. 주요 확정값:
  - **Application name**: `미리 · MIRI 공시앱` (manifest `name`)
  - **Short name**: `미리`
  - **Application ID (packageId)**: 기본이 도메인 역순(`com.onrender.miri_gongsi.twa` 형태)으로
    제안되므로 **반드시 `kr.miri.gongsi` 로 직접 입력**.
  - **Display mode**: `standalone`
  - **Status bar color / theme**: `#0b0e1a` (manifest theme_color)
  - **Signing key**: 아래 3장에서 만든 키스토어 경로/alias 지정 (또는 init 중 새로 생성).
- 산출물: `twa-manifest.json`, `android` 프로젝트 스캐폴드, `app` 모듈 등.

비대화형이 필요하면 `twa-manifest.json` 을 미리 작성해두고 `init` 없이 `build` 로 진행하는
방법도 있으나, 최초 1회는 대화형 init 권장.

### 2.3 빌드 (AAB 생성)

```bash
cd "C:/Users/urimk/ventures/gongsi-alert/playstore/twa"
npx --yes @bubblewrap/cli build
```

- 산출물:
  - `app-release-bundle.aab`  ← **Play Store 업로드용 (이게 최종물)**
  - `app-release-signed.apk`  ← 로컬 테스트 설치용
- 빌드 시 키스토어 비밀번호를 물어본다(또는 `--skipPwaValidation`, `--signingKeyPath` 등 플래그).
- AAB 서명은 **업로드 키(upload key)** 로 이뤄진다(3장 참고).

### 2.4 이 머신에서 실제 시도한 결과 (실측 2026-07-16)

- `doctor`: JDK 17 자동설치 **성공**, Android SDK 자동설치 **성공**.
  최종 verdict: `doctor Your jdkpath and androidSdkPath are valid.`
- `init --manifest https://rimi-s76t.onrender.com/manifest.json` 실행 결과 — **manifest fetch 성공**(정본 URL 재실측):
  ```
  Initializing application from Web Manifest:
      -  https://rimi-s76t.onrender.com/manifest.json
  WARNING: Trusted Web Activities are currently incompatible with applications
  targeting children under the age of 13. ...
  Web app details (1/5)
  ? Domain: (rimi-s76t.onrender.com)   <- manifest에서 도메인 자동 추출, 대화형 프롬프트 진입
  ```
  → **init이 라이브 manifest(HTTP 200)를 정상 수집하고 5단계 대화형 프롬프트로 진입**했다.
  이후 이 비-TTY 자동화 셸에서만 `ERR_USE_AFTER_CLOSE: readline was closed`로 멈춘다(=TTY 이슈, 아래 2.5). 배포/manifest는 정상.
- 게이트 상 실제 스토어 업로드/서명키 Play 등록/키스토어 생성은 하지 않음.

### 2.5 유일한 차단지점 — 대화형(TTY) 프롬프트 (배포 정상)

**init이 자동화 셸에서 멈추는 원인은 배포가 아니라 대화형 프롬프트를 비-TTY 셸이 못 받기 때문이다.**

라이브 URL 실측(정본 도메인, 2026-07-16 재실측):
```
GET https://rimi-s76t.onrender.com/manifest.json                -> HTTP 200 (application/json)   [정상]
GET https://rimi-s76t.onrender.com/                             -> HTTP 200                       [정상]
GET https://rimi-s76t.onrender.com/.well-known/assetlinks.json  -> HTTP 404 (아직 미push)         [파리티 갭]
```

- manifest·루트는 **200 정상** — Render 서비스 가동 중. (초판 문서의 404/`no-server` 서술은 **존재하지 않는 stale 문서 URL**을 실측한 오보였고, 정본 도메인으로 정정함.)
- `/.well-known/assetlinks.json`만 **배포에서 404**다. 이유: 오늘 추가한 assetlinks 파일(`web/.well-known/assetlinks.json`)과 `app.py` 전용 라우트가 **아직 git push 안 됨**(push=배포=Partner 게이트). **로컬 서버에서는 200 + application/json** 확인됨 — 코드/파일은 정상이며, Partner 배포 게이트로 (실 SHA-256 포함) 파일이 push된 뒤에야 배포 도메인에서 200이 되어 DAL 검증이 통과한다.
- init은 manifest를 정상 fetch한 뒤 5단계 대화형 프롬프트(Domain/URL/이름/색/아이콘)로 진입한다. 이 비-TTY 자동화 셸에서는 stdin EOF로 `ERR_USE_AFTER_CLOSE: readline was closed`가 난다.

**코드 자체는 정상**(참고, `app.py` 읽기만 함 — 무접촉):
- `app.py:682` — `api.mount("/", StaticFiles(directory=web, html=True))` : `/manifest.json`, `/index.html`, 아이콘을 루트에서 서빙.
- `app.py:675-679` — `/.well-known/assetlinks.json` 전용 라우트, `media_type="application/json"` 200 서빙(파일: `web/.well-known/assetlinks.json`).

→ **결론**: 배포 재기동은 불필요(이미 가동 중). AAB 빌드 선결조건은 (a) 실제 터미널에서 init을 대화형 완주하거나 `twa-manifest.json`을 선작성해 비대화형 build, (b) DAL 검증용으로 실 SHA-256 넣은 assetlinks.json을 Partner 배포 게이트로 push.

### 2.6 (참고) 비대화형 build 진행법

실제 터미널에서 init을 대화형으로 완주하는 게 가장 단순하다. CI/자동화가 필요하면
`twa-manifest.json`을 미리 작성한 뒤 `bubblewrap build`를 비대화형으로 실행한다.
(위 2.5의 readline 에러는 이 비-TTY 자동화 셸 한정 현상이며, 배포/manifest와 무관하다.)

---

## 3. 서명 키스토어 (Keystore) 생성·보관

> **실생성은 게이트.** 아래는 명령·절차 문서화. 실행 시 비밀번호는 **절대 커밋/공유 금지**.
> 시스템 keytool은 없지만 Bubblewrap가 설치한 JDK의 keytool을 쓰면 된다.

### 3.1 키스토어 생성 명령 (전문)

```bash
# Bubblewrap JDK의 keytool 사용 (시스템 keytool 미설치 대응)
KEYTOOL="C:/Users/urimk/.bubblewrap/jdk/jdk-17.0.11+9/bin/keytool.exe"

"$KEYTOOL" -genkeypair \
  -alias miri-upload \
  -keyalg RSA \
  -keysize 2048 \
  -validity 10000 \
  -keystore "C:/Users/urimk/keys/miri-upload.keystore" \
  -storetype PKCS12 \
  -dname "CN=MIRI, OU=Mobile, O=MIRI, L=Seoul, ST=Seoul, C=KR"
# 실행 중 -storepass / -keypass 를 대화형으로 입력 (명령줄에 비번 넣지 말 것)
```

- `alias`: `miri-upload` (업로드 키 별칭)
- `keyalg RSA`, `keysize 2048`, `validity 10000`(약 27년) — Google 권장 최소치 충족.
- `storetype PKCS12` 권장(JKS는 레거시).
- **비밀번호**: 강한 문자열, 별도 비밀번호 관리자에 저장. 명령줄 인자로 넣으면 셸 히스토리에 남으므로 대화형 입력.

### 3.2 보관·백업 권고

- 저장 위치(로컬, 리포 밖): 예) `C:\Users\urimk\keys\miri-upload.keystore`
  → **리포지토리 밖**에 둘 것. (경로가 리포 안이라면 반드시 .gitignore, 5장 참고)
- 백업: 오프라인 2벌 이상(암호화 USB / 개인 비밀번호 관리자 첨부). **분실 시 업로드 키 재발급 절차 필요**.
- 비밀번호는 키스토어와 **분리 보관**.
- **커밋 금지 대상**: `*.keystore`, `*.jks`, 비밀번호 파일. (.gitignore에 추가 완료 — 5장)

### 3.3 Play App Signing (업로드 키 vs 앱 서명 키)

- **앱 서명 키(app signing key)**: 사용자에게 배포되는 APK를 실제로 서명하는 키.
  Play App Signing을 쓰면 **Google이 이 키를 보관·관리**한다(권장/기본).
- **업로드 키(upload key)**: 개발자가 AAB를 Play Console에 업로드할 때 서명하는 키(위 3.1에서 만든 것).
  Google이 업로드 키 서명을 검증한 뒤, 자신이 보관한 앱 서명 키로 재서명해 배포.
- 장점: 업로드 키를 분실/유출해도 재설정 가능(앱 서명 키는 Google이 안전 보관).
- **중요**: `assetlinks.json`의 SHA-256 지문은 **실제 앱에 서명되는 키 = 앱 서명 키**의 지문이어야 한다.
  Play App Signing 사용 시, 앱 서명 키 지문은 **Play Console → 앱 무결성(App integrity) → 앱 서명 키 인증서**
  에서 확인해 assetlinks에 넣어야 한다(업로드 키 지문이 아님!).
  단 업로드 키 지문도 함께 넣어두면 개발 중 로컬 검증에 편리하므로, **두 지문을 배열에 모두** 넣는 것을 권장.

---

## 4. assetlinks.json (Digital Asset Links)

파일 위치: `C:\Users\urimk\ventures\gongsi-alert\web\.well-known\assetlinks.json`
배포 후 접근 URL: `https://rimi-s76t.onrender.com/.well-known/assetlinks.json`

### 4.1 현재 파일 (플레이스홀더 SHA-256)

```json
[
  {
    "relation": ["delegate_permission/common.handle_all_urls"],
    "target": {
      "namespace": "android_app",
      "package_name": "kr.miri.gongsi",
      "sha256_cert_fingerprints": ["REPLACE_WITH_REAL_SHA256_AFTER_KEYSTORE"]
    }
  }
]
```

키스토어 실생성이 게이트라 지문은 플레이스홀더 상태.

### 4.2 실키 생성 후 SHA-256 지문 추출 → 교체

```bash
KEYTOOL="C:/Users/urimk/.bubblewrap/jdk/jdk-17.0.11+9/bin/keytool.exe"
"$KEYTOOL" -list -v \
  -keystore "C:/Users/urimk/keys/miri-upload.keystore" \
  -alias miri-upload
# 출력의 "SHA256:" 줄 (예: SHA256: AB:CD:...:EF) 값을 복사해 assetlinks의
# sha256_cert_fingerprints 배열에 넣는다. 콜론 포함 대문자 형식 그대로 OK.
```

- Play App Signing 사용 시(권장): **Play Console → App integrity** 의 앱 서명 키 SHA-256도 추가.
  → 최종 배열은 `[업로드키 지문, 앱서명키 지문]` 2개 권장.
- 교체 후 파일 예시:

```json
[
  {
    "relation": ["delegate_permission/common.handle_all_urls"],
    "target": {
      "namespace": "android_app",
      "package_name": "kr.miri.gongsi",
      "sha256_cert_fingerprints": [
        "AA:BB:CC:...:업로드키지문",
        "11:22:33:...:앱서명키지문(PlayConsole)"
      ]
    }
  }
]
```

### 4.3 배포 주의

- `web/.well-known/assetlinks.json` 은 **정적 파일로 위 경로에서 서빙**되어야 한다.
  Render/FastAPI(app.py) 라우팅에서 `.well-known/assetlinks.json` 이 200 + `content-type: application/json`
  으로 응답하는지 확인 필요(백엔드 파트 담당). **app.py 무접촉 — 라우팅은 백엔드 파트가 처리.**
- **실배포는 게이트.** SHA-256 교체 후 배포는 Partner 승인.

---

## 5. .gitignore (키스토어 커밋 방지)

`C:\Users\urimk\ventures\gongsi-alert\.gitignore` 에 아래 블록 **추가 완료**:

```
# === Android/TWA 서명 키스토어 (절대 커밋 금지) ===
*.keystore
*.jks
*.p12
*.pepk
android.keystore
upload-keystore.jks
signing-key-list.txt
keystore-passwords.txt
playstore/keystore/
playstore/*.keystore
playstore/*.jks
```

기존 `*.key`, `.env` 패턴과 함께 자격증명 커밋을 차단한다.

---

## 6. Digital Asset Links 검증 절차

키스토어 생성 → assetlinks 교체 → 배포(게이트 통과) **후**:

### 6.1 Google 공식 검증 API

```
https://digitalassetlinks.googleapis.com/v1/assetlinks:check?source.web.site=https://rimi-s76t.onrender.com&relation=delegate_permission/common.handle_all_urls&target.android_app.package_name=kr.miri.gongsi&target.android_app.certificate.sha256_fingerprint=AA:BB:CC:...
```

- `linked: true` 이면 연결 성공. (브라우저/`curl`로 GET)

### 6.2 파일 직접 확인

```bash
curl -s https://rimi-s76t.onrender.com/.well-known/assetlinks.json
# → 위 4.2의 최종 JSON이 그대로 200으로 응답해야 함
```

### 6.3 실기기 확인

- TWA 앱 설치 후, 앱 실행 시 **주소창(URL bar)이 없이 전체화면 standalone**으로 뜨면 Asset Links 검증 성공.
- 주소창이 보이면(Custom Tab 폴백) assetlinks 지문 불일치 — 4.2 재확인.

---

## 7. PWABuilder 대안 경로 (웹 기반, Bubblewrap 불가 시)

로컬에 JDK/SDK 설치가 어려운 환경 대안:

1. 브라우저에서 `https://www.pwabuilder.com` 접속.
2. URL 입력: `https://rimi-s76t.onrender.com` → Start / Analyze.
3. PWA 리포트 확인 후 **Package For Stores → Android (Google Play)** 선택.
4. 패키지 옵션 설정:
   - **Package ID**: `kr.miri.gongsi`
   - **App name**: `미리 · MIRI 공시앱`
   - **Signing key**:
     - "Create new" → PWABuilder가 키스토어를 생성해주고 `signing.keystore` + 비밀번호를
       zip에 동봉(비밀번호는 안전 보관, 절대 커밋 금지), 또는
     - "Use mine" → 3장에서 만든 키스토어 업로드.
5. **Download** → zip 안에 `app-release-signed.aab`(Play 업로드용) + `assetlinks.json`
   (PWABuilder가 사용한 서명키의 SHA-256이 이미 채워진 형태) 포함.
6. zip 내 `assetlinks.json` 의 지문을 `web/.well-known/assetlinks.json` 으로 반영(게이트).

- 장점: 로컬 JDK/SDK 불필요, 서명키·assetlinks 자동 생성.
- 주의: PWABuilder가 만든 서명키를 쓰면 그 키를 반드시 백업(분실 시 업데이트 불가).
  Play App Signing 등록 시 이 키를 업로드 키로 등록.

---

## 8. 게이트 요약 (Partner/President 승인 필수)

- 키스토어 실생성 및 Play App Signing 등록
- assetlinks.json 실제 SHA-256 반영 및 배포
- AAB Play Console 업로드 / 내부테스트·프로덕션 트랙 배포
- 스토어 리스팅 제출

이 문서의 범위: 재현 명령·환경 실측·플레이스홀더 산출물까지. 실배포는 승인 후.
