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

Subcomandos e em que marco cada um sai do stub: `ingest` (M2 fontes pequenas / M3 WildChat), `run` s01–s06 (M4), `report raw` (M2) e `report universe`/`report dedup-sample` (M4), `db-check` (M1), `make-seed` (M5), `labels` (M5), `merge-labels` (M5), `train` (M7), `apply` (M7), `load-db` (M8), `export` (M9), `serve` (M9). Enquanto é stub, o comando imprime o aviso e sai com **código 2** — isso é esperado, não é bug.

```powershell
& "$env:USERPROFILE\.local\bin\uv.exe" run pf run                 # s01..s06 (= `all`)
& "$env:USERPROFILE\.local\bin\uv.exe" run pf run s05             # so um estagio
& "$env:USERPROFILE\.local\bin\uv.exe" run pf run s04-s06         # intervalo
& "$env:USERPROFILE\.local\bin\uv.exe" run pf report universe     # distribuicoes do universo
& "$env:USERPROFILE\.local\bin\uv.exe" run pf report dedup-sample # 50 pares + data/final/dedup_sample_50.txt
pwsh -File scripts/smoke_test.ps1                                    # pipeline inteira em miniatura, em tmp
pwsh -File scripts/run_pipeline.ps1                                  # pipeline real + os dois relatorios
```

```powershell
& "$env:USERPROFILE\.local\bin\uv.exe" run pf ingest                    # todas as fontes default_on
& "$env:USERPROFILE\.local\bin\uv.exe" run pf ingest aya --max-rows 200 # smoke de uma fonte
& "$env:USERPROFILE\.local\bin\uv.exe" run pf report raw                # confere data/raw/*.parquet

# WildChat (M3): dois passes de 1-3 h cada, retomáveis.
& "$env:USERPROFILE\.local\bin\uv.exe" run pf ingest wildchat-pt              # retomar é o PADRÃO
& "$env:USERPROFILE\.local\bin\uv.exe" run pf ingest wildchat-en --restart    # descarta checkpoint e recomeça
& "$env:USERPROFILE\.local\bin\uv.exe" run pf ingest wildchat-en --downsample # pool -> 100.000, sem rede
& "$env:USERPROFILE\.local\bin\uv.exe" run pf ingest lmsys                    # sai 2: fonte gated, desligada
```

## Pegadinhas (leia antes de mexer)

- **`uv` fora do PATH.** Foi instalado agora em `C:\Users\gigio\.local\bin\uv.exe` e a sessão atual não o enxerga. Sempre `& "$env:USERPROFILE\.local\bin\uv.exe" ...` até abrir um terminal novo.
- **TLS interceptado nesta máquina.** `uv sync` puro falha com `invalid peer certificate: UnknownIssuer` ao buscar o PyPI: há um middlebox (antivírus/proxy) reassinando o tráfego, e a raiz dele só existe no armazenamento de certificados do Windows, não no bundle embutido do uv. A correção é `system-certs = true` em `[tool.uv]` no `pyproject.toml` (já commitado) — equivale a `uv sync --system-certs` e faz o uv confiar no trust store do SO. (Em uv < 0.12 a chave chamava `native-tls`; usá-la no uv 0.12 gera warning de depreciação em *todo* comando.) **Não** use `--allow-insecure-host`: isso desliga a verificação de verdade.
- **TLS interceptado atinge o Python também.** O `system-certs` resolveu o `uv`, mas `requests`/`huggingface_hub` apontam para o bundle do `certifi`, que não conhece a raiz do middlebox: todo download do Hub morria em `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`. O `cli._bootstrap_env()` chama `certs.ensure_ca_bundle()`, que gera `data/system-ca.pem` (certifi + raízes de servidor do store do Windows, via `ssl.enum_certificates`) e exporta `REQUESTS_CA_BUNDLE` + `SSL_CERT_FILE`. **A verificação continua ligada** — nunca troque isso por `verify=False` nem por `HF_HUB_DISABLE_SSL_VERIFICATION`. O bundle é regenerado sozinho a cada 7 dias e pode ser apagado. Script solto (fora do `pf`) não passa pelo bootstrap: exporte `REQUESTS_CA_BUNDLE` na mão antes de rodar.
- **`HF_HOME` do ambiente vence o `settings.toml`.** O bootstrap usa `os.environ.setdefault`, e esta máquina já tem `HF_HOME=F:\hf_cache` no ambiente do usuário — é para lá que o cache vai, não para o `G:/hf-cache` do `settings.toml`. Confira com `echo $env:HF_HOME` antes de culpar o disco errado.
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

