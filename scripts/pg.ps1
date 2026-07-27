# Portable PostgreSQL control. No admin rights, no Docker, no Windows service.
#
#   scripts\pg.ps1 start     scripts\pg.ps1 stop
#   scripts\pg.ps1 status    scripts\pg.ps1 psql
#
# The cluster lives under $HOME\pgportable and listens on 5433 so it cannot
# collide with anything already using the default 5432.

param([Parameter(Position = 0)][ValidateSet('start','stop','status','psql','reset')]$Action = 'status')

$Root = Join-Path $HOME 'pgportable'
$Bin  = Join-Path $Root 'pgsql\bin'
$Data = Join-Path $Root 'data'
$Log  = Join-Path $Root 'pg.log'
$Port = 5433
$env:PGPASSWORD = 'amex'

if (-not (Test-Path $Bin)) {
    Write-Host "Not installed. Expected binaries at $Bin" -ForegroundColor Red
    Write-Host "Download postgresql-16.4-1-windows-x64-binaries.zip from EDB and extract to $Root"
    exit 1
}

switch ($Action) {
    'start' {
        & "$Bin\pg_ctl.exe" -D $Data -l $Log -o "-p $Port" start
    }
    'stop' {
        & "$Bin\pg_ctl.exe" -D $Data stop -m fast
    }
    'status' {
        & "$Bin\pg_ctl.exe" -D $Data status
    }
    'psql' {
        & "$Bin\psql.exe" -h 127.0.0.1 -p $Port -U amex -d servicing
    }
    'reset' {
        # Drop and recreate the database, then reseed. The audit chain goes
        # with it — that is why this is a separate, explicit command.
        & "$Bin\psql.exe" -h 127.0.0.1 -p $Port -U amex -d postgres -c "DROP DATABASE IF EXISTS servicing;"
        & "$Bin\psql.exe" -h 127.0.0.1 -p $Port -U amex -d postgres -c "CREATE DATABASE servicing;"
        Push-Location (Split-Path $PSScriptRoot -Parent)
        python backend/seed.py
        Pop-Location
    }
}
