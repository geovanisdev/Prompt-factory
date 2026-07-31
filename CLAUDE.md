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

Subcomandos e em que marco cada um saiu do stub: `ingest` (M2 fontes pequenas / M3 WildChat), `run` s01–s06 (M4), `report raw` (M2) e `report universe`/`report dedup-sample` (M4), `db-check` (M1) e `db-check --bench` (M8), `make-seed`/`labels`/`merge-labels` (M5), `load-db` (M8), `serve`/`export` (M9), busca semântica dentro do `serve` (M10). Ainda stub: `train` (M7) e `apply` (M7) — o comando imprime o aviso e sai com **código 2**, o que é esperado, não é bug.

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
# Interface, busca semântica e export (M9/M10). A app só LÊ — não roda pipeline nenhuma.
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
5. `app/`: FastAPI toca **apenas** o SQLite (mais `emb/universe.f16.npy`, convertido para f32 residente na busca semântica do M10) e serve um `static/index.html` único, sem build.

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
| s06 | → `final/universe.parquet` + `final/dedup_near_map.parquet` + `emb/universe.f16.npy` | recheck de idioma + dedup próximo validado par a par |

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
- **Union-find é transitivo, e isso descartava linha boa.** Confirmado A~B e B~C, o A e o C caem no mesmo componente **sem nunca terem sido comparados**. Medido na primeira execução do s06: dos 44.333 descartes, **27% (11.964) tinham cosseno < 0,985 contra o canônico do próprio grupo** e 13,4% tinham Jaccard < 0,65. `[dedup] near_pairwise = true` reconfere cada membro **contra o canônico** nos mesmos três critérios; quem falha permanece no universo. `false` reproduz o comportamento antigo — os dois modos são replayáveis.
- **O canônico não muda quando membros saem, e isso é invariante, não sorte.** `dedup.choose_canonical` é um `min` sobre o grupo e a validação só remove quem **não** é o canônico: o mínimo de um subconjunto que ainda contém o mínimo é o mesmo mínimo. É o que permite validar num passe só. **Não há recomputação em cascata** de propósito: dois membros poupados podem ser duplicatas entre si, mas reagrupá-los exigiria refazer o union-find até um ponto fixo com o canônico mudando no meio — preferimos deixar passar alguns pares próximos a descartar linha por transitividade.
- **O e5 corta em 512 tokens (~1.879 caracteres em pt, ~2.117 em en), e por isso cosseno 1,0000 não prova nada.** Dois textos com o mesmo cabeçalho longo dão vetores idênticos mesmo divergindo por completo depois do corte: houve um cluster de 2.088 linhas com 8.180 caracteres de prefixo comum onde uma pedia previsão sobre o Surface RT e outra um roteiro de Kyoto. O Jaccard é o critério que enxerga o texto inteiro — nunca deixe o cosseno decidir sozinho.
- **O idioma de um prompt é o da INSTRUÇÃO, não o do material colado embaixo dela.** 976 linhas do template francês `Goal / Corriger les erreurs de formatage...` chegaram ao universo — 839 como `pt` (318 delas classificadas pt-BR e 212 pt-PT!) e 137 como `en`. Os dois detectores do s02 estavam **certos**: são documentos bilíngues, ~200 caracteres de francês seguidos de milhares de caracteres de payload JSON em português ou inglês, e na janela de 1.000 caracteres do s02 o payload ganha por volume. fastText e lingua **concordaram** em pt/en — não houve discordância para o árbitro pegar.
- **O recheck de idioma do s06 usa uma janela MENOR que a do s02, não maior** (`[dedup] lang_recheck_head_chars = 250`). Medido: a cobertura da família francesa cai conforme a janela cresce — 250 → 2.061 de 2.061; 300 → 2.057; 400 → 1.943; 500 → 1.202. Quanto maior a janela, mais o payload afoga a instrução. Linha com `n_chars` menor que a janela **não** é reprocessada: o s02 já leu exatamente o mesmo texto.
- **O recheck mora no s06 e não no s02** porque refazer o s02 obrigaria a refazer as ~2 h de embedding do s05; no s06 os embeddings continuam válidos, porque remover linha não move as outras (a máscara é posicional). Ele roda **antes** do agrupamento, senão uma linha em francês poderia ser escolhida canônica e manter justamente a errada.
- **O recheck exige as DUAS camadas de acordo num idioma fora de {pt, en}.** Sozinho, o árbitro devolve CATALÃO para texto em russo (que não está no conjunto dele) e derrubaria linha boa com um rótulo inventado — foram 251 casos `ru→ca` poupados por essa guarda. Medido no corpus real: **2.215 linhas fora (2.177 fr + 38 es)**, com a família `Goal/Corriger` inteira dentro e **nenhum falso positivo** nas 154 restantes, todas prompts genuínos em francês ou espanhol.

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
- **Carga bulk = sem índice e sem trigger.** Popular o FTS pelo trigger, linha a linha, é ordens de grandeza mais lento; e o comando `'delete'` de um FTS externo exige que `old.text` seja EXATAMENTE o texto indexado — divergir **corrompe em silêncio**. A ordem é: derruba 3 triggers + 15 índices → `BEGIN` explícito (a conexão é autocommit!) → executemany → COMMIT → `'rebuild'` do FTS → `conn.executescript(db.DDL)`, que recria índices e triggers de uma vez porque todo o DDL é `IF NOT EXISTS`.
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
| `app/main.py` | `criar_app()` (fábrica), lifespan, `idx_prompts_app` + `idx_prompts_chatmark`, sonda da semântica, mount do `static/` |
| `app/semantic.py` | a matriz f32 residente, o casamento uid → id e as duas guardas de alinhamento (M10) |
| `app/cache.py` | cache em memória de `/api/facets` e `/api/stats`, com invalidação por escrita e por `db_build_id` |
| `app/static/index.html` | **a interface inteira**: um arquivo, CSS+JS inline, sem build e sem rede |
| `export.py` | `REGISTRY` perfil×container, escritores JSONL/CSV, manifesto |

