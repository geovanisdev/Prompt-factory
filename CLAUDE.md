# CLAUDE.md — Prompt Factory

## O que é

Banco de **prompts escritos por pessoas reais** (primeiro turno de usuário em conversas com LLMs, nunca sintéticos), em português brasileiro e inglês, classificado por taxonomia e navegável numa interface local. Serve para curar coleções e exportar JSONL/CSV com licença e atribuição por linha, para alimentar plataformas de data annotation.

Plano completo (fontes, decisões de engenharia, marcos M0–M10): `C:\Users\gigio\.claude\plans\eu-preciso-criar-um-cuddly-grove.md`.

## Comandos

```powershell
# uv NÃO está no PATH desta máquina até abrir um terminal novo — use o caminho completo:
& "$env:USERPROFILE\.local\bin\uv.exe" sync                 # cria/atualiza .venv a partir do uv.lock
& "$env:USERPROFILE\.local\bin\uv.exe" run pf --help        # lista os subcomandos
& "$env:USERPROFILE\.local\bin\uv.exe" run pf report data/final/universe.parquet
& "$env:USERPROFILE\.local\bin\uv.exe" run pytest -q        # testes
& "$env:USERPROFILE\.local\bin\uv.exe" run ruff check .     # lint
& "$env:USERPROFILE\.local\bin\uv.exe" run pf serve         # interface local em http://127.0.0.1:8765
```

Subcomandos e em que marco cada um sai do stub: `ingest` (M2/M3), `run` s01–s06 (M4), `report` (M4), `db-check` (M1), `make-seed` (M5), `labels` (M5), `merge-labels` (M5), `train` (M7), `apply` (M7), `load-db` (M8), `export` (M9), `serve` (M9). Enquanto é stub, o comando imprime o aviso e sai com **código 2** — isso é esperado, não é bug.

## Pegadinhas (leia antes de mexer)

- **`uv` fora do PATH.** Foi instalado agora em `C:\Users\gigio\.local\bin\uv.exe` e a sessão atual não o enxerga. Sempre `& "$env:USERPROFILE\.local\bin\uv.exe" ...` até abrir um terminal novo.
- **TLS interceptado nesta máquina.** `uv sync` puro falha com `invalid peer certificate: UnknownIssuer` ao buscar o PyPI: há um middlebox (antivírus/proxy) reassinando o tráfego, e a raiz dele só existe no armazenamento de certificados do Windows, não no bundle embutido do uv. A correção é `system-certs = true` em `[tool.uv]` no `pyproject.toml` (já commitado) — equivale a `uv sync --system-certs` e faz o uv confiar no trust store do SO. (Em uv < 0.12 a chave chamava `native-tls`; usá-la no uv 0.12 gera warning de depreciação em *todo* comando.) **Não** use `--allow-insecure-host`: isso desliga a verificação de verdade.
- **Nunca commitar `data/`.** Parquet, `.sqlite`, `.npy` e exports são artefatos regeneráveis e grandes. O `.gitignore` deixa passar só os `.gitkeep`. O `uv.lock`, ao contrário, **é** commitado.
- **Nunca ler parquet com `cat`/`Get-Content`/`Read`.** É binário: enche o contexto de lixo e não responde nada. Use `pf report <arquivo>`.
- **PowerShell corrompe encoding em redirecionamento.** `>` e `Out-File` gravam UTF-16/BOM e quebram acentuação e JSONL. **Toda escrita de dados sai de dentro do Python** (pyarrow, `open(..., encoding="utf-8")`, `csv` com `utf-8-sig` só no export CSV). Redirecionar em PowerShell serve, no máximo, para log descartável.
- **UTF-8 garantido pela CLI.** `cli.main()` faz `PYTHONUTF8=1` e reconfigura stdout/stderr para utf-8 antes de qualquer coisa — mas isso só vale para processos iniciados por `pf`.
- **`HF_HOME` vai para `G:/hf-cache`** (via `[general] hf_home` do `settings.toml`, aplicado pelo `cli.main()`). O `C:` não aguenta o cache de WildChat + torch.
- **Thresholds moram em `config/settings.toml`**, nunca hardcoded — é o que torna os estágios replayáveis.

## Arquitetura em 5 linhas

1. Estágios `s01`–`s12` são funções puras arquivo→arquivo; cada um lê e escreve **Parquet imutável** em `data/`, então tudo é retomável e replayável.
2. `raw/{fonte}.parquet` → s01 normalize → s02 idioma+variante pt-BR/pt-PT → s03 PII → s04 dedup exato → s05 embed (e5-small, `.npy` f16) → s06 dedup próximo → `final/universe.parquet`.
3. s07 amostra-semente → campanha de rotulagem por agentes → s08 merge → s09 treino → s10 aplica com limiares de confiança.
4. s11 carrega o universo rotulado num **SQLite** (WAL + FTS5 `remove_diacritics 2`), construído à parte e trocado por swap de arquivo.
5. s12/app: FastAPI toca **apenas** o SQLite (mais os `.npy` por mmap na busca semântica) e serve um `static/index.html` único, sem build.

## Arquivos críticos

`src/prompt_factory/schema.py` (contrato canônico das colunas) · `db.py` (DDL/FTS/conexão) · `cli.py` (entrypoint) · `labeling/taxonomy.json` (fonte única da taxonomia) · `config/sources.toml` (licença e atribuição por fonte) · `.claude/skills/rotular-prompts/SKILL.md`.
