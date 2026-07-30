# Prompt Factory

Banco de **prompts escritos por pessoas reais** — o primeiro turno de usuário em conversas com LLMs, nunca texto sintético — em português brasileiro e inglês, classificado por taxonomia (tipo de tarefa, domínio, qualidade, NSFW) e navegável numa interface local. A pipeline ingere fontes abertas do HuggingFace, normaliza, detecta idioma e variante pt-BR/pt-PT, remove PII, deduplica em dois passes (exato e por similaridade de embeddings), rotula uma amostra-semente com agentes e destila esses rótulos num classificador que cobre o resto. O resultado é um SQLite com busca insensível a acento, do qual se montam coleções e se exporta JSONL/CSV com **licença e atribuição por linha**.

## Quickstart

```powershell
& "$env:USERPROFILE\.local\bin\uv.exe" sync          # 1. cria o .venv a partir do uv.lock
& "$env:USERPROFILE\.local\bin\uv.exe" run pf --help # 2. lista os comandos da pipeline
& "$env:USERPROFILE\.local\bin\uv.exe" run pytest -q # 3. roda os testes
```

> `uv` ainda não está no PATH desta máquina — daí o caminho completo. O `pyproject.toml` já traz `[tool.uv] system-certs = true`, necessário onde o TLS é interceptado por proxy/antivírus. Ver `CLAUDE.md` para as demais pegadinhas.

## Pipeline

```
config/sources.toml
        │
        ▼
   pf ingest ──────────────►  data/raw/{fonte}.parquet
                                     │
        ┌────────────────────────────┴────────────────────────────┐
        │  pf run   (Parquet imutável a cada estágio, retomável)   │
        │                                                          │
        │  s01 normalize → s02 idioma+variante → s03 PII           │
        │      → s04 dedup exato → s05 embed (e5-small, .npy f16)  │
        │      → s06 dedup próximo                                 │
        └────────────────────────────┬────────────────────────────┘
                                     ▼
                        data/final/universe.parquet
                                     │
   pf make-seed ─► s07 amostra-semente (12k)
                                     │
        [skill rotular-prompts: agentes rotulam em lotes de 80]
                                     │
   pf merge-labels ─► s08     pf train ─► s09     pf apply ─► s10
                                     │
   pf load-db ─► s11  ──────►  data/db/prompts.sqlite  (WAL + FTS5 sem acento)
                                     │
                    ┌────────────────┴────────────────┐
                    ▼                                 ▼
        pf export ─► s12                        pf serve
   data/exports/*.jsonl|csv + manifest     http://127.0.0.1:8765
```

## Fontes

| Fonte | Dataset | Idioma | Licença | Comercial | Redistribuível |
|---|---|---|---|---|---|
| `wildchat_pt` | `allenai/WildChat-4.8M` | pt | ODC-BY-1.0 | sim | sim |
| `aya` | `CohereLabs/aya_dataset` | pt | Apache-2.0 | sim | sim |
| `oasst` | `OpenAssistant/oasst1`+`oasst2` | pt | Apache-2.0 | sim | sim |
| `arena140k` | `lmarena-ai/arena-human-preference-140k` | pt | CC-BY-4.0 | sim | sim |
| `wildchat_en` | `allenai/WildChat-4.8M` | en | ODC-BY-1.0 | sim | sim |
| `no_robots` | `HuggingFaceH4/no_robots` | en | CC-BY-NC-4.0 | **não** | sim |
| `dolly` | `databricks/databricks-dolly-15k` | en | CC-BY-SA-3.0 | sim | sim |
| `hh_rlhf` | `Anthropic/hh-rlhf` (só *helpful*) | en | MIT | sim | sim |
| `prism` | `HannahRoseKirk/prism-alignment` | en | CC-BY-4.0 | sim | sim |
| `lmsys` *(desligada)* | `lmsys/lmsys-chat-1m` | pt | LMSYS-1M | **não** | **não** |

Atribuições por extenso e intervalos esperados de contagem: `config/sources.toml`. O export exclui `redistributable = false` por padrão e contabiliza as fontes não comerciais à parte.

## Estado

**M0 — scaffold.** Os subcomandos da CLI existem e documentam a forma final, mas ainda são stubs (saem com código 2). Roteiro dos marcos M1–M10 no plano do projeto; convenções e pegadinhas em `CLAUDE.md`.
