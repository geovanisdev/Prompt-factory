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

Subcomandos e em que marco cada um saiu do stub: `ingest` (M2 fontes pequenas / M3 WildChat), `run` s01–s06 (M4), `report raw` (M2) e `report universe`/`report dedup-sample` (M4), `db-check` (M1) e `db-check --bench` (M8), `make-seed`/`labels`/`merge-labels` (M5), `load-db` (M8), `serve`/`export` (M9), busca semântica dentro do `serve` (M10), `annotate serve|seed|status` (P1/P3a), `annotate migrate` (P3a) e `annotate gerar` (P4c). Ainda stub: `train` (M7) e `apply` (M7) — o comando imprime o aviso e sai com **código 2**, o que é esperado, não é bug.

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

`src/prompt_factory/schema.py` (contrato canônico das 27 colunas) · `ingest/base.py` (contrato das 12 colunas do raw + `write_raw`) · `stages/__init__.py` (`StageConfig` + `STAGES`/`CADEIA`, único ponto de contato do `cli.py` com a pipeline) · `labeling_io.py` (manifest atômico, máquina de estados dos lotes e validação estrita) · `db.py` (DDL/FTS/`INDEXES`/conexão — o `executescript(DDL)` idempotente é o pivô da carga bulk) · `cli.py` (entrypoint) · `labeling/taxonomy.json` (fonte única da taxonomia) · `labeling/mappings/*.json` (categoria nativa → task_type) · `config/sources.toml` (licença e atribuição por fonte — **a ordem das seções é o desempate do dedup**; também a **única** fonte da `attribution` do export) · `app/queries.py` (TODO o SQL de leitura da interface: sanitizador do FTS, WHERE, ORDER BY, facetas) · `app/models.py` (o contrato de entrada da API) · `export.py` (REGISTRY de formatos + manifesto) · `annotate/db.py` (o DDL da Bancada e TODO o vocabulário fechado dela) · `annotate/geracao.py` (o livro-caixa da campanha de geração + a validação estrita do material) · `.claude/skills/rotular-prompts/SKILL.md` · `.claude/skills/gerar-material/SKILL.md`.

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

## Bancada — plataforma de anotação (Trilha B, P1–P3b)

Produto **novo e separado**, em `src/prompt_factory/annotate/`, com nome de tela **"Bancada"**. Não é uma aba da interface de curadoria: outro banco, outra porta, outra identidade visual.

```powershell
& "$env:USERPROFILE\.local\bin\uv.exe" run pf annotate seed          # projetos + diretrizes + personas + pacote + tarefas do pool
& "$env:USERPROFILE\.local\bin\uv.exe" run pf annotate seed --force  # APAGA fixtures e refaz (recusa se houver anotação; sai 3)
& "$env:USERPROFILE\.local\bin\uv.exe" run pf annotate status        # contagens + projetos + pool + política de licença
& "$env:USERPROFILE\.local\bin\uv.exe" run pf annotate migrate       # sobe o schema PRESERVANDO as anotações (deixa .v<N>.bak)
& "$env:USERPROFILE\.local\bin\uv.exe" run pf annotate               # = serve, em http://127.0.0.1:8766
```

```powershell
# Campanha de GERAÇÃO (P4c). Protocolo em .claude/skills/gerar-material/SKILL.md.
& "$env:USERPROFILE\.local\bin\uv.exe" run pf annotate gerar status  # painel + composição + cobertura do pool
& "$env:USERPROFILE\.local\bin\uv.exe" run pf annotate gerar preparar --campanha material -n 10 --lang ambos --out-dir tmp
& "$env:USERPROFILE\.local\bin\uv.exe" run pf annotate gerar preparar --campanha anotacoes -n 8 --out-dir tmp
& "$env:USERPROFILE\.local\bin\uv.exe" run pf annotate gerar preparar --lote mat_0003 --out-dir tmp  # RETOMA o mesmo lote
& "$env:USERPROFILE\.local\bin\uv.exe" run pf annotate gerar importar --lote mat_0003 --file tmp\r.json --model sonnet
& "$env:USERPROFILE\.local\bin\uv.exe" run pf annotate gerar importar --lote anot_0001 --file tmp\r.json --status pendente_avaliacao
```

