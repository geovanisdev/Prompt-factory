<#
.SYNOPSIS
    Prompt Factory -- regressao rapida da pipeline inteira (M4).

.DESCRIPTION
    Roda s01..s06 com --max-rows 200 num diretorio de dados DESCARTAVEL, montado
    a partir de um recorte de data/raw/. Nunca encosta em data/interim,
    data/final, data/emb nem no SQLite de producao.

    Passos:
      1. copia 200 linhas de cada fonte de data/raw/ para o diretorio temporario
         (scripts/make_smoke_raw.py -- escrita de dados sempre de dentro do Python);
      2. pf run s01-s06 --max-rows 200 --data-dir <tmp>;
      3. pf report universe --data-dir <tmp>;
      4. pf report dedup-sample --data-dir <tmp> (pode nao achar par nenhum:
         com 200 linhas por fonte o near-dup e raro, e isso nao e erro).

    A primeira execucao numa maquina limpa baixa o modelo e5-small (~450 MB) no
    HF_HOME; depois disso o smoke roda em poucos minutos.

.PARAMETER KeepArtifacts
    Nao apaga o diretorio temporario no fim (para inspecionar os parquets).

.PARAMETER Rows
    Linhas por fonte (padrao 200).

.EXAMPLE
    pwsh -File scripts/smoke_test.ps1
    pwsh -File scripts/smoke_test.ps1 -KeepArtifacts -Rows 500
#>
[CmdletBinding()]
param(
    [switch]$KeepArtifacts,
    [int]$Rows = 200
)

$ErrorActionPreference = 'Stop'
$raiz = Split-Path -Parent $PSScriptRoot
$uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
if (-not (Test-Path $uv)) { $uv = 'uv' }  # PATH novo ja enxerga o uv

function Invoke-Passo {
    param([string]$Titulo, [string[]]$Args)
    Write-Host ""
    Write-Host "=== $Titulo ===" -ForegroundColor Cyan
    & $uv @Args
    if ($LASTEXITCODE -ne 0) {
        throw "[smoke_test] FALHOU em '$Titulo' (codigo $LASTEXITCODE)"
    }
}

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("pf-smoke-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
Write-Host "[smoke_test] data-dir descartavel: $tmp" -ForegroundColor Yellow

$falhou = $null
try {
    Push-Location $raiz
    Invoke-Passo 'recorte do raw' @('run', 'python', 'scripts/make_smoke_raw.py', $tmp, '--rows', "$Rows")
    Invoke-Passo 'pipeline s01-s06' @('run', 'pf', 'run', 's01-s06', '--max-rows', "$Rows", '--data-dir', $tmp)
    Invoke-Passo 'report universe' @('run', 'pf', 'report', 'universe', '--data-dir', $tmp)
    Invoke-Passo 'report dedup-sample' @('run', 'pf', 'report', 'dedup-sample', '--data-dir', $tmp)

    $universo = Join-Path $tmp 'final\universe.parquet'
    $emb = Join-Path $tmp 'emb\universe.f16.npy'
    foreach ($arquivo in @($universo, $emb)) {
        if (-not (Test-Path $arquivo)) { throw "[smoke_test] saida faltando: $arquivo" }
    }
    Write-Host ""
    Write-Host "[smoke_test] PASS" -ForegroundColor Green
}
catch {
    $falhou = $_
    Write-Host ""
    Write-Host "[smoke_test] FAIL: $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    Pop-Location
    if ($KeepArtifacts -or $falhou) {
        Write-Host "[smoke_test] artefatos preservados em $tmp"
    }
    else {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
}

if ($falhou) { exit 1 }
exit 0