Pegadinhas deste bloco:

- **A conexão por request nasce com `check_same_thread=False`, e isso não é preguiça.** O FastAPI resolve uma dependência-gerador com `contextmanager_in_threadpool`: o `__enter__`, o corpo da rota e o `__exit__` são **três `anyio.to_thread.run_sync` independentes**, sem afinidade de thread (o `__exit__` usa até um `CapacityLimiter` próprio). Com UMA requisição em voo o pool reaproveita a thread ociosa e tudo parece funcionar — foi assim que a medição original ("4 de 4 na mesma thread") passou. Medido depois, com as mesmas 24 requisições: 1 em paralelo → 0 erros; **4 em paralelo → 15 respostas 500; 12 em paralelo → 24**, todas com `sqlite3.ProgrammingError` vindo do `conn.close()`. E 2+ em voo é o caso normal: a interface dispara `health`+`stats`+`collections` juntas no primeiro paint. Isto NÃO é o caso proibido do `db.connect` (conexão compartilhada entre usuários): aqui ela nasce, é usada e morre dentro de uma requisição, uma operação por vez. Regressão coberta por `tests/test_frontend.py::test_conexao_do_request_atravessa_threads`.
- **Nenhuma rota que toca o banco pode ser `async def`** — agora por causa do bloqueio, não da thread: `sqlite3` é síncrono, e uma consulta de 200 ms no event loop trava o servidor inteiro (inclusive o download de export em curso).
- **`nsfw = 0` perde as linhas NULL em silêncio.** Não rotulado é o estado normal (a campanha do M6/M7 não rodou: hoje é 100% do banco). Todo booleano anulável usa `IS 1` / `IS NOT 1`; o `nsfw=exclude` da API mantém os não rotulados de propósito. `quality_min`, esse sim, EXCLUI os sem rótulo — é o desejado, mas a interface tem de dizer.
- **`MATERIALIZED` no CTE do FTS é 100x mais lento** (9,5 → 956 ms em 200k). A palavra não pode aparecer em `queries.py`; o SQLite inlina sozinho.
- **`snippet()` do FTS5 custa o corpus se ficar dentro do CTE, e 2,3 ms se ficar fora.** Medido em 120k linhas com um termo de 97.541 hits: dentro do CTE 384 ms, restrito aos 50 rowids da página **2,3 ms**. Por isso o snippet sai numa SEGUNDA consulta (`sql_snippets`), depois de a página estar decidida. É o que torna viável devolver o trecho com o termo em vez do começo do texto — num prompt de 1 MB os dois não têm relação.
- **`sort=random` precisa de DOIS módulos.** `(id * seed) % 1000003` **não embaralha**: com ids pequenos o produto nunca passa do módulo e a ordem sai idêntica à de `id`. A fórmula em uso é `(((id * 2654435761 + seed * 40503) % 4294967291) * 279470273 % 1000003)`. O teste `test_random_embaralha_de_verdade` existe exatamente para pegar a regressão.
- **Toda ordenação termina em `p.id`.** Sem o desempate, `n_chars DESC` com empates devolve a mesma linha em duas páginas e some com outra, sem aviso.
- **O Pydantic coage `true` para `1`.** `"quality": true` passaria por um validador comum já convertido e viraria "qualidade ruim" em silêncio — a checagem de `bool` tem de ser `mode="before"`. Mesma pegadinha do `labeling_io`, com um agravante: lá bastava testar `isinstance` na ordem certa, aqui a coerção acontece antes de qualquer validador normal.
- **`extra="forbid"` em todo modelo.** `?task_types=codigo` (plural) devolve 422 em vez de ignorar o parâmetro e entregar o corpus inteiro como se fosse o recorte pedido.
- **Cada faceta é contada com o filtro DELA MESMA removido.** Consequência que confunde quem lê os números: uma dimensão **não filtrada** soma exatamente `total`; uma dimensão **filtrada** soma mais que `total`. Sem isso, marcar "pt" zeraria "en" e o usuário ficaria preso no primeiro clique.
- **`task_type`/`domain` saem na ordem da taxonomia e COM os zeros.** Enquanto `rotulagem_pendente` for `true` todas são zero; a interface desabilita o grupo com explicação em vez de escondê-lo (sumir parece bug).
- **`idx_prompts_app` e `idx_prompts_chatmark` são criados pelo lifespan, não pelo DDL.** São índices de consulta da app, e o `pf load-db` derruba/recria só os índices que ele conhece — um banco recém-carregado simplesmente não os tem. O de expressão nem PODE estar no DDL: a janela dele vem do `settings.toml`. Os quatro de ORDENAÇÃO (`created`/`nchars`/`dups`/`labelm`), esses, estão em `db.DDL_INDICES_ORDENACAO` **e** são recriados pelo lifespan, porque o banco no disco hoje foi carregado antes de eles existirem.
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

