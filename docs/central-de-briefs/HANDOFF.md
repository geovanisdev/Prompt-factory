# HANDOFF — o contexto para quem chega agora na Central de Briefs Pedagógicos

Este documento existe para uma sessão futura começar a trabalhar no módulo novo
sem esta conversa. Ele diz o que o projeto é, como as peças que o módulo vai
tocar funcionam HOJE (com arquivo e função nomeados), e onde o módulo se
encaixa em cada uma. O plano do módulo em si está ao lado, em `PLANO.md`.

## 1. A tese do projeto, em um parágrafo

O Prompt Factory é um banco de **prompts escritos por pessoas reais** — primeiro
turno de usuário em conversas com LLMs, nunca sintéticos — em pt-BR e inglês,
com **licença e atribuição por linha**. A tese: não usar prompt criado por IA
para treinar IA, e provar a proveniência de cada linha. Tudo no repositório
serve a essa afirmação: o corpus tem hoje **159.733 linhas** (`data/db/prompts.sqlite`),
cada uma com `license`, `commercial_ok` e `redistributable` vindos de
`config/sources.toml` (a única fonte da `attribution` do export), e o contrato
canônico das 27 colunas mora em `src/prompt_factory/schema.py`.

## 2. As duas metades

**Metade A — o corpus.** Pipeline `s01`–`s06` (`pf run`): normalização → idioma
→ PII → dedup exato → embeddings e5-small → dedup próximo →
`data/final/universe.parquet`. O s11 (`pf load-db`) carrega tudo num SQLite que
é **reconstruído do zero e trocado por swap de arquivo** — consequência que
atravessa todo o resto: nenhum id de linha do corpus é estável entre recargas;
só o `uid` é. A interface de curadoria (`pf serve`, porta 8765) só LÊ esse
banco. A taxonomia de classificação — 16 `task_type` + 16 `domain` — é
`labeling/taxonomy.json`, fonte única, e `task_type`/`domain` estão **nulos em
100% das linhas** (a campanha de rotulagem M6 está em curso no checkout
principal; é por isso que este trabalho roda numa worktree).

**Metade B — a Bancada** (`src/prompt_factory/annotate/`, `pf annotate`, porta
8766). Plataforma de anotação-portfólio, produto separado com banco próprio
(`data/db/annotate.sqlite`) que **guarda trabalho humano e não é regenerável**
— daí todo schema mudar por migração de reconstrução (`pf annotate migrate`,
`annotate/migracao.py`), nunca por `ALTER TABLE` nem por recriação. O corpus é
aberto **somente leitura**, sempre, e o cruzamento entre os dois bancos é por
`uid` em Python (nunca ATTACH, nunca rowid — `annotate/db.py`, docstring do
módulo).

## 3. A Bancada em cinco mecanismos (os que o módulo novo toca)

### 3.1 Vocabulário fechado e a máquina de estados como dado

Todo CHECK do DDL de `annotate/db.py` sai de uma tupla do mesmo módulo
(`PAPEIS`, `TIPOS_TAREFA`, `STATUS_CRIACAO`…), e o Pydantic valida contra a
mesma tupla — banco e API concordam por construção. A máquina de revisão é
DADO (`db.TRANSICOES`), não uma sequência de `if`. Duas lições de migração que
custaram caro e governam qualquer schema novo:

- `CREATE TABLE IF NOT EXISTS` **cria tabela nova de graça** num banco
  existente (foi assim que `briefs` nasceu na v6);
- mas **não reescreve um CHECK** nem acrescenta coluna em tabela que já existe
  — foi isso que custou a v4 (um valor a mais no CHECK de `rubricas.origem`) e
  a v6 (a coluna `anotacoes.versao_brief`). Hoje o schema está na **v6**
  (`db.SCHEMA_VERSION_ANOTACAO`).

### 3.2 Payloads versionados

O que o anotador produz viaja em `anotacoes.payload_json` com o contrato
gravado em `anotacoes.payload_schema` (ex.: `avaliar_rubrica@3`) — a versão
anda com o dado, não com a versão da app (`annotate/payloads.py`, `VERSOES`
por tipo). O mesmo padrão de "contrato dentro do dado" aparece nos arquivos:
`briefs.json` carrega `"schema": "briefs@1"`, os lotes da campanha de geração
carregam `"contrato": "geracao@1"` (`geracao.CONTRATO`). Pegadinha recorrente
(quatro aparições no repo): `bool` é subclasse de `int` em Python e o Pydantic
coage `true`→`1` antes de validador comum — todo campo numérico tem validador
`mode="before"` que recusa booleano.

