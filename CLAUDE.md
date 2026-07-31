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

Subcomandos e em que marco cada um saiu do stub: `ingest` (M2 fontes pequenas / M3 WildChat), `run` s01–s06 (M4), `report raw` (M2) e `report universe`/`report dedup-sample` (M4), `db-check` (M1) e `db-check --bench` (M8), `make-seed`/`labels`/`merge-labels` (M5), `load-db` (M8), `serve`/`export` (M9). Ainda stub: `train` (M7) e `apply` (M7) — o comando imprime o aviso e sai com **código 2**, o que é esperado, não é bug.

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
# Campanha de rotulagem (M5). Detalhes em labeling/README.md e na skill.
& "$env:USERPROFILE\.local\bin\uv.exe" run pf make-seed                 # s07: semente + 155 lotes + manifest
& "$env:USERPROFILE\.local\bin\uv.exe" run pf labels status             # painel da campanha
& "$env:USERPROFILE\.local\bin\uv.exe" run pf labels next -n 4 --out-dir tmp   # reivindica 4 lotes
& "$env:USERPROFILE\.local\bin\uv.exe" run pf labels submit --batch batch_0007 --file r.jsonl --model haiku
& "$env:USERPROFILE\.local\bin\uv.exe" run pf labels gold --file pregold.jsonl # importa a calibração revisada
& "$env:USERPROFILE\.local\bin\uv.exe" run pf merge-labels              # s08: final/seed_labels.parquet
```

```powershell
# Carga do banco (M8). Constrói ao lado e troca por os.replace no fim.
& "$env:USERPROFILE\.local\bin\uv.exe" run pf load-db                        # s11: build + swap
& "$env:USERPROFILE\.local\bin\uv.exe" run pf load-db --allow-unlabeled-pct 100  # antes do M7: universo sem rótulo
& "$env:USERPROFILE\.local\bin\uv.exe" run pf load-db --no-swap              # constrói e PARA
& "$env:USERPROFILE\.local\bin\uv.exe" run pf load-db --swap-only            # retry do swap recusado por lock
& "$env:USERPROFILE\.local\bin\uv.exe" run pf db-check --bench               # mede o banco real (só leitura)
```

```powershell
# Interface e export (M9). A app só LÊ o SQLite — não roda pipeline nenhuma.
& "$env:USERPROFILE\.local\bin\uv.exe" run pf serve                          # http://127.0.0.1:8765 (contrato em /docs)
& "$env:USERPROFILE\.local\bin\uv.exe" run pf serve --port 8799 --db X.sqlite # outra porta, outro banco
& "$env:USERPROFILE\.local\bin\uv.exe" run pf export --format jsonl          # o universo inteiro + manifesto
& "$env:USERPROFILE\.local\bin\uv.exe" run pf export --collection "curadoria" --format csv
& "$env:USERPROFILE\.local\bin\uv.exe" run pf export --lang pt --commercial-only --name recorte-pt
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
4. s11 carrega o universo rotulado num **SQLite** (WAL + FTS5 `remove_diacritics 2`), construído à parte e trocado por swap de arquivo (`pf load-db`).
5. `app/`: FastAPI toca **apenas** o SQLite (mais os `.npy` por mmap na busca semântica do M10) e serve um `static/index.html` único, sem build.

## Arquivos críticos

`src/prompt_factory/schema.py` (contrato canônico das 27 colunas) · `ingest/base.py` (contrato das 12 colunas do raw + `write_raw`) · `stages/__init__.py` (`StageConfig` + `STAGES`/`CADEIA`, único ponto de contato do `cli.py` com a pipeline) · `labeling_io.py` (manifest atômico, máquina de estados dos lotes e validação estrita) · `db.py` (DDL/FTS/`INDEXES`/conexão — o `executescript(DDL)` idempotente é o pivô da carga bulk) · `cli.py` (entrypoint) · `labeling/taxonomy.json` (fonte única da taxonomia) · `labeling/mappings/*.json` (categoria nativa → task_type) · `config/sources.toml` (licença e atribuição por fonte — **a ordem das seções é o desempate do dedup**; também a **única** fonte da `attribution` do export) · `app/queries.py` (TODO o SQL de leitura da interface: sanitizador do FTS, WHERE, ORDER BY, facetas) · `app/models.py` (o contrato de entrada da API) · `export.py` (REGISTRY de formatos + manifesto) · `.claude/skills/rotular-prompts/SKILL.md`.

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

## Campanha de rotulagem (M5)

