# Campanha de rotulagem

Esta pasta guarda tudo da **destilação de rótulos**: em vez de pagar API, agentes Haiku do Claude Code rotulam uma amostra-semente de ~12.000 prompts (6k pt + 6k EN, estratificada por fonte × faixa de tamanho), e essa semente treina um classificador local (LogisticRegression sobre embeddings `multilingual-e5-small`) que rotula o resto do universo. A fonte única da taxonomia é `taxonomy.json` — nenhum eixo ou valor de enum existe fora dele, e todo rótulo produzido é validado contra ele antes de entrar em `merge`. Os eixos são `task_type`, `domain`, `quality`, `nsfw`; rótulos nativos de `no_robots` e `databricks-dolly-15k` (~25k itens) entram de graça via `mappings/`, com peso 0.5 e só no eixo `task_type`.

O trabalho é fatiado em lotes de 80 itens (77 novos + 3 itens de calibração ocultos, reamostrados do `batch_0000` de ouro revisado à mão), gravados em `batches/` como JSON, com `manifest.json` na raiz desta pasta servindo de livro-caixa: cada lote tem estado `pending → claimed → done` (mais `failed` na quarentena), escrito atomicamente, o que torna a campanha **retomável entre sessões** e segura para vários agentes em paralelo. A skill `.claude/skills/rotular-prompts/` executa o protocolo — reivindica lotes no manifest, dispara ~4 agentes Haiku em paralelo, valida estritamente (cobertura exata dos uids do lote, enums da taxonomia), tenta 1 retry e só então marca `done`. O portão de qualidade é o *agreement* nos 3 itens de calibração, medido em `task_type` e `domain` (6 comparações): abaixo de `[labeling] agreement_min` o lote volta sozinho para `pending`. Os lotes, os rótulos e a semente **não** são versionados (ver `.gitignore`); versionados são a taxonomia, os mapeamentos e o manifest.

## O ouro é público, e por isso não se reusa

`manifest.json` é versionado, e dentro dele estão os `gold_uids` de cada lote e os
rótulos revisados à mão. Num repositório público isso quer dizer que **o gabarito
desta campanha é legível por qualquer pessoa** — inclusive por quem viesse a
anotar para o projeto.

A escolha é deliberada e tem um preço declarado. Manter o manifest versionado é o
que permite auditar a campanha inteira: quem quiser conferir a alocação por
estrato, o reuso das vagas de ouro ou a nota de cada lote precisa do livro-caixa,
não de uma descrição dele. Esconder o gabarito depois de ele já ter estado no
histórico seria sigilo de fachada, porque histórico de git não se apaga.

A regra que compensa o preço é de operação, não de arquivo:

> **O ouro publicado nunca é reusado com anotadores externos.** Qualquer rodada
> com pessoas de fora sorteia ouro novo, de itens que não estão no manifest
> público, e a nota de calibração dela é medida só contra esse ouro novo.

Um gabarito que o anotador pode ler mede a leitura dele, não o julgamento. As
notas de agreement já registradas (82 lotes, média 0,913) foram produzidas por
agentes headless sem acesso ao manifest — a skill entrega ao agente apenas
`uid`, `lang` e `text`, e os três itens de calibração não carregam marca alguma
no arquivo. Elas continuam válidas como estão; o que não se pode fazer é
compará-las com uma rodada futura que tenha visto o gabarito.

## Layout

| Caminho | O que é | Versionado |
| --- | --- | --- |
| `taxonomy.json` | fonte única da taxonomia v1.0 (16 task_type + 16 domain + flags) | sim |
| `mappings/*.json` | categoria nativa da fonte → `task_type`, com peso; `null` = sem correspondência limpa | sim |
| `manifest.json` | livro-caixa: estado de cada lote, gold_uids, ouro, agreement, alocação | sim |
| `seed/seed.parquet`, `seed/strata.txt` | s07: os 12.000 sorteados e o relatório de estratos | não |
| `batches/batch_NNNN.json` | o lote como o agente o vê (uid, lang, text truncado) | não |
| `labels/batch_NNNN.jsonl` | a resposta validada de cada lote | não |

## Comandos

```powershell
$pf = "$env:USERPROFILE\.local\bin\uv.exe"
& $pf run pf make-seed                      # s07: semente + lotes + manifest
& $pf run pf labels status                  # painel da campanha
& $pf run pf labels next -n 4 --out-dir tmp # reivindica 4 lotes
& $pf run pf labels submit --batch batch_0007 --file resp.jsonl --model haiku
& $pf run pf labels gold --file pregold.jsonl   # importa a calibração revisada
& $pf run pf merge-labels                   # s08: final/seed_labels.parquet
```

Marcos: os lotes e a skill chegam no **M5** (piloto de 5 lotes), a campanha completa é o **M6** (≥95% dos lotes `done`, ≥11k rótulos, agreement ≥80%).
