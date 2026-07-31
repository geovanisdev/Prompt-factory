# Prompt Factory

Banco de **prompts escritos por pessoas reais** — o primeiro turno de usuário em conversas com LLMs, nunca texto sintético — em **português brasileiro e inglês**, deduplicado, limpo de PII e navegável numa interface local. Serve para curar coleções e exportar JSONL/CSV **com licença e atribuição em cada linha**, para alimentar plataformas de data annotation.

Tudo roda **offline depois da ingestão**: a interface é um SQLite mais um arquivo HTML sem build, sem framework e sem nenhuma referência a host externo.

```
144.754 prompts · 106.039 en · 38.715 pt · 9 fontes abertas · 6 licenças
prompts de 1 a 981.656 caracteres · 440 MB de texto
```

---

## Índice

1. [O que tem dentro](#o-que-tem-dentro) · 2. [Fontes e licenças](#fontes-e-licenças) · 3. [Instalação](#instalação) · 4. [Caminho rápido (5 min)](#caminho-rápido-5-min) · 5. [Reprodução completa](#reprodução-completa-em-clone-limpo) · 6. [A interface](#a-interface) · 7. [Export](#export-e-licenças) · 8. [Estado dos marcos](#estado-dos-marcos) · 9. [Estrutura](#estrutura-do-repositório)

---

## O que tem dentro

O funil, medido no run que produziu o banco atual:

| etapa | linhas | o que caiu |
| --- | ---: | --- |
| `data/raw/*.parquet` (9 fontes) | **214.947** | — |
| s01 normalize | 214.946 | 1 `uid` repetido dentro do run |
| s02 idioma + variante | 201.704 | **13.242** que não eram pt nem en (a `language` das fontes erra muito) |
| s03 PII | 201.704 | nada sai; 1.088 linhas foram *reescritas* |
| s04 dedup exato | 189.087 | **12.617** cópias byte-a-byte do texto normalizado |
| s05 embeddings | 189.087 | — (só produz `emb/embeddings.f16.npy`) |
| s06 dedup próximo | **144.754** | **44.333** quase-duplicatas (cosseno ≥ 0,985 **e** Jaccard ≥ 0,65 **e** razão de tamanho ≤ 2) |

Distribuição do universo:

* **idioma**: en 106.039 · pt 38.715
* **variante do pt**: `pt-indef` 22.730 · `pt-BR` 14.698 · `pt-PT` 1.287 — `pt-indef` é a **maioria e não é erro**: significa que o texto não tem marca dialetal suficiente para decidir ("como fazer um bolo?" não é brasileiro nem português).
* **fonte**: wildchat_en 58.203 · wildchat_pt 31.952 · hh_rlhf 14.873 · dolly 14.399 · no_robots 9.948 · prism 7.561 · aya 6.287 · arena140k 1.259 · oasst 272
* **4.547 linhas ainda têm cópia exata no corpus** (`n_exact_dups > 0`): são os robôs que sobreviveram ao dedup por diferirem num detalhe. A interface mostra `×N` nelas e o filtro `max_dups=0` derruba a família inteira.
* **1.088 linhas tiveram PII substituída** por marcadores.
* `task_type`, `domain`, `quality` e `nsfw` estão **NULL em 100% das linhas**: a campanha de rotulagem (M6/M7) ainda não rodou. É o estado esperado, não defeito — a interface diz "rotulagem PENDENTE" e desabilita esses filtros com a explicação, em vez de escondê-los.

---

## Fontes e licenças

Cada prompt carrega a licença **até a linha** no banco (`license`, `commercial_ok`, `redistributable`); a atribuição por extenso vem de `config/sources.toml` no momento do export.

| fonte | dataset | idioma | licença | comercial | redistribuível | no universo |
| --- | --- | --- | --- | :-: | :-: | ---: |
| `wildchat_pt` | `allenai/WildChat-4.8M` | pt | ODC-BY-1.0 | sim | sim | 31.952 |
| `aya` | `CohereLabs/aya_dataset` | pt | Apache-2.0 | sim | sim | 6.287 |
| `arena140k` | `lmarena-ai/arena-human-preference-140k` | pt | CC-BY-4.0 | sim | sim | 1.259 |
| `oasst` | `OpenAssistant/oasst1`+`oasst2` | pt | Apache-2.0 | sim | sim | 272 |
| `wildchat_en` | `allenai/WildChat-4.8M` | en | ODC-BY-1.0 | sim | sim | 58.203 |
| `hh_rlhf` | `Anthropic/hh-rlhf` (só *helpful*) | en | MIT | sim | sim | 14.873 |
| `dolly` | `databricks/databricks-dolly-15k` | en | **CC-BY-SA-3.0** | sim | sim | 14.399 |
| `no_robots` | `HuggingFaceH4/no_robots` | en | **CC-BY-NC-4.0** | **não** | sim | 9.948 |
| `prism` | `HannahRoseKirk/prism-alignment` | en | CC-BY-4.0 | sim | sim | 7.561 |
| `lmsys` *(desligada)* | `lmsys/lmsys-chat-1m` | pt | LMSYS-1M | **não** | **não** | 0 |

**O que isso implica no export** — e é a razão de o projeto ser construído do jeito que é:

* **`redistributable = false` fica FORA do arquivo por padrão** e o manifesto conta quantas linhas foram excluídas. Dá para incluir (`include_nonredistributable`) para uso local, e aí o manifesto carimba um `WARNING: não publique`.
* **`commercial_ok = false` NÃO é excluído** — é contabilizado à parte. A licença não comercial não impede o uso, impede *um certo* uso, e quem decide é quem exporta. Hoje são as 9.948 linhas do `no_robots`.
* **CC-BY-SA-3.0 é viral**: as 14.399 linhas do `dolly` fazem o derivado herdar a licença. A interface pinta essa classe com cor própria (`viral`) para a decisão ser tomada antes, não depois.
* **Toda linha exportada leva `attribution`.** ODC-BY, CC-BY e CC-BY-SA exigem crédito a cada uso; sem esse campo o arquivo simplesmente não cumpre a licença. Ele não é coluna do banco: é resolvido de `config/sources.toml` na hora de escrever.

`lmsys` é o maior pool de português que existe, mas é *gated* no Hub e a licença proíbe redistribuição. `pf ingest lmsys` já existe: com `enabled = false` ele não faz rede nenhuma, imprime o passo a passo para ligar e sai com código 2.

---

## Instalação

### Pré-requisitos reais

* **Python 3.12** (o `pyproject` fixa `>=3.12,<3.13`).
* **[uv](https://docs.astral.sh/uv/)**. Nesta máquina ele está em `C:\Users\gigio\.local\bin\uv.exe` e **não entra no PATH** até você abrir um terminal novo — daí o caminho completo em todos os comandos abaixo. Se `uv` já funciona no seu shell, ignore o prefixo.
* **~8 GB de disco livre**: `data/` fica com 3,6 GB (raw 701 MB · interim 1,8 GB · final 237 MB · emb 250 MB · db 698 MB), mais ~2,5 GB de cache do HuggingFace para as fontes pequenas (só o `arena140k` são 1,6 GB) e ~470 MB do modelo de embeddings.
* **Rede** para a ingestão — e paciência: os dois passes do WildChat baixam ~9–11,5 GB **cada**, em *streaming*, sem guardar nada em disco. Depois disso nada mais sai da máquina.

```powershell
git clone <url> Prompt-factory
cd Prompt-factory
& "$env:USERPROFILE\.local\bin\uv.exe" sync          # cria o .venv a partir do uv.lock
& "$env:USERPROFILE\.local\bin\uv.exe" run pf --help # lista os comandos
& "$env:USERPROFILE\.local\bin\uv.exe" run pytest -q # 628 testes, ~48 s
& "$env:USERPROFILE\.local\bin\uv.exe" run ruff check .
```

### Três coisas que quebram e não são culpa sua

1. **TLS interceptado (proxy/antivírus).** Se `uv sync` falhar com `invalid peer certificate: UnknownIssuer`, é isso: um middlebox reassina o tráfego e a raiz dele só existe no armazenamento de certificados do Windows. Já está resolvido no repositório — `[tool.uv] system-certs = true` no `pyproject.toml` faz o uv confiar no trust store do sistema. **Nunca** troque isso por `--allow-insecure-host`, que desliga a verificação de verdade.
2. **O mesmo TLS atinge o Python.** `requests`/`huggingface_hub` usam o bundle do `certifi`, que não conhece essa raiz, e todo download do Hub morre em `CERTIFICATE_VERIFY_FAILED`. A CLI resolve sozinha: `pf` gera `data/system-ca.pem` (certifi + raízes do Windows) e exporta `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`. **A verificação continua ligada.** Script solto que não passa pelo `pf` precisa exportar `REQUESTS_CA_BUNDLE` na mão.
3. **`HF_HOME` do ambiente vence o `settings.toml`.** O `config/settings.toml` pede `G:/hf-cache`, mas a aplicação usa `setdefault`: se a sua máquina já define `HF_HOME`, é para lá que o cache vai. Confira com `echo $env:HF_HOME` antes de culpar o disco errado.

---

## Caminho rápido (5 min)

Para ver a ferramenta funcionando sem esperar as ~4 horas do corpus inteiro. `--max-rows` é aplicado **por fonte** e só pelo s01.

```powershell
$uv = "$env:USERPROFILE\.local\bin\uv.exe"

& $uv run pf ingest aya --max-rows 2000        # uma fonte pequena, ~1 min
& $uv run pf ingest oasst
& $uv run pf ingest no_robots --max-rows 2000
& $uv run pf report raw                        # confere o que caiu em data/raw/

& $uv run pf run --max-rows 2000               # s01..s06 em miniatura
& $uv run pf report universe                   # distribuições do universo

& $uv run pf load-db --allow-unlabeled-pct 100 # constrói o SQLite (sem rótulos, de propósito)
& $uv run pf serve                             # http://127.0.0.1:8765
```

Menor ainda, sem tocar em `data/` nem na rede — a pipeline inteira sobre dados de brinquedo, num diretório temporário:

```powershell
pwsh -File scripts/smoke_test.ps1
```

---

## Reprodução completa (em clone limpo)

Na ordem. Os tempos são os **medidos** nesta máquina (Windows 11, CPU, disco em `G:`), não estimativas.

| # | comando | tempo real | produz |
| --- | --- | ---: | --- |
| 1 | `pf ingest` | ~5 min | as 7 fontes pequenas em `data/raw/*.parquet` |
| 2 | `pf ingest wildchat-pt` | **~45 min** | `raw/wildchat_pt.parquet` — 3.199.860 linhas varridas → 55.959 |
| 3 | `pf ingest wildchat-en` | **~45 min** | `raw/wildchat_en_pool.parquet` — 158.660 linhas |
| 4 | `pf ingest wildchat-en --downsample` | < 1 min | `raw/wildchat_en.parquet` — 100.000 exatos (sem rede) |
| 5 | `pf report raw` | segundos | conferência: contagens dentro das faixas de `sources.toml` |
| 6 | `pf run s01-s04` | ~10 min | `interim/dedup1.parquet` — 189.087 linhas |
| 7 | `pf run s05` | **120 min** | `emb/embeddings.f16.npy` + `emb/uids.txt` |
| 8 | `pf run s06` | **47 min** | `final/universe.parquet` — **144.754** linhas + `emb/universe.f16.npy` |
| 9 | `pf report universe` · `pf report dedup-sample` | segundos | distribuições + 50 pares de near-dup para revisão humana |
| 10 | `pf load-db --allow-unlabeled-pct 100` | **36 s** | `data/db/prompts.sqlite` (698 MB) |
| 11 | `pf serve` | sobe em ~5 s | http://127.0.0.1:8765 |

Total: **~4 h 15 min**, das quais 3 h 30 são os passos 2, 3, 7 e 8.

`pf run` sozinho encadeia s01..s06; a tabela separa os passos 6–8 porque o **s05 é o longo — e é retomável**: ele mantém um memmap pré-alocado e um `emb/progress.json`, então um `Ctrl+C` perde no máximo um bloco de 2.048 textos. Os dois passes do WildChat também retomam: rodar o mesmo comando continua de onde parou, e `--restart` é que recomeça do zero. `scripts/run_pipeline.ps1` faz os passos 6 a 9 de uma vez.

O `--allow-unlabeled-pct 100` do passo 10 **não é gambiarra**: por padrão a carga recusa um banco com mais de 1% das linhas sem rótulo, justamente porque um banco carregado com o parquet de rótulos ausente fica plausível, abre na interface e só denuncia o erro semanas depois. Antes do M7 a falta de rótulo é intencional, e a flag é como se diz isso em voz alta.

### Por que o s05 leva 2 horas

Ele embeda 189.087 textos com o `intfloat/multilingual-e5-small` em CPU, a ~16 textos/s. O gargalo é o *forward* de 512 tokens, não a tokenização — e o corpus tem prompts de até 981.656 caracteres (cortados em 3.000 antes de tokenizar, senão a vazão despenca). Com GPU isso cai para minutos e o código não muda; o `.npy` resultante, porém, **não é byte-idêntico** entre CPU e GPU.

### O que não precisa ser refeito

`data/` inteiro é regenerável e está no `.gitignore` (só os `.gitkeep` passam) — Parquet, `.sqlite`, `.npy` e exports são artefatos grandes e reproduzíveis. O `uv.lock`, ao contrário, **é** versionado: é ele que garante que o clone de amanhã instale exatamente o que rodou aqui.

---

## A interface

```powershell
& "$env:USERPROFILE\.local\bin\uv.exe" run pf serve
# http://127.0.0.1:8765   ·   contrato da API sempre atual em /docs
& "$env:USERPROFILE\.local\bin\uv.exe" run pf serve --port 8799 --db outro.sqlite
```

Ela **só lê** o SQLite (mais os `.npy` da busca semântica) e edita o que o usuário editar; não roda estágio nenhum. Se o banco não existir, `pf serve` **recusa subir** e imprime a linha de comando do conserto — um servidor que sobe e dá 500 em tudo enterra a mensagem útil num traceback por requisição.

Três colunas: **facetas** · **lista** · **mesa** (seleção, coleção ativa, export). Escuro por padrão, claro no botão `tema`. O cromo é sans e **o texto do prompt é serifado**: o conteúdo é o produto, não pode ter a mesma voz da ferramenta.

Primeiro paint contra o banco real: **292 ms** a frio, **34 ms** com o cache quente.

### As duas buscas

| | **texto** (padrão) | **sentido** |
| --- | --- | --- |
| acha | prompts que **contêm** as palavras | prompts que **falam da mesma coisa** |
| como | FTS5 com `remove_diacritics 2` — `coracao` acha `coração` | cosseno entre embeddings e5, top-k exato |
| no card | o trecho com o termo em destaque | a similaridade + barra de proximidade |
| ordem | relevância (bm25), mais novo, mais curto, mais duplicado, aleatório… | só proximidade — e sem paginação |
| custo | 24 ms num termo raro; 140 ms num termo com 14 mil acertos | **~87 ms**; a **primeira** consulta da sessão leva **~16 s** |

O seletor `texto | sentido` fica colado à barra de busca (atalho `m`). **Os filtros da barra lateral continuam valendo nos dois modos** — o que muda é que a busca por sentido **ordena, não recorta**: o conjunto continua sendo o do filtro, e por isso "adicionar os N do filtro" e o export levam o conjunto inteiro, não os 50 vizinhos que estão na tela. A interface avisa isso na hora de fazer.

Os ~16 s da primeira busca por sentido são o carregamento do modelo (10,6 s de import do torch + 4,7 s do e5) mais 0,4 s para converter a matriz de 212 MiB. Por isso o aquecimento **começa no clique do modo**, roda numa thread e a tela mostra uma faixa explicando — em vez de travar sem dizer nada. Sem `data/emb/universe.f16.npy` o modo avisa que falta rodar o s05/s06 e a busca textual continua intacta.

Exemplo do que "sentido" quer dizer: buscar **`pedir aumento pro chefe`** traz `nota para pedir aumento` em primeiro, e logo abaixo `faça uma petição trabalhista` e `faça uma proposta de emprego` — que não têm uma palavra em comum com a consulta.

### Sinais que confundem quem chega agora

* **`pt-indef` é a maioria do português e não é erro.** Quer dizer "o texto não tem marca dialetal suficiente para decidir".
* **`×120` num card** quer dizer que aquele texto aparece 120 vezes no corpus. É automação, não linguagem — ~34% do português do WildChat é UM template só. O filtro **"só originais"** (`max_dups=0`) derruba a família inteira de uma vez.
* **"conversa colada"** marca prompts que trazem `User:`/`Assistant:` dentro do próprio texto (~15% do wildchat_pt). O linguista precisa ver isso antes de exportar.
* **Filtrar por qualidade ESCONDE os não rotulados** (`NULL >= 1` é falso) — hoje, 100% do banco. Já "esconder NSFW" **mantém** os não rotulados de propósito: `null` é "não sabemos", `false` é "sabemos que não".
* **Uma faceta filtrada soma MAIS que o total.** Cada dimensão é contada com o filtro dela mesma removido; sem isso, marcar "pt" zeraria "en" e você ficaria preso no primeiro clique.

### Teclado

`/` busca · `j`/`k` navega · `espaço` marca · `Shift+J/K` estende a seleção · `a` marca a página · `x` abre o texto · `Enter` abre o item · `e` edita · `y` copia · `c` joga na coleção · `n`/`p` páginas · `r` fila de revisão · `m` alterna texto ⇄ sentido · `?` a folha inteira.

### Editar

Editar o texto guarda o original ao lado (`text_original`), marca `edited` e reindexa o FTS; o botão `↺` desfaz. Editar um rótulo carimba `label_method = 'manual'`, tira o item da fila de revisão e **não** mexe no texto nem no índice. A qualidade vai de **1 a 3** (1 ruim, 2 usável, 3 bom) — é o que a taxonomia define, não 1 a 5.

---

## Export e licenças

Pela interface (`abrir exportação…`) ou pela linha de comando — os dois usam o mesmo código, o mesmo objeto de filtro e escrevem o mesmo manifesto:

```powershell
& $uv run pf export --format jsonl                          # o universo inteiro
& $uv run pf export --collection "curadoria" --format csv
& $uv run pf export --lang pt --commercial-only --name recorte-pt
```

Saem dois arquivos em `data/exports/`: o dado e o `<nome>.manifest.json`, com `sha256`, `row_count` **contado no laço** (não um `count(*)` prévio), contagens por fonte/licença/idioma, quantas linhas ficaram de fora por não serem redistribuíveis, quantas são não comerciais, e as **atribuições exigidas**. O arquivo é escrito em disco e só então servido — o `sha256` do manifesto é o do arquivo que você baixa.

Duas armadilhas do CSV, ambas medidas:

* o módulo `csv` do Python **recusa ler** campo acima de 131.072 caracteres, e este corpus tem prompts de ~1 milhão. **O arquivo está correto** — quem precisa de ajuste é o leitor: `csv.field_size_limit(10**7)`. O manifesto carimba `NOTA_CSV` quando isso vai acontecer, senão a conclusão natural é que o export saiu corrompido.
* o Excel corta em 32.767 caracteres por célula. Para texto longo, **JSONL**.

E uma regra: CSV **tem** de ser lido com um parser de CSV. Os prompts têm vírgula, aspas e quebra de linha; `split(",")` destrói o arquivo.

---

## Estado dos marcos

| marco | o que é | estado |
| --- | --- | --- |
| M0–M1 | scaffold, schema canônico de 27 colunas, SQLite + FTS5 sem acento | ✅ |
| M2 | ingesters das 7 fontes pequenas | ✅ |
| M3 | WildChat em streaming com checkpoint/resume: pt, pool en e downsample | ✅ |
| M4 | s01–s06: normalize → idioma/variante → PII → dedup exato → embeddings → dedup próximo | ✅ **144.754 linhas** |
| M5 | infraestrutura da campanha de rotulagem (s07 semente + lotes, s08 merge) | ✅ pronta — **a campanha não rodou** |
| M6 | a campanha de rotulagem propriamente dita | ⏳ **pendente** |
| M7 | s09 treino + s10 aplicação do classificador | ⏳ **stub** (`pf train`/`pf apply` saem com código 2) |
| M8 | s11 carga bulk no SQLite com rebuild do FTS e swap de arquivo | ✅ |
| M9 | interface FastAPI + página única + export com manifesto | ✅ |
| M10 | busca semântica + este README | ✅ |

### Pendente de **decisão humana** (não de código)

1. **Os limiares do near-dup.** O s06 colapsou 44.333 linhas com `near_cosine = 0.985`, `near_jaccard = 0.65` e `near_len_ratio = 2.0`. Aceitar esses números é uma escolha, não um resultado: apertar recupera texto legítimo, afrouxar limpa mais robô. `pf report dedup-sample` grava `data/final/dedup_sample_50.txt` com 50 pares reais **exatamente para essa leitura**. Os limiares moram em `config/settings.toml`, nunca no código — mudá-los é replayar o s06.
2. **A calibração da rotulagem.** `pf make-seed` já sorteou os 12.000 itens da semente (6k pt + 6k en, estratificados por fonte × faixa de tamanho) e fatiou em **155 lotes**. O `labeling/manifest.json` está com os 155 em `pending` e a calibração (`batch_0000`, 100 itens) em `gold_pending`. O próximo passo é humano: revisar esses 100 itens à mão e importar com `pf labels gold`. Sem esse ouro não há como medir a concordância dos agentes — e um `agreement = None` ("não medido") é deliberadamente diferente de `0.0` ("errou tudo").
3. **A taxonomia v1.1.** `task_type` e `domain` não têm `CHECK` no DDL justamente para poderem evoluir sem migração. A v1.0 tem 16 tarefas e 16 domínios, em `labeling/taxonomy.json`.

### Trabalho futuro documentado (não implementado)

* Perfil de export **Label Studio** — o `export.REGISTRY` já separa `profile` de `container` para isso caber sem migração de schema; falta decidir o `<View>`.
* Coluna `is_template` por clustering de prefixo, que resolveria de uma vez os ~34% de automação do português (exige um s13 e recarregar o banco).
* `lmsys` opt-in, com a garantia de que o export nunca inclui `redistributable = 0`.
* Histórico de edições — hoje existe só o par texto/original.

---

## Estrutura do repositório

```
config/
  settings.toml     TODOS os thresholds (nunca hardcoded — é o que torna os estágios replayáveis)
  sources.toml      licença, atribuição e faixa esperada por fonte
                    (a ORDEM das seções é o desempate do dedup)
labeling/
  taxonomy.json     fonte única das 16 tarefas + 16 domínios
  mappings/         categoria nativa da fonte → task_type
  manifest.json     estado da campanha (lotes, ouro, tentativas)
src/prompt_factory/
  schema.py         contrato canônico das 27 colunas
  ingest/           um módulo por fonte + o contrato de 12 colunas do raw
  stages/           s01..s08 e s11 — funções puras arquivo→arquivo
  db.py             DDL, FTS5, índices, conexão
  embedder.py       o ÚNICO caminho para o espaço vetorial (o prefixo `query: ` mora nele)
  export.py         formatos, manifesto, política de licença
  app/              FastAPI: queries.py (todo o SQL de leitura), semantic.py,
                    presenters.py e static/index.html (a interface inteira, um arquivo)
  cli.py            entrypoint `pf`
scripts/            smoke_test.ps1, run_pipeline.ps1
tests/              628 testes, ~48 s
data/               gitignorado; regenerável
```

Cinco linhas de arquitetura:

1. Os estágios são **funções puras arquivo→arquivo**; cada um lê e escreve **Parquet imutável**, então tudo é retomável e replayável.
2. `raw/{fonte}.parquet` → s01 → s02 → s03 → s04 → s05 → s06 → `final/universe.parquet`.
3. s07 amostra-semente → campanha de rotulagem por agentes → s08 merge → s09 treino → s10 aplica com limiares de confiança.
4. s11 carrega o universo num **SQLite** (WAL + FTS5), construído **ao lado** e trocado por `os.replace` — o servidor pode estar lendo o arquivo antigo enquanto o novo é montado.
5. A app toca **apenas** o SQLite (mais os `.npy`, por mmap, na busca semântica) e serve um `index.html` único, sem build.

**Nunca abra um `.parquet` com `cat`/`Get-Content`**: é binário. Use `pf report <arquivo>`.

Decisões de engenharia, medições e as dezenas de pegadinhas descobertas durante a construção estão em **[`CLAUDE.md`](CLAUDE.md)** — aquele é o documento de quem vai *mexer* no código; este é o de quem vai *usar*.
