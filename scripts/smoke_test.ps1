<#
.SYNOPSIS
    Prompt Factory — regressão rápida da pipeline inteira (STUB do M0).

.DESCRIPTION
    TODO (M4): a partir do M4 este é o teste de regressão PERMANENTE — roda a
    pipeline toda com `--max-rows 200` em cada estágio, num diretório de dados
    descartável, e confere que:

        - cada estágio produz parquet com o schema canônico (schema.py);
        - contagens de linhas batem com o esperado do subconjunto;
        - o SQLite carrega e o FTS acha "coração" buscando "coracao";
        - `pf export` gera JSONL/CSV cujo manifest bate com as contagens.

    Deve rodar em poucos minutos e ser seguro de executar a qualquer momento —
    nunca toca em data/db/prompts.sqlite de produção nem em data/final/.

    Invocação prevista:
        pwsh -File scripts/smoke_test.ps1 [-KeepArtifacts]

.NOTES
    Marco: M4. Até lá este script apenas avisa e sai com código 2.
#>

Write-Host "[smoke_test] stub do M0 — a regressao e' preenchida no M4." -ForegroundColor Yellow
exit 2