### Performance da interface (M9-C)

Tudo aqui foi medido **contra o banco real** (`data/db/prompts.sqlite`: 144.754 linhas, 687 MB, `task_type`/`domain` 100% nulos), pelo servidor real, uma requisição por vez, com o arquivo já quente no cache do SO. `scripts/` não guarda o harness: ele é descartável, o que importa são os números e o porquê.

| medida | antes | 1ª chamada | repetida |
| --- | ---: | ---: | ---: |
| `/api/prompts` página 1, sem filtro | 520 ms | **40** | 18 |
| `/api/facets` sem filtro | 1.388 ms | **139** | 8 |
| busca `coracao` | 24 ms | 24 | 24 |
| busca `texto` (14.020 acertos) | 136 ms | 142 | 140 |
| `/api/facets` com `lang=pt` + busca | 924 ms | **114** | 8 |
| `sort=longest` | 569 ms | **46** | 43 |
| `sort=dups` | 199 ms | **18** | 18 |
| página 2000 (OFFSET 99.950) | 8.391 ms | **23** | 23 |
| `/api/stats` | 1.876 ms | **114** | 8 |
| **primeiro paint** (prompts+facets+stats) | **3.784 ms** | **292 ms** | **34 ms** |

O gargalo real **não era** o que a lentidão sugeria. Nenhuma dessas consultas era complexa: quase todas viravam **uma varredura da tabela de 687 MB**, que nesta máquina custa ~170 ms — e várias delas por resposta. Todos os números uniformemente próximos de 170, 340 ou 1.700 ms eram múltiplos de uma mesma varredura.