`pf make-seed` (s07) sorteia **12.000** itens do universo (6k pt + 6k en), estratificados por **fonte × faixa de n_chars**, e já fatia a semente em lotes: `labeling/seed/seed.parquet` + `strata.txt`, `labeling/batches/batch_NNNN.json` e o `labeling/manifest.json`. `pf labels` opera a campanha; `pf merge-labels` (s08) funde tudo em `data/final/seed_labels.parquet`. O protocolo dos agentes está em `.claude/skills/rotular-prompts/SKILL.md`.

| estágio | entrada → saída | o que decide |
| --- | --- | --- |
| s07 | `final/universe.parquet` → `labeling/seed/*` + `batches/*` + `manifest.json` | cotas por fonte, faixas de tamanho, ouro e lotes |
| s08 | `labeling/labels/*.jsonl` + `manifest.gold` + `native_category` → `final/seed_labels.parquet` | precedência manual > agent > native |

Pegadinhas deste bloco:

- **`quality` vai de 1 a 3, não a 1–5.** É o que `labeling/taxonomy.json` define e o que `schema.QUALITY_VALUES` valida (1 ruim, 2 usável, 3 bom). A validação recusa qualquer outro valor.
- **`bool` é subclasse de `int` em Python**, então `"quality": true` passaria por `isinstance(q, int)` e viraria 1 em silêncio. `labeling_io` testa `isinstance(q, bool)` ANTES do teste de inteiro — nunca inverta essa ordem.
- **O s07 e o s08 estão em `STAGES`, mas fora do `pf run all`** (`stages.CADEIA` é quem define o `all`). Entre os dois existe uma campanha de horas com revisão humana: se o `all` os incluísse, um `pf run` de rotina regeraria a semente e apagaria o manifest de uma campanha viva. Por isso `pf make-seed` também **recusa** rodar sobre uma campanha com trabalho feito, a menos que venha `--force`.
- **Refazer a semente invalida os rótulos**, não só o arquivo: uids novos = lotes novos = rótulos apontando para itens que não estão mais em lote nenhum.
- **A calibração sai de DENTRO dos 12.000**, não ao lado: 100 itens viram o `batch_0000`, o humano revisa, e os 11.900 restantes viram 155 lotes de 77 novos + 3 ouros escondidos. Com as cotas reais isso dá exatamente **155 lotes** (154×77 + 42).
- **Os 3 ouros de cada lote não têm marca nenhuma no arquivo** entregue ao agente (só `uid`/`lang`/`text`, como todo item); quem sabe quais são é o `manifest.gold_uids`. Marcá-los mediria o cuidado do agente com 3 itens, não a qualidade do lote.
- **Agreement é medido em `task_type` e `domain` dos 3 ouros = 6 comparações**, granularidade 1/6. `[labeling] agreement_min = 0.80` portanto exige **5 acertos em 6**. `quality` e `nsfw` ficam fora de propósito: a fronteira deles é legitimamente borrada entre anotadores.
- **`None` e `0.0` de agreement são coisas diferentes.** Sem ouro importado o agreement é `None` ("não medido"); confundir com 0.0 reprovaria a campanha inteira antes da calibração. Lote devolvido pelo portão guarda a nota que o reprovou, mas **sai da média** (o trabalho dele foi descartado).
- **Falha de validação NÃO muda o estado.** O lote segue `claimed`, a CLI lista todos os erros e imprime uma linha parseável `RETRY_UIDS: uid1,uid2,...` — é ela que dirige o retry.
- **O parser de resposta é tolerante na FORMA e estrito no CONTEÚDO**: cerca de código, prosa em volta, BOM, CRLF e eco do input são descartados com aviso; uid faltando/inventado/repetido e valor fora do enum são erro.
- **`oasst` tem 286 linhas e cota 600**: a redistribuição de déficit é caminho normal, não exceção. Medido com as cotas reais: wildchat_pt 3.809 (+209), aya 1.270 (+70), arena140k 635 (+35), oasst 286 — e cada cessão aparece no `strata.txt`.
- **`Generation` do no_robots está mapeada para `null`** (4.560 linhas, 46% da fonte): a fonte usa uma categoria só para "gerar texto" e a taxonomia divide isso em `geracao-criativa` e `redacao-pratica`. Chutar um dos lados injetaria milhares de rótulos errados justamente nas duas classes que mais se confundem.
- **Rótulo de agente sobre item de calibração é descartado no s08** — cada ouro reaparece em ~4,7 lotes e serve para medir, não para rotular.
- **`labeling/labels/`, `labeling/seed/` e `labeling/batches/` são gitignorados**; versionados são `taxonomy.json`, `mappings/*.json` e o `manifest.json`.
- **A saída de ferramenta do harness trunca em ~30k caracteres**, e um lote de 80 itens chega a ~120k. Por isso `pf labels next` tem `--out`/`--out-dir`: o fluxo real é sempre por arquivo.

