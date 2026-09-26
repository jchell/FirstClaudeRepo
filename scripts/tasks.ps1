<#
.SYNOPSIS
  Runs the data platform on a Windows laptop (Docker Desktop, WSL2 backend).

.EXAMPLE
  ./scripts/tasks.ps1 init     # first-time setup: Vault init, migrations, admin user
  ./scripts/tasks.ps1 up       # start everything (unseals Vault)
  ./scripts/tasks.ps1 up -Lite # start without Kafka Connect/Debezium (smaller machines)
  ./scripts/tasks.ps1 vault    # re-apply Vault engines/policies/AppRoles after an upgrade
  ./scripts/tasks.ps1 dev-up   # start plus the seeded test sources (add -Mssql for SQL Server)
  ./scripts/tasks.ps1 down     # stop (data is kept)
  ./scripts/tasks.ps1 test     # unit tests + integration tests against the running stack
  ./scripts/tasks.ps1 reset    # DELETE all platform data and bootstrap material

.NOTES
  Bootstrap material lives in %USERPROFILE%\.dataplat, outside the repo. The Vault
  unseal key is stored encrypted with Windows DPAPI (current user) and is only ever
  decrypted in memory, then piped into the unseal step.
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("init", "up", "dev-up", "seed", "down", "vault", "unseal", "test", "logs", "reset")]
    [string]$Command = "up",
    [switch]$Lite,
    [switch]$Mssql
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$env:DATAPLAT_HOME = Join-Path $env:USERPROFILE ".dataplat"
if ($Lite) { $env:COMPOSE_PROFILES = "" } elseif (-not $env:COMPOSE_PROFILES) { $env:COMPOSE_PROFILES = "full" }

$PlainInit = Join-Path $env:DATAPLAT_HOME "vault-init.json"
$ProtectedInit = Join-Path $env:DATAPLAT_HOME "vault-init.dpapi"
$Infra = @("postgres", "minio", "vault", "redpanda", "oxigraph")

function Invoke-Compose { docker compose @args; if ($LASTEXITCODE -ne 0) { throw "docker compose $args failed" } }

function Protect-InitFile {
    if (Test-Path $PlainInit) {
        Get-Content -Raw $PlainInit | ConvertTo-SecureString -AsPlainText -Force |
            ConvertFrom-SecureString | Set-Content -NoNewline $ProtectedInit
        Remove-Item -Force $PlainInit
        Write-Host "Vault unseal key protected with DPAPI at $ProtectedInit"
    }
}

function Get-InitJson {
    if (-not (Test-Path $ProtectedInit)) { throw "No $ProtectedInit - run 'tasks.ps1 init' first." }
    $secure = Get-Content -Raw $ProtectedInit | ConvertTo-SecureString
    return [System.Net.NetworkCredential]::new("", $secure).Password
}

function Invoke-Bootstrap([string]$Step) {
    if (Test-Path $ProtectedInit) {
        Get-InitJson | docker compose run --rm -T bootstrap bootstrap $Step --stdin
    } else {
        docker compose run --rm -T bootstrap bootstrap $Step
    }
    if ($LASTEXITCODE -ne 0) { throw "bootstrap $Step failed" }
}

switch ($Command) {
    "init" {
        New-Item -ItemType Directory -Force $env:DATAPLAT_HOME | Out-Null
        Invoke-Compose build
        Invoke-Compose run --rm -T bootstrap bootstrap prepare
        Invoke-Compose up -d @Infra
        Invoke-Bootstrap vault
        Protect-InitFile
        Invoke-Compose run --rm api migrate
        Invoke-Compose run --rm api create-admin --username admin
        Invoke-Compose up -d
        Write-Host "`nConsole: http://localhost:3000  (log in as 'admin' with the password shown above)"
    }
    "up" {
        Invoke-Compose up -d vault
        Invoke-Bootstrap unseal
        Invoke-Compose up -d
        Invoke-Compose run --rm api migrate
        Write-Host "Console: http://localhost:3000"
    }
    "dev-up" {
        & $PSCommandPath up -Lite:$Lite
        $env:COMPOSE_FILE = "docker-compose.yml;docker-compose.dev.yml"
        if ($Mssql) { $env:COMPOSE_PROFILES = "$env:COMPOSE_PROFILES,mssql" }
        Invoke-Compose up -d --wait --no-build
        if ($Mssql) { Invoke-Compose run --rm -T seed landing postgres mysql mongo sftp ftp smb s3 kafka mssql }
        else { Invoke-Compose run --rm -T seed }
        Write-Host "Test sources are up and seeded (see docker-compose.dev.yml for their names)."
    }
    "seed" { $env:COMPOSE_FILE = "docker-compose.yml;docker-compose.dev.yml"; Invoke-Compose run --rm -T seed }
    "vault" { Invoke-Bootstrap vault; Protect-InitFile }
    "unseal" { Invoke-Bootstrap unseal }
    "down" { $env:COMPOSE_FILE = "docker-compose.yml;docker-compose.dev.yml"; Invoke-Compose down }
    "logs" { docker compose logs -f --tail 100 }
    "test" {
        Push-Location backend
        try {
            uv run --extra dev pytest -q
            uv run --extra dev pytest -q -m integration
        } finally { Pop-Location }
    }
    "reset" {
        $answer = Read-Host "This deletes ALL platform data and $env:DATAPLAT_HOME. Type 'reset' to continue"
        if ($answer -ne "reset") { Write-Host "Cancelled."; return }
        $env:COMPOSE_FILE = "docker-compose.yml;docker-compose.dev.yml"
        docker compose --profile full --profile tools --profile mssql down -v
        Remove-Item -Recurse -Force $env:DATAPLAT_HOME -ErrorAction SilentlyContinue
        Write-Host "Reset complete. Run 'tasks.ps1 init' to start fresh."
    }
}