- **Varredura de índice NÃO-cobridora é pior que não ter índice.** Onze das doze facetas diziam `SCAN p USING INDEX idx_prompts_source` — o índice tinha a coluna do `GROUP BY`, mas não a do WHERE (`nsfw`, que o filtro padrão sempre aplica), então cada uma das 144 mil entradas virava uma busca na tabela. `SCAN … USING INDEX` sem a palavra **COVERING** no `EXPLAIN QUERY PLAN` é sinal de alarme, não de otimização.
- **`GROUP BY` no prefixo CONTÍGUO do índice dispensa a B-tree temporária.** As mesmas 11 dimensões, a mesma resposta: ordem arbitrária 202 ms, ordem do índice **com um buraco** 162 ms, prefixo contíguo **37 ms**. Por isso `queries.PREFIXO_FACETAS` carrega `commercial_ok` e `redistributable`, que não são faceta nenhuma: elas só tapam o buraco entre `edited` e `pii_found`. Tirá-las "porque não são usadas" multiplica o custo por 4,4 sem mudar um número na tela. A tupla e a ordem de `DDL_INDICE_APP` andam JUNTAS (`test_prefixo_das_facetas_e_prefixo_contiguo_do_indice_da_app`).
- **Doze `GROUP BY` viraram uma varredura + marginalização em Python.** Facetas que compartilham o mesmo WHERE saem da distribuição conjunta (82 grupos no corpus real) somada por dimensão. Quem decide é `particionar_facetas`, e `campos_ativos` deriva os campos ativos **por diferença sobre o próprio `build_where`** — uma segunda lista de "campos preenchidos" diria que sem filtro nada está ativo, juntaria a faceta `nsfw` com as outras e contaria o NSFW sob o filtro que exclui NSFW.
- **`nsfw="exclude"` é DEFAULT e mesmo assim produz condição.** É por isso que a faceta `nsfw` nunca divide a varredura das outras: sem filtro nenhum, 10 dimensões vão juntas e `nsfw` + `n_chars_bucket` saem sozinhas.
- **Materializar os acertos do FTS numa tabela TEMP é 7x mais rápido nas FACETAS — e continua 100x mais lento na LISTAGEM.** As duas coisas são verdade e não se contradizem. A listagem tem `LIMIT 50` e o CTE inline deixa o SQLite parar cedo. Uma faceta consome o conjunto inteiro e não devolve texto: com o CTE o plano é `SCAN prompts_fts` + `SEARCH p USING INTEGER PRIMARY KEY`, ou seja **14 mil linhas COMPLETAS** (com o `text` junto) carregadas só para contar; com `temp.pf_hits` o plano vira `SEARCH p USING COVERING INDEX idx_prompts_app` sondando a temporária pelo rowid, e a tabela não é tocada. Medido: 77 ms → 11 ms por consulta, 8,5 ms para montar a temporária uma vez. **`AS MATERIALIZED` dentro da consulta continua proibido** — o que funciona é a tabela TEMP separada, reaproveitada por várias consultas.
- **Índice de EXPRESSÃO resolveu o número mais caro do `/api/stats`.** `sum(substr(text,1,20000) LIKE '%User:%' …)` é a única conta do panorama que lê a coluna `text`, e sozinha custava 1.462 dos 1.877 ms. Com `idx_prompts_chatmark` ela vira varredura de cobertura de 5,5 ms, e o SQLite mantém o índice sozinho a cada UPDATE de `text`. **Duas armadilhas:** (1) o corte tem de ser **literal** no SQL — `:marc` parametrizado nunca casa com um índice de expressão; por isso `queries.expr_marcadores_chat()` é ponto único da verdade. (2) o casamento é por TEXTO da expressão, então mudar `[app] chat_markers_scan_chars` sem refazer o índice não dá erro nenhum: dá os 1.462 ms de volta em silêncio. `main._indice_marcadores` compara o SQL guardado no `sqlite_master` e refaz quando diverge.
- **Somar tudo numa consulta só pode ser mais caro que somar em duas.** O bloco de `sum()` do `/api/stats` junto com a soma dos marcadores custava 1.468 ms; separados, 37 + 5,5 ms. Juntas, a coluna `text` obriga o SQLite a largar o índice de cobertura e ler a tabela para conseguir TODAS as somas.
- **OFFSET profundo é o único lugar da app que passa por cima do planner.** Com um filtro de igualdade indexado o SQLite prefere buscar por esse índice e ordenar num TEMP B-TREE: certo na página 1 (`source=aya` = 19 ms), catastrófico na 2000 (`lang=pt` = 2.256 ms). Acima de `[app] deep_offset_hint` a listagem força `INDEXED BY idx_prompts_created` — só em `newest`/`oldest` e só **sem busca** (com o CTE do FTS o laço externo são os acertos, e forçar o índice inverteria a junção). A justificativa do limiar: um OFFSET de N só é alcançável se o filtro casar mais de N linhas, e ordenar N linhas nunca sai mais barato que caminhar N entradas já ordenadas.
- **`created_ts DESC, id DESC, nsfw, lang`: as duas últimas colunas não ordenam nada.** Elas existem para o OFFSET profundo poder DESCARTAR linha pelo índice. Só `(created_ts, id)` já leva a página 2000 de 8.145 para 497 ms; com o `nsfw` dentro, para 7,2 ms — a diferença são 99.950 buscas na tabela só para avaliar o filtro padrão.
- **O cache mente se a invalidação errar, e mentir é pior que ser lento.** `/api/facets` (por filtro) e `/api/stats` são cacheados em memória; **toda** escrita joga o cache inteiro fora. A interface re-consulta `/api/facets` a cada virada de página e depois de todo `PATCH`, então o cache é o que torna a curadoria fluida — e é também o que transformaria um número velho em "o corpus". Duas guardas: a geração interna (escrita pela app) e o `db_build_id` do `app_meta` lido a CADA leitura de cache (0,0 ms), que é o que detecta um `pf load-db` trocando o arquivo embaixo do servidor. **Não cobre** escrita por outro processo no mesmo arquivo — limitação registrada em teste, e a app já assumia ser a única escritora (o `n_rows` do `/api/health` é lido uma vez no lifespan).
- **`[app] cache_max_entradas = 0` desliga o cache.** É como medir o custo real de uma consulta sem mexer no código.
- **O lifespan ficou mais caro na PRIMEIRA subida depois de um `load-db`** (~9 s contra ~6 s): ele constrói os índices que faltam, sendo o de expressão o caro (~1,5 s). Nas subidas seguintes é `IF NOT EXISTS` e não custa nada. Os cinco índices novos somam ~10 MB num banco de 687 MB.

