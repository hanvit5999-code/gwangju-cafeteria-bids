# PC에서 전체 확인(온비드·전남대 등 해외서버에서 막힌 곳 포함) 후 결과를 GitHub에 올림
# → GitHub이 받아서 웹페이지를 다시 만듦. 작업 스케줄러가 매일 실행.
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
$env:BID_DATA_DIR = "data"
$env:PYTHONIOENCODING = "utf-8"
$logFile = Join-Path $PSScriptRoot "data\pc_sync.log"
function Write-Log($m) { "[$(Get-Date -Format 'yyyy-MM-dd HH:mm')] $m" | Out-File -FilePath $logFile -Append -Encoding utf8 }

# 1) 서버가 올린 최신 결과 받기
git pull --rebase --autostash -q 2>&1 | ForEach-Object { Write-Log "pull: $_" }

# 2) 전체 확인 (윈도우 알림 포함)
$py = "C:\Users\aegug\AppData\Local\Python\pythoncore-3.15-64\python.exe"
& $py "입찰알림.py" 2>&1 | Select-Object -Last 3 | ForEach-Object { Write-Log "run: $_" }

# 3) 결과 올리기
git add data/seen.json data/found.json data/state.json
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -q -m "PC 확인 결과 $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
    for ($i = 0; $i -lt 3; $i++) {
        git pull --rebase -X theirs -q 2>&1 | ForEach-Object { Write-Log "pull2: $_" }
        git push -q 2>&1 | ForEach-Object { Write-Log "push: $_" }
        if ($LASTEXITCODE -eq 0) { Write-Log "올리기 완료"; break }
        Start-Sleep 20
    }
} else { Write-Log "바뀐 내용 없음" }
