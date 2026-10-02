$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    $pyCommand = Get-Command py -ErrorAction SilentlyContinue
    $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if ($pythonCommand -and $pythonCommand.Source -notlike '*WindowsApps*') {
        & $pythonCommand.Source -m venv .venv
    } elseif ($pyCommand) {
        & $pyCommand.Source -3 -m venv .venv
    } elseif (Test-Path -LiteralPath $bundledPython) {
        & $bundledPython -m venv .venv
    } else {
        throw 'Python 3.10 이상을 설치한 뒤 다시 실행해 주세요: https://www.python.org/downloads/'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Python 가상환경을 만들지 못했습니다.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw '의존성 설치에 실패했습니다. 인터넷 연결을 확인해 주세요.' }
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
& '.\.venv\Scripts\python.exe' app.py