### A tela (`app/static/index.html`)

Um arquivo de ~1.970 linhas, **sem build, sem framework, sem CDN** — CSS e JS inline, e nenhuma referência a host externo (provado por `tests/test_frontend.py::test_zero_referencia_externa`). Layout de 3 colunas: facetas · lista · mesa (seleção, coleção ativa, export). Escuro por padrão, claro no botão `tema`; o cromo é sans e **o texto do prompt é serifado** — o conteúdo é o produto, não pode ter a mesma voz da interface.

- **Só `snippetSeguro()` produz HTML vindo do corpus.** Ela escapa tudo com `esc()` e reabre exatamente duas strings: `&lt;mark&gt;` e `&lt;/mark&gt;`. Todo o resto vai por `esc()` ou `textContent`, e o editor recebe o texto por `.value`. Verificado no navegador contra um prompt com `<script>alert(1)</script>`: 0 elementos `script`/`img` injetados na lista.
- **A taxonomia NÃO está copiada no HTML.** As 32 classes e seus nomes legíveis vêm de `/api/stats`. Teste `test_a_taxonomia_nao_foi_copiada_para_a_tela` recusa qualquer chave da taxonomia como literal no JS.
- **A seleção é um `Set` de uids no cliente**, persistido em `localStorage` junto com um mapa enxuto `uid → {license, license_class, redistributable, commercial_ok}` — é ele que permite calcular a composição de licença do export no modo "seleção" depois de um reload.
- **`license → license_class` não é regra copiada.** A tela pergunta ao backend: um `GET /api/prompts?license=X&page_size=1` por licença no boot, e lê o `license_class` que `presenters` calculou. Nunca reimplemente `classe_da_licenca` em JS.
- **"Adicionar os N do filtro" manda o objeto `est.f` inteiro para o `from-filter`** — o mesmo `Filtros` da listagem e das facetas. É o que garante que o número prometido na tela seja o número que entra na coleção.
- **Texto: 400 caracteres no card, 120.000 no máximo ao expandir.** O corpus tem prompts de ~1 MB; jogar isso no DOM trava a aba. Ao expandir, a tela busca `GET /api/prompts/{uid}` e mostra os primeiros 120k com um aviso honesto de quanto ficou de fora — `copiar` leva o texto inteiro.
- **`Shift+J/K` lê `e.shiftKey`, não a caixa da tecla.** Num teclado comum Shift+j chega como `key="J"`, mas layout/IME/remapeador podem mandar `key="j"` com `shiftKey=true`; testar só a maiúscula quebra em silêncio. Mesma ideia no espaço (`e.key === " " || e.code === "Space"`).
- **Qualidade é 1..3** (`schema.QUALITY_VALUES`), não 1..5. Vale para a folha de atalhos, para os botões da fila de revisão e para o seletor do editor.
- **`rotulagem_pendente` desabilita, nunca esconde.** Os grupos `task_type`/`domain` aparecem com as 16 classes zeradas e a explicação na barra lateral — sumir pareceria bug.
- **O editor manda só o que mudou** (`PATCH` com os campos alterados). Limpar um rótulo é `null` explícito; não mexer é omitir a chave — é o que `exclude_unset` distingue do lado do backend.
- **O seletor `texto | sentido` (M10) é COLADO à busca, não um segundo campo**: escolher o modo é escolher como *esta* busca funciona, não fazer outra. `est.modo` **não é persistido** — se o `.npy` sumir entre duas sessões, abrir direto num modo quebrado é pior que pedir um clique. O aviso do aquecimento é uma **faixa no fluxo** (`#aviso-semantica`), nunca o `recado()`: uma mensagem que some em 2 s é exatamente a que ninguém vê numa espera de 16 s.
- **A barra de similaridade é normalizada DENTRO da lista; o número do selo, não.** Entre o 1º e o 50º de uma busca boa há 0,99 e 0,86 — numa escala absoluta de 0 a 1 são cinquenta barras cheias idênticas. O selo mostra o cosseno de verdade (comparável entre buscas), a barra compara os itens desta lista.

