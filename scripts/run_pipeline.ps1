<#
.SYNOPSIS
    Prompt Factory — pipeline completa, ponta a ponta (STUB do M0).

.DESCRIPTION
    TODO (M4): preencher com a sequência real de estágios. Esqueleto previsto:

        pf ingest                 # M2/M3 — todas as fontes habilitadas -> data/raw/*.parquet
        pf run s01 s02 s03 s04    # M4 — normalize, idioma+variante, PII, dedup exato
        pf run s05 s06            # M4 — embed (e5-small, f16 .npy) e dedup próximo
        pf make-seed              # M5 — s07, amostra-semente estratificada
        # [campanha de rotulagem: skill rotular-prompts, várias sessões]
        pf merge-labels           # M5 — s08
        pf train                  # M7 — s09
        pf apply                  # M7 — s10
        pf load-db --swap         # M8 — s11
        pf export                 # M9 — s12

    Regras que este script DEVE respeitar quando for escrito:
      - `uv` pode não estar no PATH: invocar via "$env:USERPROFILE\.local\bin\uv.exe".
      - Parar no primeiro erro ($ErrorActionPreference = 'Stop' + checar $LASTEXITCODE).
      - Nenhum redirecionamento `>` de dados: PowerShell corrompe encoding.
        Toda escrita de dados sai de dentro do Python.
      - Cada estágio é retomável: rodar de novo não pode duplicar nada.

.NOTES
    Marco: M4. Até lá este script apenas avisa e sai com código 2.
#>

Write-Host "[run_pipeline] stub do M0 — a pipeline e' preenchida no M4." -ForegroundColor Yellow
exit 2