## Carga do banco (M8)

`pf load-db` (s11) constrói `data/db/prompts.build.sqlite` **do zero** a partir de `final/universe.parquet` (obrigatório) + `final/labeled.parquet` (s10, opcional) + `final/seed_labels.parquet` (s08, opcional), e no fim troca o `data/db/prompts.sqlite` com um `os.replace`. `pf db-check --bench` mede o banco resultante em somente leitura.

| estágio | entrada → saída | o que decide |
| --- | --- | --- |
| s11 | `final/universe.parquet` + rótulos → `db/prompts.sqlite` | precedência dos rótulos, needs_review, swap |

Pegadinhas deste bloco:

- **O s11 está em `STAGES` mas FORA da `CADEIA`**, como o s07/s08 e por um motivo pior: é o único estágio cuja saída alguém está LENDO enquanto a pipeline roda (`pf serve`), e a última coisa que ele faz é trocar esse arquivo. Um `pf run` de rotina publicaria um banco com os rótulos que estivessem por ali — inclusive nenhum. `pf run s11` funciona; `pf run all` nunca o inclui.
- **Escalar numpy no sqlite3 vira BLOB em silêncio.** O sqlite3 do 3.12 aceita o buffer protocol: `np.int32(123)` grava `typeof='blob'`. As colunas com CHECK explodem na hora, mas `n_chars`, `n_words`, `variant_confidence`, `label_confidence`, `n_exact_dups` e `n_near_dups` **não têm CHECK e passam podres** — e um BLOB compara MAIOR que qualquer INTEGER, então `n_chars > 100` continua devolvendo TRUE e `sum(n_chars)` ignora a linha. Toda linha sai de `RecordBatch.to_pydict()`, nunca de iterar um `ndarray` ou uma coluna pyarrow. A guarda é `s11.SQL_TIPOS`.
- **`SELECT count(*) FROM prompts_fts` NÃO detecta índice vazio.** Numa tabela de conteúdo externo o count lê a tabela de conteúdo: medido, ele devolveu 3.000 depois de um `'delete-all'` esvaziar o índice. Quem detecta é `INSERT INTO prompts_fts(prompts_fts, rank) VALUES('integrity-check', 1)` — **com o argumento 1**; sem ele (rank 0) passa numa base vazia, porque só valida coerência interna.
- **O `integrity-check` exige conexão de ESCRITA.** Os comandos do FTS5 viajam como INSERT na tabela virtual; num `mode=ro` a resposta é `attempt to write a readonly database` antes de verificar coisa alguma. Por isso `s11.verificar()` abre read-write (ele não altera dado).
- **Conexão só-leitura deixa `-shm` órfão.** O SQLite remove `-wal`/`-shm` quando a última conexão fecha, mas só se ela puder escrever. Como um `-shm` ao lado do banco é justamente o sinal que o pré-voo do swap usa para dizer "alguém está com isto aberto", todo caminho readonly do s11 chama `_limpar_sidecars()` no fim.
- **Carga bulk = sem índice e sem trigger.** Popular o FTS pelo trigger, linha a linha, é ordens de grandeza mais lento; e o comando `'delete'` de um FTS externo exige que `old.text` seja EXATAMENTE o texto indexado — divergir **corrompe em silêncio**. A ordem é: derruba 3 triggers + 11 índices → `BEGIN` explícito (a conexão é autocommit!) → executemany → COMMIT → `'rebuild'` do FTS → `conn.executescript(db.DDL)`, que recria índices e triggers de uma vez porque todo o DDL é `IF NOT EXISTS`.
- **`text_original` é NULL até alguém editar.** A carga grava NULL em 100% das linhas (~800 MB a menos); quem lê usa `COALESCE(text_original, text)` e o `edited` já diz o estado. Copiar `text` ali seria duplicar o corpus para dizer "nada mudou".
- **`idx_prompts_facets` existe porque o SQLite não combina dois índices de igualdade.** Com só os simples, `WHERE lang=? AND task_type=? AND domain=?` escolhe UM e varre o resto. Medido em 180k linhas: contagem por faceta 89,7 → 13,2 ms (6,8x), filtro triplo 11,6 → 0,16 ms (70x), e os planos passam a dizer `USING COVERING INDEX`.
- **Rótulo observado vence rótulo inferido.** `labeled.parquet` cobre todo mundo; por cima, `seed_labels` com `label_method` `manual`/`agent` sobrescreve — o classificador TREINOU nessas linhas, deixá-lo reescrevê-las fecharia um laço em que o modelo audita a si mesmo. `native` do seed **não** vence a predição, mas entra quando não há predição nenhuma. Rótulo humano grava `label_confidence` NULL: confiança de humano não é probabilidade, e um 1.0 inventado poluiria a fila de revisão ordenada por essa coluna.
- **`--allow-unlabeled-pct` (default 1) recusa a carga ANTES de construir**, com um pré-voo que lê só a coluna `uid` do universo. Sem isso, um banco carregado com o parquet de rótulos ausente fica plausível, abre na interface e só denuncia o erro semanas depois. Antes do M7 a falta de rótulo é intencional: `--allow-unlabeled-pct 100`.
- **Swap recusado sai com código 3**, não 1: o banco novo está PRONTO em `db/prompts.build.sqlite` e o conserto é parar o `pf serve` e rodar `pf load-db --swap-only`. O build **nunca** é apagado no erro — ele é o produto.
- **`db_build_id` é `sha256(universe_sha + labels_sha)[:16]`, não um uuid4.** O projeto inteiro se apoia em "mesmas entradas ⇒ mesmo resultado"; um id aleatório faria dois bancos idênticos parecerem diferentes. A carga NÃO é byte-idêntica entre re-runs (`ingested_at`/`updated_at` usam `strftime('now')`) — o que se repete são as contagens e o build_id.

