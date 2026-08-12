---
name: rotular-prompts
description: Orquestra a campanha de rotulagem-semente do Prompt Factory. Reivindica lotes com `pf labels`, despacha rotuladores Haiku headless (`rodar_lote.py` → `claude -p`, fora do contexto do maestro) como puro cômputo, valida e grava tudo pela CLI, e retoma a campanha entre sessões pelo manifest. Use quando o usuário pedir para rotular prompts, rodar ou continuar a campanha de rotulagem, ou mencionar lotes, labels, calibração ou agreement do Prompt Factory.
---

# Campanha de rotulagem-semente (M5/M6)

Você é o **maestro**. Os rotuladores rotulam; você opera o estado. Divisão de
trabalho, e ela não se negocia:

| Quem | Faz | NÃO faz |
| --- | --- | --- |
| Rotulador Haiku (`claude -p` headless; subagente Task só no fallback) | recebe o despacho **inteiro no prompt**, devolve JSONL **como texto** | não escreve arquivo, não usa Bash, não lê o repo, não usa ferramenta nenhuma |
| Você (maestro) | claim, disparo do runner, submit, portão de agreement | não edita `manifest.json` nem `*.jsonl` à mão; **não lê o JSON dos lotes** |

## Invariantes (nunca viole)

1. **O rotulador é puro cômputo.** Todo o contexto de que ele precisa cabe no
   prompt de despacho. No caminho headless isso é estrutural (cwd vazio +
   ferramentas proibidas por flag — o gabarito do manifest fica inalcançável);
   no fallback por subagente é instrucional, e você confere `tool_uses: 0`.
2. **Toda escrita de estado passa por `pf labels`.** É lá que moram a validação
   estrita e a escrita atômica do manifest. Editar o manifest à mão é editar o
   journal de um banco de dados à mão: funciona até a primeira vez que não
   funciona.
3. **O manifest é a única fonte de estado.** Comece SEMPRE por
   `pf labels status`. Nada do que você lembra entre sessões conta.
4. **Um maestro por vez.** O claim é lock otimista via manifest atômico. Se
   suspeitar de outra sessão ativa, pergunte ao usuário antes de continuar.
5. **Haiku é o modelo padrão.** Escale um lote para Sonnet só depois de ele
   falhar 2 vezes no Haiku.
6. **Nunca redirecione com `>` no PowerShell** — grava UTF-16/BOM e quebra
   acentuação e JSONL (pegadinha documentada no CLAUDE.md). Salve respostas de
   agente com a ferramenta Write, em UTF-8, no scratchpad da sessão.

## Comandos base

O `uv` está fora do PATH desta máquina. Defina uma vez por sessão:

```powershell
$pf = "$env:USERPROFILE\.local\bin\uv.exe"
& $pf run pf labels status
```

## Passo 1 — Diagnóstico

Rode `& $pf run pf labels status` e mostre a tabela ao usuário.

- Manifest ausente? `& $pf run pf make-seed` (exige `data/final/universe.parquet`;
  faltando, **PARE** e avise que o M4 vem antes). Nunca use `--force` sem
  confirmação explícita: ele apaga a campanha inteira.
- Calibração ainda `gold_pending` → Passo 2.
- Calibração em `gold` e há lotes `pending` → Passo 3.

## Passo 2 — Calibração (parada crítica com revisão humana)

Os 100 itens do `batch_0000` viram o gabarito da campanha inteira: 3 deles vão
escondidos dentro de cada lote comum, e é a concordância com eles que aprova ou
reprova o trabalho dos agentes. Errar aqui contamina tudo.

1. `& $pf run pf labels next --batch batch_0000 --out <scratchpad>\cal.json`
2. Rode **3 passes independentes do runner, em paralelo**, sobre o mesmo lote:
   `rodar_lote.py <scratchpad>\cal.json --saida <scratchpad>\cal_N.jsonl`
   (N = 1..3; o `--saida` evita que um passe sobrescreva o outro).
3. Confira que os 3 arquivos têm 100 linhas cada.
4. Consolide por maioria simples, item a item, e escreva o pré-gold em
   `<scratchpad>\pregold.jsonl` (100 linhas). Empate triplo: fique com a
   resposta do primeiro arquivo e acrescente `"flag":"unsure"`.
5. **PARE.** Diga ao usuário que o pré-gold está pronto para revisão, aponte o
   caminho e **não prossiga sem confirmação humana**. Este é o único ponto da
   campanha em que um humano precisa olhar item a item.
6. Confirmado: `& $pf run pf labels gold --file <scratchpad>\pregold.jsonl`.
   O comando recalcula o agreement de qualquer lote já concluído antes do ouro.

## Passo 3 — Campanha (loop de ondas)

O rotulador de um lote é um **processo `claude -p` headless** — `rodar_lote.py`,
nesta pasta — e não um subagente Task. Dois motivos, os dois estruturais: o
texto dos 80 itens **nunca entra no contexto do maestro** (~40k tokens/lote
economizados; era o que limitava a sessão a 4–5 lotes), e o processo roda com
cwd num diretório temporário vazio e todas as ferramentas proibidas por flag —
o gabarito em claro do manifest fica fora de alcance **por construção**, não
por instrução.

Enquanto houver `pending` e houver sessão/cota:

1. `& $pf run pf labels next -n 4 --out-dir <scratchpad>` — reivindica 4 lotes e
   grava um `<batch_id>.json` para cada. Anote os ids; **não leia os JSONs**.