* **Dois bancos, permissões diferentes.** `data/db/annotate.sqlite` (estado da plataforma; **esta app é a dona** e o cria sozinha na primeira subida) + `prompts.sqlite` **somente leitura**, sempre (`db.connect(..., readonly=True)`). A app de curadoria assume ser a única escritora do corpus (`app/cache.py:36-41`) e é isso que autoriza o cache de agregados dela — quebrar essa suposição transforma contagens certas em contagens plausíveis e erradas. Sem ATTACH; cruzamento em Python **por `uid`**, nunca por `id`/rowid (que muda a cada `pf load-db`).
* **Não é a mesma regra do `pf serve`.** A curadoria **se recusa** a criar o `prompts.sqlite` (quem o constrói é a pipeline); a Bancada **cria** o `annotate.sqlite` se faltar. Ao contrário do corpus, este banco guarda trabalho humano e **não é regenerável** — por isso o lifespan também recusa subir sobre um `schema_version_anotacao` diferente em vez de recriar nada.
* **O corpus não é congelado no lifespan.** Nem contagem, nem `db_build_id`, nem pool: o corpus troca por swap de arquivo embaixo da app, e número lido na subida é mentira com data de validade. `GET /api/health` lê tudo ao vivo, e a conexão do corpus é aberta **por request** pelo mesmo motivo (uma conexão de longa duração continuaria lendo o inode velho, sem erro nenhum).
* **O `-shm` órfão do corpus é obrigação nossa.** Conexão read-only em WAL **cria** o `-shm` e não consegue removê-lo ao fechar (remover é escrita). O arquivo órfão é exatamente o sinal que o pré-voo do swap do `pf load-db` lê como "alguém está com isto aberto" — a próxima recarga seria recusada por causa de uma app já morta. O shutdown da app e o `pf annotate status` apagam **só o `-shm`**, dentro de `try/except OSError`. **Nunca o `-wal`**: ele pode conter transações commitadas que ainda não foram para o arquivo principal. Matar o servidor com `taskkill` pula esse `finally` e deixa o `-shm` para trás — rode `pf annotate status` depois.
* **Pool com fallback obrigatório, MATERIALIZADO no `annotate.sqlite`.** A origem boa é a coleção `[annotate] pool_collection` do corpus; como o s11 **apaga as coleções** a cada carga, hoje o que vale é o fallback (`[annotate] pool_fallback_*`). A origem ativa aparece no `/api/health` e no painel do admin — pool trocando em silêncio é mentira de interface. Coleção existente mas **vazia** conta como ausente. Resolver no corpus a cada request custava **607 ms medidos** (as docstrings antigas prometiam 100 ms e 0,2 ms — erravam por 6x e 70x) em três rotas do caminho principal; agora a tabela `pool(uid, ordem)` é reconstruída quando a **assinatura** muda. A assinatura é `sha256(db_build_id do corpus + config do pool inteira + tamanho da coleção)`: sem o segundo ingrediente, mexer em `pool_fallback_lang` não refaria nada e pareceria que a chave não funciona; sem o terceiro, a outra ferramenta enche a coleção sem trocar o `db_build_id` e o pool não veria.
* **Política de licença no pool, com o default FECHADO** (`pool_exigir_commercial_ok`/`pool_exigir_redistributable`). Até o P3a o pool filtrava idioma, tamanho e duplicatas e **não filtrava licença**: trocar `pool_fallback_lang` para `"en"` trazia **9.148 linhas `cc-by-nc-4.0` com `commercial_ok = 0`** para dentro do trabalho de anotação, sem aviso. O `export.py` já excluía `redistributable = 0` do export justamente porque a proveniência por linha é a tese do projeto — a plataforma não tinha herdado a política. Vale nas **duas** origens (um humano curando também pode escolher uma `cc-by-nc`), e o `/api/health` diz qual política está ativa e **quantas linhas ela exclui**.
* **A garantia de NSFW é honesta, não vazia.** A cláusula `nsfw IS NOT 1` fica (vale no dia em que a rotulagem rodar) e **hoje exclui zero linhas**: `nsfw` é nulo nas 159.733. A tela, o `descrever_fallback` e o `pf annotate status` dizem isso com todas as letras (`pool.NOTA_NSFW`) em vez de prometerem "sem NSFW". Prometer uma garantia que o dado não sustenta é pior que não ter a garantia.
* **O fallback amostra com passo constante, não com `LIMIT`.** O universo é gravado agrupado por fonte: `ORDER BY id LIMIT 500` devolve 500 linhas todas de `arena140k`. O passo sobre o conjunto inteiro devolve a proporção real (medido em pt: 411 wildchat_pt, 69 aya, 16 arena140k, 4 oasst).
* **Nada de `innerHTML` neste `index.html`, sem exceção.** A interface de curadoria tem uma (`snippetSeguro`, para reabrir o `<mark>` do FTS); aqui não há busca com destaque, então a regra fica absoluta e o teste a cobra. Todo nó nasce de `el()` e todo texto entra por `textContent`.
* **Portas 8765 (curadoria) e 8766 (Bancada) são distintas de propósito**: as duas sobem juntas na demonstração, e colisão de porta apareceria como "a interface errada abriu".
* Papéis **sem senha** (teatro de autorização, escrito na tela), mas o servidor valida o papel pela linha em `anotadores`, nunca pelo que o cliente afirma ser (`deps.exigir_papel`).
* Schema em pt-BR de propósito, com vocabulário fechado: cada `CHECK ... IN` do DDL sai de uma tupla de `annotate/db.py` (`PAPEIS`, `TIPOS_TAREFA`, ...) e o Pydantic valida contra a mesma tupla — banco e API concordam por construção. Ver `annotate/db.py` para o porquê de cada CASCADE (e de cada ausência: apagar anotador é recusado, porque o histórico dele é o que as métricas somam).

### O fluxo do anotador (P2)

`annotate/tarefas.py` (fila, trava, hidratação) · `annotate/payloads.py` (os 4 contratos) · `annotate/catalogo.py` (busca no pool) · `annotate/routes_trabalho.py` (as rotas) · `annotate/fixtures/demo_pack.json` (o pacote) · `annotate/seed.py` (as 3 camadas do seed).

| rota | o que decide |
| --- | --- |
| `POST /api/tarefas/proxima` | a trava do modo locked, sob `BEGIN IMMEDIATE` |
| `POST /api/tarefas/livre` | find-or-create por `(tipo, prompt_uid)` + atribuição sem prazo |
| `GET /api/catalogo` | busca no pool reusando `sanitize_fts`/`executar_fts` de `app/queries.py` |
| `GET /api/prompts/{uid}` | resolve corpus × demo pelo prefixo; 404 fora do pool |
| `POST /api/atribuicoes/{id}/submeter` | valida o payload pelo tipo **lido do banco**; grava `versao = max+1` |
| `POST /api/atribuicoes/{id}/abandonar` · `GET /api/atribuicoes` | devolver a vaga · minhas tarefas |
| `GET /api/tarefas/contagens` | os contadores das abas, **por pessoa** |

Pegadinhas deste bloco:

- **`gabarito_json` nunca sai pela API do anotador**, e a garantia não é a chave que falta: o `tarefas` do envelope é montado **campo a campo**, e a coluna nem entra no SELECT. Um `dict(linha)` passaria no teste de hoje e vazaria no dia em que alguém acrescentasse uma coluna. O `meta_json` das respostas (que guarda o defeito plantado) tem o mesmo tratamento.
- **Fila vazia é 200 com `{tarefa: null, motivo, motivo_chave, motivo_dados}`, não 404.** Fila vazia é o estado mais comum de uma plataforma bem servida; um 404 faria o cliente tratar o caminho normal como falha. O `motivo` é a frase em português (a API se explica sozinha em `/docs`); a TELA usa `motivo_chave` — ver o P3i.
- **O prazo é calculado pelo SQLite, não em Python.** `strftime('%Y-%m-%dT%H:%M:%fZ','now', '+120 minutes')` — mesmo formato e mesmo relógio do `expira_em` que a expiração compara. Dois relógios dariam uma expiração que erra por um. E o formato **tem** de ser esse: `datetime('now')` sai com espaço e sem `Z`, e a comparação lexicográfica quebraria em silêncio, com os dois lados parecendo datas.
- **Expiração preguiçosa, sem thread de fundo:** um UPDATE antes de cada claim e de cada listagem. A definição de "vaga ocupada" (`tarefas.SQL_VIVA`) repete a condição do prazo de propósito — o UPDATE é a materialização dela, não a fonte.
- **O modo livre IGNORA as vagas, e isso é decisão.** `n_anotacoes_alvo` governa a FILA (quem recebe sem escolher). Quem foi ao catálogo já decidiu; recusar com "essa já tem duas pessoas" faria o botão "Anotar este" falhar de forma imprevisível.
- **Prazo vencido NÃO descarta anotação submetida.** O TTL existe para devolver a vaga a quem espera, não para punir quem demorou; a resposta traz `expirou: true` e a tela avisa. Jogar fora trabalho humano por causa de um relógio é o pior desfecho possível aqui.
- **Find-or-create é por `(tipo, prompt_uid)`, não por origem.** Escolher no catálogo um par que a semente já cobriu **reusa** aquela tarefa: criar uma segunda contaria a mesma anotação duas vezes em toda métrica e quebraria o `ja_anotei`.
- **Nem todo prompt sustenta todo tipo.** Comparar A/B exige 2 respostas de modelo; avaliar exige rubrica ativa **e** resposta. Um prompt cru do corpus não tem nada disso. `catalogo.marcar_material` carimba `pode` em cada item (o botão já sai desabilitado, com o motivo no `title`) e `catalogo.material_faltando` é quem recusa no servidor, com 409 — as duas pontas usam a **mesma** função, senão haveria botão habilitado que devolve erro.
- **Uid sumido do corpus vira tarefa `pausada` + bilhete no `payload_json`**, e o claim pula para a próxima. Nunca 500. Vai acontecer: o universo desta máquina foi de 144.754 para 159.733 numa recarga.
- **`bool` é subclasse de `int`, terceira vez neste repo.** `"nota": true` viraria nota 1 — uma nota **válida** e errada. Todo campo numérico dos payloads (e o `anotador_id`) tem validador `mode="before"`. A ordem importa: o Pydantic coage antes de qualquer validador comum.
- **1..9 é o teto da PLATAFORMA; a escala real é a da rubrica.** O Pydantic não conhece a rubrica, então ele só barra fora de 1..9; quem confere a escala de cada critério (e a cobertura de todos eles) é a rota, que tem a rubrica em mãos. É a mesma conta que o botão da tela faz **antes** do clique — os dois concordarem é o que faz a validação parecer instantânea sem deixar de ser do servidor.
- **Os limites saem do `/api/health` (`limites`), não de uma cópia no JS.** É o que impede o botão "faltam 30 caracteres" de divergir do 422 quando alguém ajusta o `settings.toml`.
- **A rubrica-guia do SFT vindo de continuação sai do PAYLOAD da anotação, não de `rubricas`.** A rubrica só é materializada na aprovação (P3); sem esse atalho, quem acabou de escrever uma rubrica escreveria a resposta sem ela ao lado — que é exatamente o valor da continuação.
- **O seed do pool usa fatias DISJUNTAS e de PASSO CONSTANTE** (`seed.fatias_por_passo`) para `escrever_rubrica` e `sft_resposta`. Disjuntas porque, com os mesmos prompts, a fila de SFT já traria a rubrica pronta e a oferta de continuação nunca teria o que criar. Passo constante porque a versão antiga fazia `uids[:8]`/`uids[8:16]` sobre uma lista ordenada por id num universo agrupado por fonte, e **16 das 17 tarefas do corpus saíam de `arena140k`** (uma delas era o texto "Você não tem nada pra responder. Isso não é realmente um prompt…", impossível de rotular). É o MESMO defeito que o P1 já havia consertado no pool e que reapareceu no seed. As duas fatias saem **alternadas** (pares/ímpares), e não cortadas ao meio: com blocos grandes (411 dos 500 uids são `wildchat_pt`) cortar ao meio jogaria a segunda metade toda numa fonte só.
- **`--force` recusa sobre trabalho humano e sai com código 3.** `tarefas` cascateia para `atribuicoes` e daí para `anotacoes`. Ele também **não apaga personas**: elas são a identidade guardada no `localStorage` do navegador.
- **O catálogo descarta resposta atrasada.** Trocar dois filtros em sequência dispara duas consultas e a primeira pode responder por último; só a busca mais recente escreve em `est.catalogo.dados` (contador `buscaAtual` no JS).
- **`app.routes` não lista mais as rotas dos routers incluídos** nas versões recentes do FastAPI (elas ficam dentro de um `_IncludedRouter`). Quem quiser conferir caminhos em teste usa `app.openapi()["paths"]`.
- **`comParams(caminho, params)` aceita objeto simples E `URLSearchParams`.** Antes só aceitava o segundo, e passar um objeto produzia `?[object Object]` — uma URL válida, um 422 por campo faltando e uma **tela vazia sem erro visível**. Foi exatamente assim que a primeira montagem da fila de triagem "funcionou" mostrando zero itens com a API devolvendo seis.
- **Medido no banco real** (159.733 prompts, pool de 500): `pf annotate seed` semeia **11** prompts + 11 rubricas + 22 respostas + 11 tarefas do pacote e 8+8 tarefas do corpus; a segunda execução cria **zero**. (Eram 8+8+16+8 até o P3i, que acrescentou três itens com prompt em inglês.)

### Schema v2, migração e triagem (P3a)

`annotate/migracao.py` (`pf annotate migrate`) · `annotate/routes_revisao.py` (a triagem) · `annotate/solo.py` (modo solo) · `annotate/projetos.py` · `annotate/diretrizes.py` + `fixtures/diretrizes.json` · `annotate/eventos.py`.

| rota nova | o que decide |
| --- | --- |
| `GET /api/revisao/fila` | fila cronológica da triagem; exclui as do próprio revisor (salvo modo solo) |
| `GET /api/revisao/{id}` | a submissão no MESMO envelope do anotador + o payload |
| `POST /api/revisao/{id}` | `aprovada` → `pendente_avaliacao` (materializa rubrica) · `devolvida` → reabre a atribuição |
| `GET /api/projetos` · `GET /api/diretrizes` | o seletor da barra · a regra vigente com versão |