### 3.3 Briefs e diretrizes — os dois eixos de instrução

`diretrizes` responde COMO se faz um tipo de tarefa; `briefs` responde O QUE é
este projeto (`annotate/briefs.py` + `fixtures/briefs.json`). São duas tabelas
versionadas porque os eixos mudam sozinhos, e cada anotação grava os ponteiros
(`versao_diretriz`, `versao_brief`). O brief tem cinco seções fixas
(`briefs.SECOES`) e um `escopo` que fica FORA da prosa porque é feito de **ids
da taxonomia do corpus** — id não se traduz. Os papéis de projeto que o arquivo
descreve são fechados (`briefs.PAPEIS_PROJETO = ("padrao", "demonstracao")`);
um projeto novo com brief próprio exige acrescentar um papel ali e no fixture
(trabalho de arquivo versionado, sem migração). Editar uma versão já publicada
é proibido (`briefs.semear` levanta) — mudou o texto, sobe a `versao`.

Regra do brief v2 vigente que importa diretamente ao módulo novo
(`fixtures/briefs.json`, seção `selecao` do projeto `padrao`): *"A prompt you
create must be something you would genuinely have sent, in your own words,
dedicated CC0. One borrowed line is enough to break the claim that every
prompt in this bank is human and traceable."*

### 3.4 O modo criar e o funil até o corpus

A 4ª vista do anotador deixa escrever um prompt livre (`annotate/criacoes.py`
+ `routes_criacoes.py`, tabela `criacoes`). O desenho, ponto a ponto:

- **A cadeia canônica é importada, nunca reescrita**: `criacoes.chave()` chama
  `textnorm.norm_display`/`norm_for_hash` e produz o `hash_norm` exatamente
  como o s01; `criacoes.uid_previsto()` chama `schema.make_uid`. Divergir em
  qualquer um falha **em silêncio** (aviso de duplicata que não dispara, badge
  que não acende) — por isso os testes comparam contra essas funções, nunca
  contra literais.
- **Aviso de duplicata é aviso** (201, nunca bloqueio): colidir com o corpus é
  o dedup funcionando.
- **Uma passagem de revisão, sem edição**: uma criação é um TEXTO; corrigi-lo
  e ingerir o resultado como "prompt escrito por gente" destruiria a
  proveniência. Recusa exige comentário; aprovação grava `uid_previsto` —
  calculado do **id da linha** (`make_uid("plataforma", str(id), …)`), não do
  texto, para que reingerir reproduza o mesmo uid.
- **Licença fixa**: `criacoes.LICENCA = "cc0-1.0"`, declarada no formulário. É
  a única licença que quem escreve pode conceder na hora em que escreve
  (comentário em `schema.LICENSE_POLICY`).
- **Funil**: `submetida → aprovada → exportada` (`db.STATUS_CRIACAO`);
  `exportada` é carimbada pelo `pf ingest plataforma` (P6,
  `ingest/plataforma.py`) DEPOIS de a linha existir em `data/raw/` — escreve
  primeiro, carimba depois. O `meta_json` do raw **não carrega texto livre**
  (o s03 só limpa PII de `text`), as sugestões de `task_type`/`domain` viajam
  como pista (meta), nunca como `native_category`. No dedup do corpus, o
  `cc0-1.0` fica **por último dentro da classe (True, True)** do
  `dedup.LICENSE_RANK`, para que um texto colado do corpus no formulário não
  substitua a linha original.
- A tabela `criacoes` já tem uma coluna `brief` — é o **contexto livre escrito
  pelo próprio autor** ("por que escrevi isto"), não confundir com o brief de
  projeto do P9 nem com o pedido pedagógico do módulo novo.

### 3.5 O padrão de campanha de geração (P4c) — o molde do módulo novo

`annotate/geracao.py` + `.claude/skills/gerar-material/SKILL.md`. É o desenho
que a destilação de pedidos deve copiar, e ele não se negocia:

- **O agente gerador é puro cômputo**: recebe o conteúdo do lote dentro do
  prompt de despacho, devolve um objeto JSON como texto, não escreve arquivo,
  não usa git, não roda `pf`, não lê banco. Tudo de que ele precisa está no
  arquivo do lote.
- **Só o fio principal grava, pela CLI** (`pf annotate gerar importar`), com
  **validação estrita antes de encostar no banco**. Falha de validação NÃO
  muda o estado (o lote segue `claimed`; retry dirigido é o caminho normal);
  o que muda o estado é a contagem (`[geracao] max_tentativas` → `failed`).
- **O manifest é o livro-caixa** (`geracao/manifest.json`): estados
  `pending/claimed/done/failed` com transições em
  `geracao.TRANSICOES` (frozenset), TTL de claim órfão
  (`varrer_orfaos`), id do lote contado pelo manifest e **nunca por
  `listdir`** (`proximo_id` — a disciplina do part-file do WildChat), escrita
  atômica (`labeling_io.escrever_json_atomico`), arquivo do lote gravado
  ANTES do manifest.
- **Árvore própria com texto gitignorado**: `geracao/lotes/**` e
  `geracao/respostas/**` ficam fora do git (carregam texto do corpus); só o
  `manifest.json` é versionado. `GeracaoPaths(raiz)` redireciona tudo para os
  testes rodarem em `tmp_path`.
- **Validação: erro exige certeza, aviso não.** Enum fora do vocabulário e
  cobertura de itens são erro; nome de defeito fora do catálogo é aviso (o
  catálogo é aberto); recusa por idioma só quando o detector aponta a outra
  língua com confiança acima do piso (`conferir_lingua` — a lição do s02).
- **A saída de ferramenta trunca em ~30k caracteres**, então lote viaja por
  arquivo (`--out-dir`), sempre.

### 3.6 Instrumentos e rubricas

`annotate/rubricas_modelo.py` + `fixtures/modelos_rubrica.json` são o banco de
modelos de instrumento (servido por `GET /api/modelos-rubrica`). A forma
canônica de uma rubrica é a de `rubrica@3`, e a normalização é ponto único
(`tarefas.normalizar_criterio` / `normalizar_pergunta`, chamadas na escrita E
na leitura — duas formas para a mesma coisa foi o defeito do
`routes_revisao:243`, que fazia uma rubrica 1..5 aceitar nota 9 no servidor).
Rubricas materializadas vivem na tabela `rubricas` com `origem` em
`db.ORIGENS_RUBRICA = ("fixture", "anotacao", "importada")`; a origem
`anotacao` é exatamente "rubrica escrita por humano na plataforma,
materializada na aprovação do revisor". A escala real de cada critério é a da
rubrica, e quem a confere é `tarefas.erro_contra_a_rubrica` — função única
chamada pela rota de submissão e pelo import da campanha.

## 4. O material bruto que o módulo vai destilar

A pasta local do material (fora do repositório; `[pedidos] material_dir` ou `PF_MATERIAL_DIR`): **35 arquivos .txt,
52.805.785 bytes (~52,8 MB), 1.242.152 linhas**, extração linear de PDF de
apostilas do **PNLD 2026** (Ensino Médio), todas na edição **Manual do
Professor** — ou seja, com as respostas do professor intercaladas no texto.
Três coleções, três selos do mesmo grupo editorial (anonimizados aqui como Editora A, B e C — o repositório é público e os nomes não agregam sinal técnico):

| coleção | editora | arquivos |
| --- | --- | --- |
| Ciência Viva | Editora B | 3 |
| Do Seu Jeito | Editora A | 16 |
| Identidade / Síntesis | Editora C | 16 |