`src/prompt_factory/schema.py` (contrato canônico das 27 colunas) · `ingest/base.py` (contrato das 12 colunas do raw + `write_raw`) · `stages/__init__.py` (`StageConfig` + `STAGES`, único ponto de contato do `cli.py` com a pipeline) · `db.py` (DDL/FTS/conexão) · `cli.py` (entrypoint) · `labeling/taxonomy.json` (fonte única da taxonomia) · `config/sources.toml` (licença e atribuição por fonte — **a ordem das seções é o desempate do dedup**) · `.claude/skills/rotular-prompts/SKILL.md`.

## Preparo do universo (M4)

`pf run` encadeia os seis estágios; cada um lê e escreve Parquet imutável e é idempotente. `--data-dir` redireciona a árvore inteira (é assim que o smoke roda sem tocar em `data/`); `--max-rows` só é aplicado pelo **s01**, por fonte.

| estágio | entrada → saída | o que decide |
| --- | --- | --- |
| s01 | `raw/*.parquet` → `interim/normalized.parquet` | cadeia canônica, dedup de `uid` intra-execução, `lang` provisório |
| s02 | → `interim/lang.parquet` | idioma em 2 camadas + variante pt-BR/pt-PT |
| s03 | → `interim/scrubbed.parquet` | PII (e **recalcula** `hash_norm`) |
| s04 | → `interim/dedup1.parquet` + `dedup_exact_map.parquet` | dedup exato por `hash_norm` |
| s05 | → `emb/embeddings.f16.npy` + `emb/uids.txt` | embeddings e5-small |
| s06 | → `final/universe.parquet` + `final/dedup_near_map.parquet` + `emb/universe.f16.npy` | dedup próximo |

Pegadinhas deste bloco:

- **`lang` do s01 é provisório.** Só `"pt"`/`"en"` EXATOS da fonte são copiados; `"Portuguese"`, `"English"` e `"pt-BR"` viram `"und"` e o s02 decide pelos detectores. É de propósito: as fontes que rotulam por extenso são justamente as que erram (o mesmo template francês aparece 7.553x como `English` e 2.049x como `Portuguese`).
- **`wildchat_en_pool.parquet` não é fonte.** O s01 ignora qualquer `*_pool.parquet` em `raw/` — entrar com ele duplicaria 158 mil linhas. Qualquer OUTRO stem fora do `sources.toml` é erro fatal.
- **Chave vazia não agrupa no s04.** `norm_for_hash("!!!") == ""`, então todo prompt só de pontuação compartilha o `hash_norm` do sha256 da string vazia; o s04 deixa essas linhas passarem inteiras.
- **Invariante posicional dos `.npy`.** `emb/embeddings.f16.npy` está alinhado ao `dedup1.parquet`; `emb/universe.f16.npy` ao `universe.parquet`. São pares diferentes de propósito — o s06 muda as posições. O s06 confere o primeiro par (forma + uids) com assert antes de qualquer conta.
- **O s05 é retomável.** Memmap pré-alocado em `.tmp` + sidecar `emb/progress.json`; Ctrl+C perde no máximo um bloco de `[embed] chunk_rows`. Se o corpus ou o modelo mudarem, o sidecar é descartado e o passe recomeça. Ele é o estágio LONGO: ~16 textos/s em CPU para os textos longos do WildChat (o gargalo é o forward de 512 tokens, não a tokenização) — conte horas, não minutos.
- **A tabela canônica nunca passa por pandas.** `quality` é int8 *nullable* e viraria float64 (3 → 3.0 no export). O s04 usa pandas só para ordenar colunas de texto e ranks inteiros; a filtragem é máscara posicional em pyarrow.
- **O prefixo `query: ` do e5 é obrigatório** e mora em `embedder.embed_texts` — o repo do modelo não publica prompts nomeados, então `prompt_name="query"` NÃO funciona; é `prompt="query: "`. Sem o prefixo os vetores continuam saindo, plausíveis e errados.
- **`fast-langdetect` trunca em 80 caracteres por padrão** (`LangDetectConfig(max_input_length=80)`). O wrapper passa `None` e fatia em `[langid] max_input_chars`. Se o download do modelo `full` falhar, ele cai sozinho para o `lite` embutido no wheel e avisa alto.
- **`lingua` 2.2.0 não tem GALEGO**; o árbitro usa CATALÃO no lugar.
- **Aceitar os limiares do near-dup é decisão humana.** `pf report dedup-sample` grava `data/final/dedup_sample_50.txt` justamente para isso.