Pegadinhas deste bloco:

- **A migração reconstrói AO LADO e troca**, como o s11 faz com o corpus — não `ALTER TABLE`. Três propriedades que o `ALTER` não daria: o schema migrado é **idêntico** ao de um banco novo (há teste que compara os dois `sqlite_master`), falhar no meio não estraga nada, e a cópia é explícita coluna a coluna (uma coluna nova sem tratamento vira `KeyError` na hora, não NULL descoberto meses depois). O arquivo antigo vira `.v1.bak` — a operação inteira se desfaz com um `mv`.
- **`init_db` recusa ANTES de tocar no DDL** quando a versão diverge (`db.SchemaDivergente`). Aplicar o DDL da v2 sobre um banco v1 criaria as tabelas novas e deixaria as antigas com os CHECKs velhos: um meio-schema que passa em todo teste de existência e falha na primeira transição de status. **Meio-migrado é pior que não migrado, porque não parece quebrado.**
- **Backfill de status:** `pendente_revisao` → `pendente_triagem`; `aprovada` → `pendente_avaliacao` (a revisão da v1 era só a primeira passagem; dizer `avaliada` afirmaria uma avaliação que ninguém fez); `rejeitada` → `devolvida`. Um status fora do mapa **recusa a migração inteira**.
- **`rubricas` é copiada DEPOIS de `anotacoes`** na migração: `rubricas.anotacao_id` referencia `anotacoes`, e `db.connect` liga `foreign_keys=ON`. Hoje toda rubrica é fixture com `anotacao_id` nulo e a ordem não apareceria — a partir da triagem, que materializa rubricas de verdade, apareceria.
- **A máquina de status é DADO** (`db.TRANSICOES`), não uma sequência de `if`. As rotas consultam dela e o teste lê dela — uma transição nova aparece nos dois lugares de uma vez. **Só a triagem devolve trabalho ao anotador**: `incorrigivel` do Rate and Review é terminal, porque um item aprovado que volta dias depois mede o revisor, não quem anotou.
- **`veredito` é `aprovada|devolvida`**, não `rejeitada`: nada é recusado na triagem, o trabalho volta para ajuste — e o status que ele produz na anotação tem exatamente esse nome. Um veredito que não vira estado seria decoração.
- **Devolver REABRE a mesma atribuição** (`em_andamento`), não cria outra: o `UNIQUE(tarefa_id, anotador_id)` é a trava anti-repetição, e uma segunda atribuição faria o mesmo trabalho contar duas vezes em toda métrica. Quem versiona é `anotacoes.versao`, e o `submeter` grava `max + 1` sozinho.
- **A diretriz é versionada e gravada por anotação** (`anotacoes.versao_diretriz`, de `fixtures/diretrizes.json` → tabela `diretrizes`). O cenário: a regra do A/B é ajustada na terça, o cliente reclama de inconsistência na quinta — sem a versão, a única saída é refazer o lote inteiro. A versão é lida do **banco pelo servidor**, nunca do que o cliente afirma ter lido (JS em cache diria a versão velha). **Editar uma versão já usada é proibido** e o `semear` levanta: suba a `versao` no arquivo.
- **MODO SOLO (`[annotate] permitir_autorrevisao`, default `false`) muda DUAS regras juntas**, e é a mesma decisão: o papel deixa de ser autorização e vira vista, **e** o revisor passa a ver as próprias. Afrouxar só a segunda não resolveria nada — ao trocar para o papel de revisor a interface teria de vestir outra persona, e o trabalho do autor apareceria revisado por uma fixture. **Atribuição falsa num portfólio é pior que permissão larga numa app de `127.0.0.1`.** Nada disso acontece em silêncio: faixa permanente na fila (não se fecha), cada item marcado como seu, `revisoes.autorrevisao`/`avaliacoes.autorrevisao` gravados (não deduzidos depois — `atribuicoes.anotador_id` pode ser corrigido e a derivação passaria a mentir), e o `/api/health` + `pf annotate status` declarando o modo.
- **Dois projetos nascem com o banco:** `Portfólio` (trabalho real sobre o corpus, default de toda tarefa nova) e `Demonstração` (o pacote de fixtures). Separá-los é o que permite ao painel e ao export dizerem qual trabalho é de quem, em vez de somar fixture com autoria na mesma coluna. A migração aloca pelo **prefixo do uid** (`demo:`), que é o mesmo contrato do resolvedor — nenhum chute.
- **`tarefas.projeto_id` é NULLABLE de propósito.** `ALTER TABLE ADD COLUMN` com `REFERENCES` exige default NULL, e o schema de um banco novo tem de ser idêntico ao de um migrado (há teste). Quem garante o preenchimento é o seed e as rotas.
- **`eventos.acao` é o único campo sem CHECK do schema.** Mesma razão do `task_type` do corpus: o vocabulário cresce a cada marco, e migrar um banco com trabalho humano só para registrar o NOME de um evento novo seria absurdo. O inventário do que existe está em `eventos.ACOES`.
- **O evento é gravado na MESMA transação da mudança que ele registra.** Um INSERT depois do COMMIT some quando o commit falha — e a trilha passa a mentir justamente nos casos interessantes.
- **O catálogo faz UMA passagem, com `count(*) OVER ()`.** O total teria de ser calculado de qualquer jeito, então a janela sai de graça e a segunda ida ao banco desaparece. Medido no corpus real buscando `"como"` (23.561 acertos, o pior caso: um termo quase-vazio): 186 ms com duas consultas, **105 ms** com uma. Termos normais ficam em 19–34 ms.
- **Os quatro `ws*` do JS servem a DOIS leitores** (quem anota escreve, quem tria lê) via `ctx = {form, leitura}`. Um segundo desenho "de revisão" seria um segundo lugar onde a rubrica pode divergir de si mesma — e a divergência apareceria justamente no item que o revisor está julgando.

### Idioma: inglês primário, pt-BR no toggle (P3i)