2. Para cada lote, um Bash em background (paralelo real):
   `& $pf run python .claude/skills/rotular-prompts/rodar_lote.py <scratchpad>\<batch_id>.json`
   O runner compõe o despacho (`despacho.md` + itens), chama o Haiku headless,
   escreve `<batch_id>.jsonl` ao lado do json e imprime UMA linha de resumo.
   Sai 0 (completo), 2 (incompleto — submeta assim mesmo, o submit lista o que
   falta) ou 1 (falha dura).
3. A cada término:
   `& $pf run pf labels submit --batch <batch_id> --file <scratchpad>\<batch_id>.jsonl --model haiku`
4. **Submit falhou (exit 1) com `RETRY_UIDS: ...`?** O lote segue reivindicado —
   falha de validação não mexe no estado. Retry dirigido:
   `... rodar_lote.py <batch>.json --uids uid1,uid2 --nota "motivo curto"` —
   o runner reprocessa só esses itens e mescla no jsonl na ordem do lote;
   submeta de novo. Falhou de novo? Rode o `diag_lote.py` ANTES de qualquer
   requeue (ver Escalada) — não trave o ciclo num lote só.
5. **Agreement abaixo do portão** (`[labeling] agreement_min`, hoje 0.80): o
   `submit` devolve o lote para `pending` sozinho e sai 1. Não é bug, é o
   portão funcionando — e **não re-rode em seguida**: os 3 ouros são os mesmos
   e o resultado tende a repetir. Diagnostique item a item primeiro.
6. **Fim de cada onda**: `pf labels status` + commit de
   `labeling/labels/ + labeling/manifest.json` juntos (snapshot consistente da
   campanha) + `git push`.

**Fallback sem o claude CLI** (outra máquina): o modo antigo continua válido —
1 subagente Haiku por lote via Task, com o conteúdo de `despacho.md` + os itens
inline no prompt. Custa ~40k tokens de maestro por lote, e a garantia anti-cola
vira instrucional: confira `tool_uses: 0` no resultado de cada agente.

## O prompt de despacho

A fonte única é **`despacho.md`, nesta pasta**, com os placeholders
`<<BATCH_ID>>`, `<<N_ITEMS>>` e `<<ITEMS_JSON>>` que o `rodar_lote.py`
preenche (os itens entram como o array JSON do lote). Convenção nova —
decidida em diagnóstico de reprovação, como as do batch_0002 e a do
batch_0005 — se escreve LÁ, na seção CONVENÇÕES: o humano e o runner leem o
mesmo arquivo, e duas cópias divergiriam exatamente na regra mais recente.
No fallback por subagente, o prompt do Task é o `despacho.md` com os
placeholders trocados à mão.

## Escalada e falhas

- **`gold_uids` é FIXO por lote, e isso muda o que "reprovado" significa.** Um
  lote devolvido pelo portão volta com os MESMOS três ouros na próxima
  tentativa. Se dois deles caem numa fronteira genuinamente ambígua, o lote não
  passa nunca — não é um retry que resolve. Com 6 comparações e portão em 0,80,
  um único item fronteiriço já custa 0,33, e mesmo um agente com 85% de acerto
  por comparação reprova ~22% dos lotes. **Reprovação repetida é sinal para
  olhar o desacordo item a item, não para tentar de novo**: se o gabarito
  encoda uma convenção que a taxonomia não diz, o conserto é escrever a
  convenção na seção CONVENÇÕES do prompt de despacho — é para isso que a
  calibração existe. Aí o número passa a medir "aplicou a diretriz", e não mais
  "concorda por conta própria" com o humano; são afirmações diferentes e o
  dataset card tem de dizer qual delas está sendo feita.
- **O diagnóstico é `diag_lote.py`, nesta pasta:**
  `& $pf run python .claude/skills/rotular-prompts/diag_lote.py <batch_id> <resposta.jsonl> <lote.json>`
  — imprime ouro × agente item a item, com o texto de cada ouro. Foi ele que
  transformou o 0,67 do batch_0005 numa convenção nova em vez de um retry cego,
  e foi um diagnóstico assim que descobriu o gabarito corrompido do batch_0002.
- Lote que falhou 2 vezes no Haiku (validação ou agreement), **diagnóstico
  feito e sem gabarito nem convenção a corrigir**: re-rode com
  `rodar_lote.py ... --model claude-sonnet-5` e submeta com `--model sonnet`.
- Lotes `failed` acumulados: pergunte ao usuário antes de
  `pf labels requeue --failed`.
- Erro de `taxonomy_version` em qualquer comando: **PARE tudo** e reporte. A
  taxonomia mudou no meio da campanha e isso é decisão humana.
- Um agente que responde com prosa, cerca de código ou eco do input não é falha
  fatal: o parser tolera a forma. Erro mesmo é uid faltando, uid inventado ou
  valor fora do enum — e aí vale o retry dirigido.

## Encerramento da sessão (sempre)

1. `& $pf run pf labels status` e mostre a tabela final.
2. **Commit + push do estado da campanha**: `labeling/labels/` e
   `labeling/manifest.json` juntos (os rótulos são o único artefato não
   regenerável da campanha, e o remoto é o que os protege do disco).
3. Diga como retomar: "invoque a skill `rotular-prompts` de novo; o manifest
   guarda tudo, e claims órfãos desta sessão voltam sozinhos para `pending`
   depois de `[labeling] claim_ttl_hours` (hoje 2h)".
4. Se `pending == 0`: sugira `& $pf run pf merge-labels` e apresente o relatório
   (agreement médio, distribuição por classe, avisos de mapeamento suspeito).
