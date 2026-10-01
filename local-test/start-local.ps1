# Run the log viewer on this PC: http://localhost:8081/ui/
#
#   start-local.cmd                     synthetic sample logs (generated on the first run, no AWS needed)
#   start-local.cmd -RealLogs           the real logs in s3://fenix-ecr-logs/vector/ (needs AWS credentials
#                                       that may read that bucket: -AwsProfile <profile> or your default ones)
#
# Users, saved searches and secrets stay in local-test\.store and local-test\.secrets (never committed).
param(
    [switch]$RealLogs,
    [string]$AwsProfile = "",
    [string]$Bucket = "fenix-ecr-logs",
    [string]$Prefix = "vector/",
    [string]$Region = "us-west-2",
    [int]$Port = 8081
)
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$AdminUser = "latish.madapada@fenixcommerce.com"
function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

# ---- Python virtual environment (first run only)
$venv = Join-Path $ProjectDir ".venv"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Step "Creating a Python virtual environment in $venv"
    $exe = $null
    foreach ($c in @("py -3.12", "py -3.11", "py -3", "python")) {
        $parts = $c.Split(" ")
        try { & $parts[0] @($parts | Select-Object -Skip 1) --version *> $null; if ($LASTEXITCODE -eq 0) { $exe = $parts; break } } catch {}
    }
    if (-not $exe) { throw "Install Python 3.12 from https://www.python.org/downloads/ (tick 'Add to PATH'), then run again" }
    & $exe[0] @($exe | Select-Object -Skip 1) -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment" }
}
Write-Step "Installing / checking Python packages"
& $py -m pip install --disable-pip-version-check -q -r (Join-Path $ProjectDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed (a corporate proxy may need PIP_INDEX_URL / PIP_CERT)" }

# ---- first admin password, generated once and kept in local-test\.secrets
$secretsDir = Join-Path $PSScriptRoot ".secrets"
$pwFile = Join-Path $secretsDir "local-admin-password.txt"
New-Item -ItemType Directory -Force -Path $secretsDir | Out-Null
if (-not (Test-Path $pwFile)) {
    $chars = [char[]]"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    $bytes = New-Object byte[] 20
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    (-join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })) + "-7a" | Set-Content -Encoding ascii -NoNewline $pwFile
}
$AdminPassword = (Get-Content -Raw $pwFile).Trim()

# ---- where the logs come from
if ($RealLogs) {
    $env:LOGS_BACKEND = "s3"; $env:LOGS_BUCKET = $Bucket; $env:LOGS_PREFIX = $Prefix; $env:LOGS_REGION = $Region
    if ($AwsProfile) { $env:AWS_PROFILE = $AwsProfile }
    $env:CACHE_DIR = Join-Path $PSScriptRoot ".cache"
    $where = "s3://$Bucket/$Prefix" + $(if ($AwsProfile) { " (profile $AwsProfile)" } else { "" })
} else {
    $logs = Join-Path $PSScriptRoot "logs"
    if (-not (Test-Path (Join-Path $logs "vector"))) {
        Write-Step "Generating synthetic sample logs in $logs (once)"
        & $py (Join-Path $PSScriptRoot "make_sample_logs.py") $logs
        if ($LASTEXITCODE -ne 0) { throw "Could not generate the sample logs" }
    }
    $env:LOGS_BACKEND = "local"; $env:LOGS_LOCAL_DIR = $logs; $env:LOGS_PREFIX = "vector/"
    $env:CACHE_DIR = Join-Path $PSScriptRoot ".cache"
    $where = "$logs\vector (synthetic; delete the folder to generate fresh ones)"
}
$env:STORAGE_BACKEND = "local"; $env:LOCAL_STORE_DIR = Join-Path $PSScriptRoot ".store"
$env:SECRETS_BACKEND = "local"; $env:LOCAL_SECRETS_DIR = $secretsDir
$env:AUTH_MODE = "password"
$env:BOOTSTRAP_ADMINS = $AdminUser
$env:BOOTSTRAP_ADMIN_PASSWORD = $AdminPassword
$env:PYTHONDONTWRITEBYTECODE = "1"

Write-Step "Starting the log viewer"
Write-Host "    Logs    : $where"
Write-Host "    Admin   : $AdminUser  (first password: $AdminPassword - only until you change it)"
Write-Host "    Console : http://localhost:$Port/ui/" -ForegroundColor White
Write-Host "    API docs: http://localhost:$Port/docs   (Ctrl+C here to stop)" -ForegroundColor White
Set-Location $ProjectDir
& $py -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port $Port