`annotate/static/index.html` (o dicionário `TEXTOS` + `t()`) · `annotate/diretrizes.py` + `fixtures/diretrizes.json` · `annotate/fixtures/demo_pack.json` · `annotate/pool.py` · `export.IDIOMA_DOS_ARTEFATOS` · `tests/test_annotate_i18n.py`.

O portfólio é lido por avaliadores **estrangeiros**: o inglês virou a língua primária e o pt-BR um toggle. Não foi um embrulho — as telas do P1/P2/P3a estavam em português e foram **traduzidas**. **Comentário de código, docstring e mensagem de CLI continuam em pt-BR**: são internos, e é a convenção do repo.

**A convenção de língua dos DADOS** (padrão de mercado, e o que a tela diz campo a campo):

| o quê | língua | por quê |
| --- | --- | --- |
| justificativa, comentário de revisão, critério de rubrica, todo metadado | **inglês** | é o que o cliente lê |
| resposta esperada (SFT) e a conversa com o modelo | **língua do prompt** (pt-BR em geral) | é o dado sendo produzido |
| dataset card, relatório de qualidade, auditoria (P5b) | **inglês** | ver `export.IDIOMA_DOS_ARTEFATOS` |

Pegadinhas deste bloco:

- **`TEXTOS` é JSON ESTRITO dentro do JS.** Aspas duplas em chave e valor, sem vírgula sobrando, sem comentário dentro do bloco. É isso que permite ao pytest carregá-lo com `json.loads` e provar paridade e ausência de chave órfã **sem um parser de JavaScript**. Quebrar a sintaxe estrita não quebra a tela — quebra os testes que a protegem, que é pior.
- **Chave montada por concatenação é invisível para o teste.** `t("tela." + papel)` passaria despercebido pelo teste de chave órfã e deixaria entradas mortas no dicionário. Por isso as TABELAS (`TIPOS`, `VISTAS`, `ROTULO_STATUS`, `ROTEIRO`, `PAPEIS`, `FAIXAS`, `MODELOS_RUBRICA`) guardam a **chave inteira** como literal.
- **Texto estático no HTML não existe.** Cada nó carrega `data-t` (textContent), `data-t-ph`, `data-t-title` ou `data-t-aria`, e `aplicarTextos()` os preenche. Uma frase solta no corpo da página ficaria em português para sempre, inclusive em modo `en`, e nenhum teste de dicionário a pegaria.
- **Trocar de idioma NÃO recarrega e NÃO refaz chamada.** `escolherIdioma()` redesenha a partir de `est` — e é por isso que rascunho, tarefa aberta, seleção da triagem e página do catálogo sobrevivem. Três campos precisaram MUDAR DE CASA para isso valer: o comentário de devolução (`est.triagem.comentario`), a busca do catálogo (`est.catalogo.q`, escrita a cada tecla) e o motivo da fila vazia (`est.motivoChave`, guardado como chave e traduzido no desenho).
- **As rotas falam uma língua; a tela fala duas.** Toda frase de tela que nascia no backend agora sai em DOIS formatos: a versão em português (para `/docs` e para o `pf annotate status`, onde pt-BR é a convenção) **e** a chave + os dados. São quatro lugares: `tarefas.CHAVE_MOTIVO_*` (fila vazia), `pool.T_*` (as quatro frases diagnósticas do painel do admin), `rubrica_ativa().nota_chave` (a rubrica proposta) e a oferta de continuação, que **deixou de existir no backend** — `convite`/`porque` saíram de `routes_trabalho` e viraram `cont.convite`/`cont.porque` no dicionário. Frase de tela mora na tela.
- **O painel do admin era o pior vazamento**, e não o mais óbvio: `pool.motivo`, `descrever_fallback()`, `politica_licenca().descricao` e `NOTA_NSFW` são exatamente o que o brief manda um avaliador procurar (proveniência), e chegavam em português. `_do_banco` guarda a chave em `app_meta` (`pool_motivo_i18n`); um banco materializado ANTES do P3i não a tem, e o default sai do próprio `app_meta` para não mostrar `{colecao}` cru.
- **`.dado` marca o que NÃO se traduz.** Nome de projeto, de pessoa, de fonte e descrição de projeto vêm do banco e saem na língua em que foram cadastrados. A classe não pinta nada: ela existe para que a varredura de língua consiga separar "Portfólio" (um projeto que se chama assim) de uma string de cromo esquecida na tradução.
- **A VERSÃO da diretriz é do conteúdo, não da tradução.** `v1` em inglês e `v1` em português são a mesma regra dita em duas línguas, e `anotacoes.versao_diretriz` continua apontando para a versão — nunca para o par (versão, língua). Por isso as duas moram na **mesma linha** da tabela `diretrizes` (contrato `diretrizes@2`, `{"textos": {"en": [...], "pt": [...]}}`), e não numa coluna `lang`: com uma linha por língua, publicar `v2` só em inglês faria `versao_vigente` devolver 2 e a tela em português abrir **vazia**. Nenhuma migração de schema foi precisa — `pf annotate migrate` continua dizendo "já está na versão 2".
- **O seed PROMOVE a diretriz monolíngue, e só ela.** Um banco do P3a tem a v1 no contrato `@1`; `diretrizes._promovivel` aceita a sobrescrita **apenas** quando as linhas em português continuam idênticas palavra por palavra — acrescentar tradução não muda a regra. Qualquer outra diferença continua levantando "não se edita".
- **O mesmo vale para as fixtures do pacote.** `seed._traduzir` atualiza `criterios_json`/`meta_json` de uma rubrica ou resposta **já semeada** quando o CANÔNICO (o objeto sem os `*_i18n` e sem o rótulo de schema, via `seed._canonico`) é idêntico. Sem isso, a idempotência por chave natural deixaria as oito rubricas do banco do dono monolíngues para sempre. Medido: `pf annotate seed` promoveu 4 diretrizes e traduziu 24 fixtures, com 7 anotações preservadas e todas ainda em `versao_diretriz = 1`.
- **O canônico do critério é IDENTIDADE, não texto.** É a string que `anotacoes.payload_json` grava em `notas[].criterio` e que `_conferir_contra_a_rubrica` casa. Nos oito itens originais ele continua em **português** (mexer nele apagaria a leitura das anotações já gravadas); nos três itens novos e nos modelos de rubrica do JS ele é **inglês**, que é a convenção daqui para a frente. O `*_i18n` é sidecar de exibição, e ausente significa "uma língua só" — o caso de toda rubrica escrita por um anotador.
- **`pool_fallback_lang` aceita string E lista.** Com `["pt", "en"]` o teto de `pool_max` é dividido em **cota igual por língua** (`pool._cotas`) e cada cota é amostrada com passo constante dentro dela. Proporcional ao corpus não serviria: são 116.051 linhas em inglês contra uma fração disso em português, e o pool sairia monolíngue de novo, na outra ponta. Quem mantém as 9.148 `cc-by-nc-4.0` de fora é a **política de licença**, não o filtro de idioma. Medido no banco real: 500 uids, **250 pt + 250 en**, 8 fontes, zero `cc-by-nc`.
- **Prompt e resposta de modelo NÃO são traduzidos**, no pacote nem em lugar nenhum: a língua deles é intrínseca ao dado. O que ficou bilíngue é a ESTRUTURA (título de rubrica, critério, descrição, âncora de escala, catálogo de defeitos). Os três itens novos têm prompt em inglês de verdade — um avaliador que não lê português precisa conseguir **fazer** o trabalho, não só olhar a interface traduzida.
- **`navigator.language` só desvia para o português.** Sem preferência salva, `pt*` abre em pt e todo o resto abre em inglês: um avaliador estrangeiro nunca pode ver esta plataforma em português. A preferência salva vence o navegador, e um valor inválido no `localStorage` cai no inglês.

