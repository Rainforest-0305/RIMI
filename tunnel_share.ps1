# -*- coding: utf-8 -*-
# 미리(MIRI) — 무계정 공개 URL 공유 스크립트 (Cloudflare Quick Tunnel)
#
# 하는 일:
#   1) uvicorn 으로 app:api 를 127.0.0.1:8137 에 띄운다 (프론트 web/ + API 동시 서빙).
#   2) cloudflared quick tunnel 로 https://<랜덤>.trycloudflare.com 공개 URL 을 발급한다.
#      -> 계정/로그인/도메인 불필요. 이 URL 을 타인에게 공유하면 바로 접속 가능.
#   3) Ctrl+C 로 종료하면 uvicorn·cloudflared 프로세스를 함께 정리한다.
#
# 전제:
#   - cloudflared: bin\cloudflared.exe 우선, 없으면 PATH 의 cloudflared 사용.
#     (없으면 하단 안내대로 다운로드하거나 ngrok 대안 사용)
#   - DART_API_KEY: config.py 가 kis-trading\.env 또는 로컬 .env 에서 읽음.
#     키가 없어도 프론트/URL 발급은 되지만 공시 피드는 비게 됨.
#
# 사용:  powershell -ExecutionPolicy Bypass -File .\tunnel_share.ps1
#        (선택) -Port 9000  으로 포트 변경 가능.

param(
    [int]$Port = 8137
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# ---- cloudflared 경로 결정 ----
$cf = Join-Path $root "bin\cloudflared.exe"
if (-not (Test-Path $cf)) {
    $inPath = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($inPath) {
        $cf = $inPath.Source
    } else {
        Write-Host "[ERR] cloudflared 를 찾을 수 없습니다." -ForegroundColor Red
        Write-Host "  해결: 아래 바이너리를 bin\cloudflared.exe 로 저장 후 재실행하세요."
        Write-Host "  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        Write-Host "  또는 대안(ngrok): ngrok http $Port  (단, ngrok 은 authtoken 무료가입 필요)"
        exit 1
    }
}

Write-Host "[1/3] uvicorn 기동: http://127.0.0.1:$Port  (app:api)" -ForegroundColor Cyan
# uvicorn 을 별도 프로세스로 띄운다 (workers 1 = 노트북 부하 배려).
$uv = Start-Process -FilePath "python" `
    -ArgumentList @("-m", "uvicorn", "app:api", "--host", "127.0.0.1", "--port", "$Port", "--workers", "1") `
    -PassThru -NoNewWindow

# ---- uvicorn 헬스 대기 (최대 ~30초) ----
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
}
if (-not $healthy) {
    Write-Host "[ERR] uvicorn 헬스체크 실패. app.py 로그를 확인하세요." -ForegroundColor Red
    if ($uv -and -not $uv.HasExited) { Stop-Process -Id $uv.Id -Force }
    exit 1
}
Write-Host "      uvicorn OK (/api/health 200)" -ForegroundColor Green

Write-Host "[2/3] cloudflared quick tunnel 발급 중... (수 초 소요)" -ForegroundColor Cyan
Write-Host "      바이너리: $cf"

# cloudflared 를 띄우고 stderr/stdout 에 나오는 trycloudflare.com URL 을 잡아 강조 출력.
$logFile = Join-Path $env:TEMP ("miri_tunnel_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
$cfProc = Start-Process -FilePath $cf `
    -ArgumentList @("tunnel", "--no-autoupdate", "--url", "http://localhost:$Port") `
    -PassThru -NoNewWindow -RedirectStandardError $logFile -RedirectStandardOutput "$logFile.out"

$publicUrl = $null
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Path $logFile) {
        $hit = Select-String -Path $logFile -Pattern "https://[a-zA-Z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { $publicUrl = $hit.Matches[0].Value; break }
    }
    if ($cfProc.HasExited) { break }
}

if ($publicUrl) {
    Write-Host ""
    Write-Host "==================================================================" -ForegroundColor Yellow
    Write-Host " [3/3] 공개 URL (타인 공유 가능):" -ForegroundColor Green
    Write-Host "   $publicUrl" -ForegroundColor Yellow
    Write-Host "==================================================================" -ForegroundColor Yellow
    Write-Host " 종료: 이 창에서 Ctrl+C (uvicorn·cloudflared 자동 정리)"
    Write-Host " 터널 로그: $logFile"
} else {
    Write-Host "[ERR] 공개 URL 을 얻지 못했습니다. 로그: $logFile" -ForegroundColor Red
    if ($cfProc -and -not $cfProc.HasExited) { Stop-Process -Id $cfProc.Id -Force }
    if ($uv -and -not $uv.HasExited) { Stop-Process -Id $uv.Id -Force }
    exit 1
}

# ---- 종료 시 정리 ----
try {
    # cloudflared 프로세스가 살아있는 동안 대기.
    while (-not $cfProc.HasExited) { Start-Sleep -Seconds 2 }
} finally {
    Write-Host "`n[정리] 프로세스 종료 중..." -ForegroundColor Cyan
    if ($cfProc -and -not $cfProc.HasExited) { Stop-Process -Id $cfProc.Id -Force -ErrorAction SilentlyContinue }
    if ($uv -and -not $uv.HasExited) { Stop-Process -Id $uv.Id -Force -ErrorAction SilentlyContinue }
    Write-Host "[정리] 완료." -ForegroundColor Green
}