## Camada raw (M2/M3)

`data/raw/<fonte>.parquet` tem **12 colunas** (`ingest/base.RAW_SCHEMA`), não as 27 de `schema.py`: é camada de auditoria, com `text_raw` EXATO da fonte e **nenhuma** normalização — a cadeia canônica (`norm_display` → `hash_norm` → `uid`) começa no s01. Sem nulos: string ausente é `""`, bool ausente é `False`. Reingerir é seguro: raw é regenerável e a escrita é atômica.

Há **dois contratos de ingester** (`ingest/__init__.py` escolhe pelo que o módulo expõe):

* `iter_rows(cfg, max_rows)` — as 7 fontes pequenas do M2. Quem grava (e carimba a licença do `sources.toml`) é o `write_raw`, com `.tmp` + `os.replace`. Ele **materializa tudo em RAM**: serve até ~16k linhas por fonte, não mais.
* `run_ingest(nome, cfg, args) -> int` — quem controla a própria escrita. Só o `wildchat` usa: 3,2M de linhas em streaming não cabem no `write_raw`.

### WildChat (`ingest/wildchat.py`)

Um passe = varrer as **3.199.860** linhas do split `train` com `columns=` (pushdown de banda: ~9–11,5 GB em vez de ~15,3 GB), filtrando idioma **no cliente**. A cada **50.000 linhas varridas**: grava um part-file em `data/raw/_parts/<modo>/part_NNN.parquet` e **só depois** salva `data/raw/_checkpoints/<modo>.state.json` com o `ds.state_dict()`. A ordem part→state é obrigatória: invertida, um crash entre as duas escritas pularia 50k linhas nunca gravadas (perda silenciosa). O índice do part vem do **state**, nunca de `len(listdir())`. No fim, consolidação: dedup por `source_id` → ordena por `source_id` → parquet final.

* **Retomar é o padrão.** Rodar o mesmo comando continua de onde parou; `--resume` é só o alias explícito. `--restart` apaga checkpoint e parts. Mudar `--max-rows`, a fração ou as colunas invalida o checkpoint de propósito (o passe recusa e manda usar `--restart`).
* `--max-rows` conta linhas **VARRIDAS**, não mantidas (500 varridas ≈ 3 linhas pt, ≈ 31 no pool en).
* `wildchat_en` sai em dois arquivos: `wildchat_en_pool.parquet` (~200k, hash-gate de 11%) e, depois de `--downsample` (sem rede), `wildchat_en.parquet` com **100.000 exatos** + `wildchat_en.strata.txt`.
* Os parquets do wildchat **não gravam `pf_ingested_at`** no metadata: com um relógio no footer o downsample não seria byte-idêntico entre re-runs, que é como se prova o determinismo (`Get-FileHash`).
* `toxic` é sempre `False` neste release (as linhas tóxicas foram removidas antes da publicação): `nsfw_hint` constante `False` é o valor correto, não bug.

**Medido no M3** (revisão `c827c6df`, ~45 min por passe, ~64 checkpoints, 30–70 s entre eles): pt 3.199.860 varridas → 58.049 com `language=Portuguese` → **55.959** depois do dedup; en 183.300 no gate → **158.660** de pool → **100.000** no downsample (36 estratos, 9 levados inteiros).

* **`conversation_hash` NÃO é chave.** 1.520 hashes repetem no pt (2.090 linhas a mais) e 2.543 no pool en (24.640 a mais) — sempre com texto idêntico, porque o hash é do conteúdo. O dedup da consolidação é obrigatório, não paranoia.
* **A `language` da fonte erra.** O mesmo template automatizado em francês aparece 7.553× rotulado `English` e 2.049× rotulado `Portuguese`. No pt, ~34% das linhas são UM robô só ("Usando o seguinte texto:" sobre diários oficiais). O s02 não é opcional.
* **`HTTP_RANGE_LIMIT` (2 MiB) é obrigatório nesta máquina.** Sem ele o pyarrow coalesce column chunks em ranges de até 32 MiB, o middlebox de TLS corta a resposta em ~4 MB e o passe morre em 76% — sempre no mesmo shard, então os 5 retries do `huggingface_hub` refazem a mesma requisição gigante e falham igual. Acima disso, `wildchat._passe` reabre o stream sozinho a partir do último checkpoint (`MAX_TENTATIVAS_REDE`).