### Rate and Review e escalação (P3b) — schema v3

`annotate/avaliacoes.py` (o diff campo a campo, a fila, o parecer) · `annotate/routes_avaliacao.py` (passagem 2 + escalação) · `annotate/models.py` (`AvaliarIn`, `EdicaoIn`, `DecisaoAdminIn`) · `annotate/migracao.py` (`_da_v1`/`_da_v2` + `PASSOS`) · `tests/test_annotate_p3b.py`.

A passagem 2 tem quatro passos, nesta ordem: **avalia como chegou** (`inutilizavel|ajustavel|adequado|excepcional`) → **corrige no lugar**, com motivo por mudança → **avalia o resultado** (`incorrigivel|borderline_admin|adequado|excepcional`) → **justificativa geral, obrigatória sempre**. `adequado`/`excepcional` → `avaliada`; `borderline_admin` → `escalada` + fila do admin; `incorrigivel` → `descartada`, terminal.

| rota nova | o que decide |
| --- | --- |
| `GET /api/avaliacao/fila` | o que a triagem aprovou, **com quem aprovou** (`triador`) |
| `GET /api/avaliacao/{id}` | o envelope da triagem + o comentário de quem triou |
| `POST /api/avaliacao/{id}` | as duas escalas, o diff conferido e o desfecho, numa transação |
| `GET /api/escalacao/fila` · `POST /api/escalacao/{avaliacao_id}` | os borderline sem decisão · o admin encerra |

Pegadinhas deste bloco:

- **O diff é calculado pelo SERVIDOR, não aceito do cliente.** `avaliacoes.diferencas` achata os dois payloads (`{"notas": [{"nota": 3}]}` → `{"notas.0.nota": 3}`) e exige **igualdade exata** entre os caminhos que mudaram e os motivos declarados. Os dois lados são 422 e são erros diferentes: mudança sem motivo é alguém alterando trabalho alheio em silêncio; motivo sobre campo que não mudou faz a auditoria descrever uma correção que não houve — que é pior que não ter auditoria. Os valores gravados em `edicoes_avaliacao` são os do servidor: a trilha descreve o payload, não a tela.
- **Só FOLHAS viram caminho.** Sem isso, "mudei a nota do critério 2" apareceria três vezes (`notas`, `notas.2`, `notas.2.nota`) e pediria três motivos para uma mudança. Lista/dicionário **vazio** é folha de propósito — senão apagar o último item de `por_criterio` não seria mudança nenhuma.
- **O JS e o Python têm de achatar igual**, e é por isso que `montarPayload(env, form)` ganhou o parâmetro: o front monta os DOIS payloads (o original e o corrigido) com a mesma função. Duas montagens diferentes produziriam diferenças que não existem, e o revisor teria de justificar mudanças que não fez. Quem decide continua sendo o servidor — o JS só evita o 422.
- **`ctxEdicaoRevisor` é o terceiro leitor dos mesmos `ws*`.** O contexto passou a carregar `aoDigitar`/`aoMudar`/`aoFocar`/`motivos` porque os callbacks eram chamadas diretas às funções do ANOTADOR: sem isso, o revisor escreveria no rascunho da tarefa que o anotador tem aberta. `graduar`/`escolherAB` viraram atalhos de `graduarEm`/`escolherABEm`.
- **O motivo por mudança não é modal, e não redesenha a tela.** `rrSincronizar` reconcilia as caixas de motivo incrementalmente — só entra a linha que passou a existir e só sai a que deixou de existir. Reconstruir tudo a cada tecla tiraria o foco do campo no meio da palavra. Uma caixa de **último recurso** com prefixo `""` é registrada por último: um caminho que nenhum prefixo reivindicasse viraria uma mudança impossível de justificar e um botão que nunca destrava.
- **Aprovar na triagem invalida `est.rr`.** Sem isso a sub-aba do Rate and Review continuaria mostrando o que leu na última visita, e o item recém-aprovado só apareceria depois de sair da tela e voltar. O contador da sub-aba mostra **`…`** (não `0`) enquanto a fila não foi lida: são estados diferentes, e um zero inventado é a única leitura que faz alguém deixar de clicar.
- **Devolver tem de RENOVAR o prazo** (`tarefas.reabrir`/`SQL_REABRIR`). Medido no banco real: devolver um item aprovado dias antes reabria a atribuição com `expira_em` no passado, o `expirar_vencidas` da listagem seguinte a marcava `expirada` no mesmo request, e o anotador via "prazo expirado" **sem botão** no lugar da instrução. Vale para os DOIS caminhos de volta — a triagem já tinha o defeito desde o P3a. `expira_em IS NULL` (modo livre) continua sem prazo.
- **São DOIS caminhos de volta, e o segundo some sem ninguém notar.** A triagem devolve (`revisoes`) e o admin, decidindo uma escalação, também (`decisoes_admin`). `_versao_anterior` e `/api/atribuicoes` trazem os dois comentários em campos próprios (`comentario_revisor` / `comentario_admin`): sem o JOIN de `decisoes_admin`, o admin escreveria um comentário que a rota exige — com a frase "é a única instrução que ele recebe" — e que o anotador nunca leria.
- **`devolvida` não é transição de `pendente_avaliacao`, e a rota lê isso de `db.TRANSICOES`.** Um item aprovado na triagem que volta ao anotador dias depois mede o revisor da triagem, não quem anotou. O admin, sobre um item **escalado**, pode devolver: é o caminho de escalação, não a segunda passagem se contradizendo.
- **`incorrigivel` NÃO toca a atribuição.** Ela fica como a triagem a deixou (`aprovada`). Reabri-la devolveria trabalho por um caminho que a máquina de status não conhece.
- **`gabarito_avaliacao_json` (v3) é o alvo escondido de uma anotação SINTÉTICA** — `{avaliacao_antes, familia_defeito, nota}`, NULL em todo trabalho humano, e é essa nulidade que distingue os dois. Quem preenche é o P4c; quem lê é o painel do P5a. **Nunca sai pela API**: a coluna não entra no SELECT de `tarefas.hidratar_anotacao`, que é o envelope por onde passa TODA leitura do revisor. O teste varre a resposta inteira procurando uma senha plantada, e não a chave — conferir a chave passaria hoje e falharia em silêncio no dia em que alguém a renomeasse ao serializar.
- **A migração não encadeia versões.** Há `_da_v1` e `_da_v2`, cada uma copiando para dentro de um banco já no schema de HOJE. Encadear 1→2→3 exigiria manter vivo o DDL de cada versão do meio, e o teste que compara `sqlite_master` com um banco novo deixaria de valer no meio da cadeia. O backup vira `.v<origem>.bak`. `app_meta` e `eventos` são conferidos por "nunca menos" em vez de "igual": a própria migração grava o evento `banco_migrado`.
- **`checklist` do SFT não voltava do payload** — a triagem mostrava "nada marcado" para quem tinha marcado tudo, e o Rate and Review acusaria como mudança do revisor uma caixa que o anotador já havia marcado. `formDoPayload` agora casa por NOME de critério, como as notas do `avaliar_rubrica`.
- **Sem `A`/`R` na passagem 2, e é decisão.** Ela não tem duas respostas rápidas: tem duas escalas de quatro posições e um diff a justificar. `1..4` também ficou de fora — o mesmo `3` que graduaria a escala está graduando um critério da rubrica logo abaixo, na mesma tela. Ficam `J`/`K`, `Ctrl+Enter` e as setas dentro de cada radiogroup.
- **Medido no banco real** (8 anotações preservadas na migração 2→3): três avaliações, quatro edições com motivo, duas decisões do admin; `pendente_avaliacao` → `avaliada` (2 edições), → `escalada` → admin `devolvida` → atribuição `em_andamento` com prazo novo.