## Busca semântica (M10)

`GET /api/semantic` devolve os **k mais próximos** de uma consulta em linguagem natural, dentro do **mesmo `Filtros`** da listagem. Módulo: `app/semantic.py` (`IndiceSemantico`, uma instância por app em `app.state.semantica`) + `app/routes_semantic.py`. Duas rotas de apoio existem por causa do custo do modelo: `GET /api/semantic/status` e `POST /api/semantic/warmup`.

| medida (corpus real, 144.754 × 384) | valor |
| --- | ---: |
| carga da matriz (f16 no disco → f32 na RAM) | 417 ms / **212 MiB** |
| produto `M @ v` em f32 (BLAS sgemv) | **5,8 ms** |
| o mesmo em f16, memmap por blocos | **199,3 ms** |
| `pontuar` inteiro (produto + máscara + argpartition k=50) | 7,8 ms |
| `SELECT id` sob o filtro padrão (144.754 ids) | 61 ms |
| o mesmo com `lang=pt` (38.715 ids) | 20 ms |
| embedding da consulta (modelo quente) | 10 ms |
| **consulta quente ponta a ponta** | **~87 ms** |
| `import sentence_transformers` + torch | 10,6 s |
| carregar o e5 | 4,7 s |
| **primeira consulta da sessão** | **~16,5 s** |

Pegadinhas deste bloco:

- **f32 residente é 34x mais rápido que f16 por blocos** (5,8 contra 199,3 ms), e o gargalo do f16 **não é a multiplicação, é a conversão** de 55,6 milhões de elementos por consulta. Os dois dão o mesmo escore (diferem em 6e-08). E o ganho colateral vale mais que a velocidade: **a 5,8 ms dá para pontuar o corpus inteiro e aplicar o filtro EXATO**, em vez de pegar os N melhores e filtrar depois. Over-fetch aproximado num recorte estreito devolve um top-k que parece certo e não é. `[app] semantic_precision = "f16"` existe como escape para pouca RAM.
- **Desalinhamento entre o `.npy` e o banco é o pior defeito possível** — cosseno continua saindo, vizinhos continuam plausíveis, cada uid é de outra linha, ninguém percebe. Duas guardas, as duas falhando alto: `sondar()` no lifespan (cabeçalho do `.npy` + contagem + 64 uids conferidos no SQL, ~109 ms) e `carregar()` na primeira busca (casamento uid → id **nos dois sentidos**, contra `[app] semantic_min_alinhamento = 0.99`). Coberto por `tests/test_semantic.py::test_desalinhamento_com_o_banco_falha_alto`.
- **503 e 500 são erros DIFERENTES aqui.** `.npy` ausente = `SemanticaIndisponivel` = **503** ("rode o s05/s06", clone limpo, falta de insumo). `.npy` de outra build = `DesalinhamentoEmbeddings` = **500** (o banco e a matriz discordam sobre o que é cada linha). Tratar o segundo como indisponibilidade temporária convidaria a ignorar.
- **A sonda avisa alto mas NÃO derruba a app.** A busca textual não depende dos embeddings, e recusar a interface inteira por causa de um arquivo auxiliar deixaria o usuário sem nem a lista. Quem falha é a rota; o `/api/health` carrega o resultado da sonda em `semantica`.
- **`aquecido` é matriz E modelo — e confundir isso foi um bug real, pego no navegador.** A matriz carrega em 417 ms e o e5 leva ~16 s; com `aquecido` respondendo só pela matriz, a interface anunciava "modelo pronto" meio segundo depois do clique e disparava a busca, que travava 16 s dizendo o contrário. `carregado` = matriz; `aquecido` = `carregado and _modelo_pronto`. O `aquecimento_ms` da resposta soma a carga da matriz **com** o embedding frio, porque o modelo só carrega de fato no primeiro `embed_texts`.
- **`row_factory = None` no cursor do `SELECT id`, e só nele.** `sqlite3.Row` constrói um objeto com nomes de coluna por linha, e aqui são 144.754 de uma coluna só: 76 → **61 ms**. (`np.array(cur.fetchall())` é PIOR, 85 ms: materializa a lista de tuplas antes.) Mora em `queries.executar_ids`.
- **`ConsultaSemantica` NÃO herda `sort`/`page`/`page_size`** — `extra="forbid"` os transforma em 422. Ordenar dentro de um resultado que existe por proximidade não significa nada, e "página 2 do sentido" menos ainda; aceitar e ignorar faria a interface prometer uma ordenação que não aconteceu. O front tem um caminho de URL separado por causa disso (`test_a_busca_por_sentido_nao_manda_sort_nem_pagina`).
- **`total` na resposta é quantos VOLTARAM; o conjunto é `n_candidatos`.** `truncated_total` é sempre `true`. A tela mostra `n_candidatos` no número grande — mostrar 50 ali diria que o filtro casou 50 linhas.
- **A busca por sentido ORDENA, não RECORTA.** Consequência prática: `/api/facets`, `from-filter` e o export operam sobre o filtro **sem o `q`** (`filtroSemTexto` no JS). Mandar a frase em português para o FTS5 casaria quase nada, e o botão "adicionar os N do filtro" prometeria um número que não é o do recorte.
- **Nenhum índice novo foi preciso.** Medido: `SELECT id, uid FROM prompts` já sai por `SCAN prompts USING COVERING INDEX sqlite_autoindex_prompts_1` (109 ms) e o `SELECT id` sob filtro cai no `idx_prompts_nsfw`/`idx_prompts_app`. O padrão de criar índice no lifespan e registrar no s11 continua valendo para o próximo que precisar.
- **O item da resposta ganha `similaridade` E preenche `score`.** `score` é o campo genérico de relevância que a listagem já devolve — só que lá é bm25 (**negativo**, menor é melhor) e aqui é cosseno (**maior é melhor**). A inversão de sinal é invisível para quem só lê o número, então a similaridade também sai com nome próprio. Nenhum campo do contrato foi renomeado ou removido (`test_busca_devolve_o_mesmo_shape_da_listagem`).
- **Não há cache do conjunto de ids**, e é decisão consciente: economizaria os 61 ms do caso sem filtro, mas ao custo de mais um estado que pode ficar velho e devolver o recorte errado — exatamente o que o cache do M9-C tem duas guardas para evitar. 87 ms já é rápido.

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