Dezoito disciplinas/frentes (Biologia, Física, Química, Matemática, LP,
Redação, História, Geografia, Filosofia, Sociologia, Arte, Espanhol, Ed.
Física, Ed. Digital, projetos integradores…), com códigos de habilidade da
BNCC (`EM13CNT201`, `EM13CHS101`…) marcando cada capítulo. **Copyright
integral e explícito**: página de créditos completa, CIP, ISBN (Filosofia,
Editora A, 2024), autores nomeados, e o carimbo "PNLD
EM 2026–2029 — MATERIAL DE DIVULGAÇÃO". Os livros ainda **citam terceiros
dentro deles** (tradução de Nietzsche da Companhia das Letras, Vernant/Difel,
questões Fuvest/UFPR) — um recorte pode cair em texto que nem é da editora. As
consequências disso para a licença do prompt criado são a seção 6 do
`PLANO.md`; a decisão curta: **o recorte é insumo local de trabalho e nunca
entra no prompt, no corpus, no git ou em export**.

## 5. O módulo novo, e onde ele se encaixa

**Central de Briefs Pedagógicos** (nome provisório): um banco de **pedidos de
prompt** destilados do material didático. Cada pedido = recorte de material
bruto + `task_type` esperado (id da taxonomia) + tema + meta pedagógica +
papel (professor | aluno). O anotador que trabalha um pedido entrega três
coisas: (a) o **prompt escrito humanamente** a partir das instruções; (b) a
**rubrica** para avaliar respostas a esse prompt; (c) uma **resposta-gold
descritiva** — o que uma resposta precisaria conter, como referência de
revisão, não a resposta ideal redigida. É a tríade prompt humano + instrumento
+ referência que datasets de instruction-following pedem — produzida pela
mesma plataforma que já faz QC em duas passagens.

Encaixe, peça por peça:

| peça existente | como o módulo a usa |
| --- | --- |
| modo criar (`criacoes.py`, §3.4) | o pedido é o INSUMO da 4ª vista: aparece no topo do formulário (como o painel de brief do P9) e a criação passa a apontar para ele; o funil `submetida→aprovada→exportada` e o `uid_previsto` **não mudam** |
| campanha P4c (`geracao.py`, §3.5) | a destilação é uma campanha irmã: CLI `preparar`/`importar`, manifest próprio, agente puro cômputo que recebe **janelas do material dentro do lote** e propõe pedidos; validação estrita (recorte verbatim conferível por substring, `task_type` no enum, papel fechado, caps) |
| taxonomia (`labeling/taxonomy.json`) | `task_type` do pedido é id da taxonomia, validado pela mesma projeção que o modo criar já usa (`tarefas._vocabulario_taxonomia`); referência por id, nunca cópia das classes |
| briefs (`briefs.py`, §3.3) | o trabalho pedagógico é um PROJETO novo com brief próprio (papel novo em `PAPEIS_PROJETO` + fixture) — escopo, convenção de língua e regra de autoria declarados lá, versionados |
| rubricas (`rubricas`/`normalizar_criterio`, §3.6) | a rubrica entregue na criação é validada na forma `rubrica@3` e **materializada na aprovação**, apontando para o `uid_previsto` — o prompt já entra no corpus podendo sustentar `avaliar_rubrica`/`sft_resposta` no pool |
| migração (`migracao.py`, §3.1) | tabela nova `pedidos` sai de graça; as colunas novas em `criacoes` (`pedido_id`, `material_json`) custam a migração **v7** — e o valor novo no CHECK de `ORIGENS_RUBRICA` pega carona nela |
| licença (`schema.LICENSE_POLICY`, brief v2) | CC0 mantido e a regra "own words" vira validação: o prompt REFERENCIA o material, nunca o reproduz (análise completa na §6 do `PLANO.md`) |

O que o módulo **não** faz: não escreve no corpus (a Bancada continua leitora;
quem ingere é `pf ingest plataforma`), não versiona nem exporta o material da
editora, não cria segundo caminho de revisão para o texto do prompt (a decisão
"uma passagem, sem edição" do P4 continua valendo).

## 6. Por onde começar

1. Ler `PLANO.md` (ao lado) — fluxo, desenho de dados, campanha, licença,
   marcos e as perguntas abertas ao dono.
2. Responder as perguntas abertas com o dono ANTES do marco F1 (duas delas
   mudam schema).
3. O ambiente: `uv` fora do PATH (`& "$env:USERPROFILE\.local\bin\uv.exe" run …`),
   escrita de dados sempre de dentro do Python em UTF-8 (PowerShell corrompe
   redirecionamento), `data/` nunca commitado, testes com `pytest -q`. Detalhe
   completo em `CLAUDE.md`, seção "Pegadinhas".