### Campanha de geração (P4c) — schema v4

`src/prompt_factory/annotate/geracao.py` (o livro-caixa, a seleção, a validação estrita e a gravação) · `geracao/manifest.json` + `lotes/` + `respostas/` · `cli._annotate_gerar` · `.claude/skills/gerar-material/SKILL.md` · `tests/test_annotate_p4c.py`.

Duas abas tinham **teto estrutural**: `avaliar_rubrica` exige rubrica ativa **e** uma resposta, `comparar_ab` exige **duas** — e só os 11 prompts do pacote de demonstração tinham isso. E havia um teto pior, que só aparece quando tudo dá certo: **se o dono anotar bem, as quatro escalas do Rate and Review nunca são exercitadas.** Daí as duas campanhas: **A (material)** levanta o teto, **B (anotações sintéticas)** calibra o revisor.

O desenho é o de `pf labels`, e não se negocia: **o agente gerador é puro cômputo** (lê o arquivo do lote, devolve JSON como texto, não escreve arquivo do repo, não usa git, não roda `pf`) e **só o fio principal grava**, pela CLI, com validação estrita antes de encostar no banco.

| ação | o que decide |
| --- | --- |
| `gerar preparar --campanha material` | prompts do pool SEM material, na ordem determinística do pool, pt/en intercalados |
| `gerar preparar --campanha anotacoes` | tarefas abertas com material em pé × persona livre × alvo sorteado pela distribuição |
| `gerar preparar --lote <id>` | RETOMA o lote existente (sessão caída, `failed` revivido) — nunca regera |
| `gerar importar --lote <id> --file <json>` | validação estrita → transação única → `done` |
| `gerar status` | painel + composição humano/sintético + cobertura do pool |

Pegadinhas deste bloco:

- **A v4 acrescenta um VALOR a um CHECK, e nenhuma coluna.** É a migração mais fácil de dispensar por engano — "não tem coluna nova, não precisa migrar" parece verdade. Só que `CREATE TABLE IF NOT EXISTS` **não reescreve o CHECK** de uma tabela que já existe: sem `pf annotate migrate`, o banco continuaria recusando a rubrica `origem='importada'` com uma regra que nenhum arquivo do repositório mostra mais. **Um CHECK velho não parece quebrado: parece um bug do código que está tentando escrever.**
- **`origem='importada'` em `rubricas` e em `respostas_modelo`, nunca `'fixture'`.** As duas saem do mesmo lote e não podem contar histórias diferentes sobre a própria procedência; e chamar de fixture uma rubrica gerada sobre prompt REAL do corpus seria mentir numa coluna.
- **A marca de sintética é UMA: `gabarito_avaliacao_json IS NOT NULL`.** Não há segunda marca, e a ausência dela é a decisão — duas marcas divergem, e no dia em que divergissem ninguém saberia qual acreditar. `geracao.composicao()` é onde o sinal vira número, e é ela que o P5a e o P5b chamam. Sintética fica **fora dos exports de dado por padrão**: o projeto não usa prompt de IA para treinar IA e não entrega anotação de IA como humana.
- **O alvo é decidido pelo PLANO, e o agente ECOA de volta.** Divergência é erro nomeado. Sem o eco, um agente que processasse os itens fora de ordem produziria trabalho perfeitamente **válido** casado com o alvo errado — e o painel do P5a passaria a medir o revisor contra uma expectativa que ninguém teve. A validação da campanha B roda contra o plano do **manifest**, não contra o arquivo do lote (que alguém pode reescrever); só a rubrica sai do arquivo, porque é a que o agente teve em mãos.
- **A escala real é a da rubrica, e o Pydantic não a conhece** (ele só barra fora de 1..9). `tarefas.erro_contra_a_rubrica` é a função ÚNICA que a rota de submissão e o import chamam. Ela nasceu deste marco: a primeira versão do import validava só o contrato do payload, e uma sintética entrou com **nota 7 num critério de 1..5** — a tela desenharia cinco botões e um valor que não é nenhum deles. Duas cópias da regra divergiriam, e a divergência apareceria como uma anotação que a tela não sabe desenhar, sem nada apontando para quando ela entrou.
- **Falha de validação NÃO muda o estado** (o lote segue `claimed` e o retry dirigido é a correção normal); o que muda é a contagem. Em `[geracao] max_tentativas` (3) o lote cai em `failed` e para de travar o ciclo, e reviver é ato explícito. Claim órfão volta a `pending` por `[geracao] claim_ttl_horas` (6 h, contra 2 h da rotulagem: escrever duas respostas defensáveis é trabalho de horas).
- **Recusar por idioma exige CERTEZA; deixar passar, não.** `conferir_lingua` só recusa quando o detector aponta a **outra** língua de {pt, en} acima de `[geracao] lang_confianca_min`; texto curto, terceira língua ou baixa confiança passam em silêncio. É a lição do s02: payload longo afoga a instrução, e um falso positivo aqui joga fora o trabalho de um lote inteiro.
- **Os dois catálogos de defeito são DISJUNTOS**, e é isso que faz a passagem 2 valer: um revisor treinado a pegar resposta ruim não é o mesmo que um treinado a pegar avaliação ruim. Resposta: fato inventado com precisão · restrição ignorada · registro errado · fluência cobrindo vazio. Anotação: nota que não bate com a justificativa · justificativa genérica · critério não observável · restrição do prompt ignorada · nota inflada/deflacionada em bloco · **justificativa em português onde a convenção pede inglês**. Os dois são **abertos**: nome fora do catálogo é AVISO, não erro — barrar obrigaria a editar código para escrever o defeito novo, e o defeito novo é o que a campanha descobre.
- **Cota inteira por faixa, não sorteio item a item.** Com n=6 e 15% de `inutilizavel`, sortear cada item de forma independente daria uma chance real de a faixa não aparecer nenhuma vez — e o lote deixaria de exercitar o botão que ele existe para exercitar. `[annotate] sinteticas_distribuicao` normaliza os pesos; chave fora de `AVALIACOES_ANTES` **para a campanha**.
- **`adequado`/`excepcional` nascem com `familia_defeito = "nenhum"`.** Plantar defeito num alvo bom produziria uma anotação boa que se declara ruim, e o revisor que acertasse contaria como errado no painel.
- **A sintética entra como `pendente_triagem` (default) ou `pendente_avaliacao`**, e a atribuição segue: `submetida` no primeiro caso, `aprovada` no segundo — que é como a triagem a deixaria. Escrever `submetida` no caminho direto faria a aba "minhas tarefas" da persona mostrar um item eternamente em revisão numa passagem que ele pulou.
- **Rodízio de personas.** A lista é girada pelo tamanho do plano; sem isso, num banco com tarefas livres o lote inteiro sairia atribuído à persona de menor id e o painel mediria uma pessoa só. Quando as outras já ocupam a tarefa o rodízio não muda nada — quem decide é o `UNIQUE(tarefa_id, anotador_id)`.
- **`preparar` grava o arquivo ANTES do manifest**, e o id do lote vem do manifest e **nunca** de `listdir` — as duas disciplinas do part-file do WildChat, pelas mesmas razões (crash entre as escritas; arquivo órfão fazendo o índice pular).
- **`gravar_material` é idempotente por chave natural.** Duas rubricas ativas no mesmo prompt fariam `rubrica_ativa` devolver a mais nova, e uma avaliação anterior passaria a citar critérios que a tela não mostra mais.
- **A rubrica gerada usa o contrato de FIXTURE (`rubrica@2`: `escala{min,max,ancoras}`), não o `escrever_rubrica@1`** (`escala_min`/`rotulo_min`), que é o formulário do anotador. Gerar no formato errado daria uma rubrica que a tela abre **sem âncora nenhuma** — funcionando, e medindo outra coisa. Por isso a validação cobra as duas PONTAS ancoradas.
- **O import NÃO cria tarefas.** Material levanta o teto do modo LIVRE (o catálogo passa a mostrar "Anotar este" habilitado); gerar tarefas em lote é do painel do admin (P5a).
- **`geracao/` é uma árvore própria, ao lado de `labeling/`**, e versionado é só o `manifest.json` — os lotes carregam o texto dos prompts e as respostas carregam o material gerado. `--geracao-dir` redireciona tudo (é assim que os testes rodam sem tocar em `geracao/`).
- **Medido no banco real** (miniatura, 2026-07-31): migração 3→4 preservando as 8 anotações humanas; `mat_0001` com 4 prompts (2 pt + 2 en, `arena140k`/`dolly`) → 4 rubricas + 8 respostas `importada`; `anot_0001` com 4 sintéticas (uma de cada alvo), recusada duas vezes (nota booleana, nota 7 num critério de 1..5, `familia_defeito` trocada) e aceita no retry sem mudar de estado. Cobertura do pool: `avaliar_rubrica` e `comparar_ab` de **0 → 4** (mais os 11 do pacote). Composição: 8 humanas + 4 sintéticas. No navegador, o catálogo do A/B passou a oferecer "Annotate this one" nos prompts com material (os demais seguem com "No material") e a tarefa abre com as **duas** colunas cegas; a fila da triagem foi de 3 para 7 e o alvo escondido não aparece no DOM nem em `/api/revisao/fila` nem em `/api/revisao/{id}`.