## Interface e export (M9)

`pf serve` sobe a app FastAPI (`src/prompt_factory/app/`) sobre `data/db/prompts.sqlite`; `pf export` faz o mesmo export da interface pela linha de comando. A app **não roda estágio nenhum**: ela lê o SQLite, edita texto/rótulo, monta coleções e escreve `data/exports/`. O contrato completo e sempre atual está em `http://127.0.0.1:8765/docs` (OpenAPI).

| módulo | o que decide |
| --- | --- |
| `app/models.py` | o contrato de ENTRADA: `Filtros` (usado em 4 lugares), `ConsultaPrompts`, corpos de PATCH/coleção/export |
| `app/queries.py` | TODO o SQL de leitura: `sanitize_fts`, `build_where`, `ORDER_BY`, facetas, snippets |
| `app/deps.py` | a conexão por request + a regra dura sobre `async def` |
| `app/presenters.py` | linha do SQLite → JSON (bool de verdade, `license_class`, `attribution`) |
| `app/main.py` | `criar_app()` (fábrica), lifespan, `idx_prompts_app`, mount do `static/` |
| `export.py` | `REGISTRY` perfil×container, escritores JSONL/CSV, manifesto |

Pegadinhas deste bloco:

- **Nenhuma rota que toca o banco pode ser `async def`.** Medido: rota `def` + `get_conn` rodam na MESMA thread e funcionam com o `check_same_thread=True` padrão; trocar para `async def` põe a rota no event loop e a dependência no threadpool, e a primeira query morre com `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread`. O conserto é **tirar o `async`**, nunca desligar o `check_same_thread` (isso troca um erro alto por corrupção silenciosa). `db.connect` ganhou o parâmetro só para o caso do gerador em threadpool — não use em rota.
- **`nsfw = 0` perde as linhas NULL em silêncio.** Não rotulado é o estado normal (a campanha do M6/M7 não rodou: hoje é 100% do banco). Todo booleano anulável usa `IS 1` / `IS NOT 1`; o `nsfw=exclude` da API mantém os não rotulados de propósito. `quality_min`, esse sim, EXCLUI os sem rótulo — é o desejado, mas a interface tem de dizer.
- **`MATERIALIZED` no CTE do FTS é 100x mais lento** (9,5 → 956 ms em 200k). A palavra não pode aparecer em `queries.py`; o SQLite inlina sozinho.
- **`snippet()` do FTS5 custa o corpus se ficar dentro do CTE, e 2,3 ms se ficar fora.** Medido em 120k linhas com um termo de 97.541 hits: dentro do CTE 384 ms, restrito aos 50 rowids da página **2,3 ms**. Por isso o snippet sai numa SEGUNDA consulta (`sql_snippets`), depois de a página estar decidida. É o que torna viável devolver o trecho com o termo em vez do começo do texto — num prompt de 1 MB os dois não têm relação.
- **`sort=random` precisa de DOIS módulos.** `(id * seed) % 1000003` **não embaralha**: com ids pequenos o produto nunca passa do módulo e a ordem sai idêntica à de `id`. A fórmula em uso é `(((id * 2654435761 + seed * 40503) % 4294967291) * 279470273 % 1000003)`. O teste `test_random_embaralha_de_verdade` existe exatamente para pegar a regressão.
- **Toda ordenação termina em `p.id`.** Sem o desempate, `n_chars DESC` com empates devolve a mesma linha em duas páginas e some com outra, sem aviso.
- **O Pydantic coage `true` para `1`.** `"quality": true` passaria por um validador comum já convertido e viraria "qualidade ruim" em silêncio — a checagem de `bool` tem de ser `mode="before"`. Mesma pegadinha do `labeling_io`, com um agravante: lá bastava testar `isinstance` na ordem certa, aqui a coerção acontece antes de qualquer validador normal.
- **`extra="forbid"` em todo modelo.** `?task_types=codigo` (plural) devolve 422 em vez de ignorar o parâmetro e entregar o corpus inteiro como se fosse o recorte pedido.
- **Cada faceta é contada com o filtro DELA MESMA removido.** Consequência que confunde quem lê os números: uma dimensão **não filtrada** soma exatamente `total`; uma dimensão **filtrada** soma mais que `total`. Sem isso, marcar "pt" zeraria "en" e o usuário ficaria preso no primeiro clique.
- **`task_type`/`domain` saem na ordem da taxonomia e COM os zeros.** Enquanto `rotulagem_pendente` for `true` todas são zero; a interface desabilita o grupo com explicação em vez de escondê-lo (sumir parece bug).
- **`idx_prompts_app` é criado pelo lifespan, não pelo DDL.** É índice de consulta da app (15 colunas, ~14 MB em 200k), e o `pf load-db` derruba/recria só os índices que ele conhece — um banco recém-carregado simplesmente não o tem, e a app o recria em ~0,6 s.
- **`attribution` NÃO é coluna do banco.** Vem de `config/sources.toml` e é resolvida num dict antes do laço do export. Sem ela o export **não cumpre a licença**: ODC-BY, CC-BY e CC-BY-SA exigem crédito a cada uso.
- **`redistributable = 0` fica fora do export por padrão** e o manifesto conta quantas saíram. `include_nonredistributable=true` é permitido (uso local) e carimba um `WARNING` no manifesto. `commercial_ok = 0` **não** é excluído, só contabilizado — a licença não comercial não impede o uso, impede *um certo* uso.
- **`exports.format` tem CHECK `IN ('jsonl','csv')`.** Por isso o `REGISTRY` separa `profile` de `container`: um perfil Label Studio entra como `profile="label-studio"` gravando `format="jsonl"`, sem migração. O perfil real fica em `manifest_json.profile`.
- **O módulo `csv` do Python recusa campo acima de 131.072 caracteres** (`_csv.Error: field larger than field limit`) e este corpus tem prompts de ~1 milhão. O arquivo exportado está CORRETO — quem precisa de ajuste é o leitor (`csv.field_size_limit(10**7)`). O manifesto carimba `NOTA_CSV` quando isso vai acontecer, senão o usuário conclui que o export saiu corrompido. (O Excel corta em 32.767 por célula: para texto longo, JSONL.)
- **O export escreve em disco e SÓ ENTÃO serve** (`POST /api/export` devolve o manifesto, `GET /api/exports/{id}/download` baixa). Streamar a resposta obrigaria a mandar o corpo antes de saber `row_count`/`sha256`, e faria a conexão atravessar o threadpool do Starlette — o mesmo `ProgrammingError` da primeira pegadinha.
- **`pf serve` recusa subir sem banco**, com a linha de comando do conserto na tela. Servidor que sobe e dá 500 em tudo enterra a mensagem útil num traceback por request.
- **Um worker, sempre.** Com N workers cada processo carregaria a própria matriz de embeddings do M10 (~293 MB) e o próprio modelo e5, para servir um usuário numa máquina só.
- **O lifespan faz `wal_checkpoint(TRUNCATE)` ao sair.** Sem isso um Ctrl+C deixa `-shm` ao lado do banco, e um `-shm` órfão é exatamente o sinal que o pré-voo do swap do `pf load-db` lê como "alguém está com isto aberto" — o próximo `load-db` seria recusado sem motivo.
- **O mount do `StaticFiles` vem POR ÚLTIMO.** Registrado antes dos routers, um mount em `/` engole `/api/*`.
- **A app é uma FÁBRICA (`criar_app(db_file, exports_dir)`), não um `app` global.** Um `app = FastAPI()` de módulo leria `paths.DB_FILE` no import e tornaria impossível apontar teste (ou `--db`) para outro arquivo. `--reload` do uvicorn usa `factory=True`.
- **`<mark>` do snippet viaja como texto no JSON.** A interface **tem** de escapar o resto do conteúdo antes de injetar como HTML: o corpus tem `<script>` de verdade dentro dos prompts.

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
