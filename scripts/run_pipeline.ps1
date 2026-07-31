<#
.SYNOPSIS
    Prompt Factory -- pipeline de preparo do universo, ponta a ponta (M4).

.DESCRIPTION
    Encadeia os estagios ja implementados:

        pf ingest              (opcional, -Ingest) -- M2/M3, horas de download
        pf run s01 s02 s03 s04 -- normalize, idioma+variante, PII, dedup exato
        pf run s05             -- embeddings e5-small (20-60 min em CPU)
        pf run s06             -- dedup proximo -> data/final/universe.parquet
        pf report universe
        pf report dedup-sample -- grava data/final/dedup_sample_50.txt

    Os estagios sao retomaveis e idempotentes: rodar de novo nao duplica nada.
    O s05 retoma do sidecar (data/emb/progress.json) se for interrompido.

    Depois desta cadeia vem a campanha de rotulagem (M5), que NAO entra no
    `pf run all` de proposito -- tem revisao humana no meio:
        pf make-seed           -- s07 semente 12k + 155 lotes + manifest
        pf labels ...          -- campanha (ver .claude/skills/rotular-prompts)
        pf merge-labels        -- s08 -> data/final/seed_labels.parquet

    Depois dos rotulos vem a carga do banco (M8), que tambem fica FORA do
    `pf run all` -- ela troca o arquivo que o `pf serve` esta lendo:
        pf load-db             -- s11 -> data/db/prompts.sqlite (build + swap)
        pf db-check --bench    -- mede as consultas da interface no banco real

    Ainda stubs: pf train / pf apply (M7), pf export (M9), pf serve (M9).

.PARAMETER Ingest
    Roda `pf ingest` antes (todas as fontes default_on). Sem isso o script
    assume que data/raw/ ja esta populado.

.PARAMETER Stages
    Sobrescreve os estagios (ex.: 's04-s06'). Padrao: 's01-s06'.

.EXAMPLE
    pwsh -File scripts/run_pipeline.ps1
    pwsh -File scripts/run_pipeline.ps1 -Stages 's05-s06'
#>
[CmdletBinding()]
param(
    [switch]$Ingest,
    [string]$Stages = 's01-s06'
)

$ErrorActionPreference = 'Stop'
$raiz = Split-Path -Parent $PSScriptRoot
$uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
if (-not (Test-Path $uv)) { $uv = 'uv' }

function Invoke-Passo {
    param([string]$Titulo, [string[]]$Args)
    Write-Host ""
    Write-Host "=== $Titulo ===" -ForegroundColor Cyan
    $inicio = Get-Date
    & $uv @Args
    if ($LASTEXITCODE -ne 0) {
        throw "[run_pipeline] FALHOU em '$Titulo' (codigo $LASTEXITCODE)"
    }
    $seg = [int]((Get-Date) - $inicio).TotalSeconds
    Write-Host "[run_pipeline] '$Titulo' ok em $seg s" -ForegroundColor DarkGray
}

Push-Location $raiz
try {
    if ($Ingest) {
        Invoke-Passo 'ingest (M2/M3)' @('run', 'pf', 'ingest')
    }
    Invoke-Passo "run $Stages" @('run', 'pf', 'run', $Stages)
    Invoke-Passo 'report universe' @('run', 'pf', 'report', 'universe')
    Invoke-Passo 'report dedup-sample' @('run', 'pf', 'report', 'dedup-sample')
    Write-Host ""
    Write-Host "[run_pipeline] universo pronto em data/final/universe.parquet" -ForegroundColor Green
    Write-Host "[run_pipeline] REVISE data/final/dedup_sample_50.txt antes de seguir para o M5" -ForegroundColor Yellow
}
catch {
    Write-Host ""
    Write-Host "[run_pipeline] FAIL: $($_.Exception.Message)" -ForegroundColor Red
    Pop-Location
    exit 1
}
Pop-Location
exit 0
