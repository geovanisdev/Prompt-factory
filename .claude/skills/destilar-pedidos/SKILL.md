---
name: destilar-pedidos
description: Orquestra a campanha de destilação da Central de Briefs Pedagógicos. Prepara lotes de janelas de material didático com `pf annotate pedidos`, despacha destiladores headless (`rodar_lote.py` → `claude -p`, fora do contexto do maestro), valida e grava tudo pela CLI, e retoma a campanha entre sessões pelo manifest. Use quando o usuário pedir para destilar pedidos, rodar ou continuar a campanha de pedidos, ou mencionar Central de Briefs, material didático, janelas, recortes ou pedidos de prompt.
---

# Campanha de destilação de pedidos (Central de Briefs, F3)

Você é o **maestro**. Os destiladores destilam; você opera o estado. Divisão de
trabalho, e ela não se negocia:

| Quem | Faz | NÃO faz |
| --- | --- | --- |
| Destilador (`claude -p` headless) | recebe o despacho **inteiro no prompt**, devolve JSONL **como texto** | não escreve arquivo, não usa Bash, não lê o repo, não usa ferramenta nenhuma |
| Você (maestro) | preparar, disparo do runner, importar, diagnóstico | não edita `manifest.json` à mão; **não lê o JSON dos lotes** |

## Invariantes (nunca viole)

1. **O texto da editora não entra no seu contexto.** Você toca o CAMINHO do
   arquivo do lote, nunca o conteúdo. Ler um lote "só para conferir" traz 72 mil
   caracteres de material com copyright integral para dentro da conversa — e é
   exatamente o que o desenho headless existe para impedir. Se precisar
   diagnosticar, use o `diag_lote.py`, que imprime só o que falhou.
2. **O destilador é puro cômputo.** Todo o contexto de que ele precisa cabe no
   prompt: as janelas, o vocabulário com as definições da taxonomia, os limites
   e as classes fora de escopo. No caminho headless a garantia é estrutural (cwd
   vazio + ferramentas proibidas por flag).
3. **Toda escrita passa por `pf annotate pedidos`.** É lá que moram a validação
   estrita e a escrita atômica do manifest.
4. **O manifest é a única fonte de estado.** Comece SEMPRE por
   `pf annotate pedidos status`. Nada do que você lembra entre sessões conta.
5. **O recorte é insumo de LEITURA.** Ele não entra no prompt que alguém vai
   escrever, não vai para o corpus, não entra no git e não sai em export. Se
   alguma decisão sua depender de contrariar isso, PARE e pergunte.
6. **Nunca redirecione com `>` no PowerShell** — grava UTF-16/BOM e quebra
   acentuação (pegadinha documentada no CLAUDE.md). O runner escreve o arquivo
   ele mesmo, em UTF-8.

## Comandos base

O `uv` está fora do PATH desta máquina. Defina uma vez por sessão:

```powershell
$pf = "$env:USERPROFILE\.local\bin\uv.exe"
& $pf run pf annotate pedidos status
```

## Passo 1 — Diagnóstico

Rode `& $pf run pf annotate pedidos status` e mostre as tabelas ao usuário.

Numa campanha nova ele lista os 35 arquivos do material com coleção e
disciplina — é dali que sai a escolha do que destilar. Numa campanha em
andamento ele mostra o cursor de cada arquivo, os lotes e o funil do banco.

- Schema divergente (exit 3)? `pf annotate migrate` antes de tudo.
- `[pedidos] material_dir` não configurado? PARE e avise: a campanha não sabe
  onde procurar.
- **`disponivel` alto no funil?** Mostre o número e diga o que ele significa:
  um agente destila centenas de pedidos por hora e um humano escreve poucos
  prompts bons por dia. Número alto ali é sinal de **parar de destilar**, não de
  acelerar. Pergunte ao usuário antes de preparar mais.

## Passo 2 — Escolha do arquivo

Pergunte ao usuário qual disciplina destilar, ou proponha. Duas orientações:

- **diversifique**: dois arquivos de disciplinas diferentes valem mais que doze
  janelas do mesmo livro — o pedido existe para dar variedade a quem escreve;
- **as primeiras janelas de todo livro são capa, créditos, CIP e sumário**, e
  rendem zero pedidos com razão. Um primeiro lote vazio num arquivo novo é
  esperado, não é falha: prepare o seguinte e o cursor já terá passado.

## Passo 3 — Campanha (loop de ondas)

Enquanto o usuário quiser e houver cota:

1. `& $pf run pf annotate pedidos preparar --arquivo "<NOME>.txt" -n 6 --out-dir <scratchpad>`
   Reivindica UM lote e grava `<scratchpad>\ped_NNNN.json`. Anote o id; **não
   leia o JSON**.
2. Dispare o runner (Bash em background quando forem vários):
   `& $pf run python .claude/skills/destilar-pedidos/rodar_lote.py <scratchpad>\ped_NNNN.json`
   Ele compõe o despacho (`despacho.md` + janelas), chama o modelo headless,
   escreve `ped_NNNN.resposta.json` ao lado e imprime UMA linha de resumo.
   Sai 0 (vieram pedidos), 2 (lote vazio — olhe, pode ser legítimo) ou 1 (falha
   dura).
3. `& $pf run pf annotate pedidos importar --lote ped_NNNN --file <scratchpad>\ped_NNNN.resposta.json --model sonnet`
4. **Import falhou (exit 1)?** O lote segue `claimed` — falha de validação não
   mexe no estado. As mensagens nomeiam a janela (`ped_0001/j02, pedido 1`).
   Retry dirigido só das janelas que falharam:
   `... rodar_lote.py <lote>.json --janelas j02,j05 --nota "motivo curto"`
   O runner reprocessa só elas e **preserva os pedidos das outras janelas**.
   Importe de novo.
5. **Três falhas** levam o lote a `failed`. Reviva com
   `pedidos preparar --lote ped_NNNN` (zera as tentativas) — mas só depois de
   diagnosticar; não trave o ciclo num lote só.
6. **Fim de cada onda**: `pedidos status` + commit de `destilacao/manifest.json`
   (só ele; lotes e respostas são gitignorados) + push.

## O prompt de despacho

A fonte única é **`despacho.md`, nesta pasta**. Os placeholders que o
`rodar_lote.py` preenche a partir do próprio lote: `<<LOTE_ID>>`, `<<ARQUIVO>>`,
`<<COLECAO>>`, `<<DISCIPLINA>>`, `<<N_JANELAS>>`, `<<MAX_POR_JANELA>>`,
`<<RECORTE_MIN>>`, `<<RECORTE_MAX>>`, `<<MIN_TEMA>>`, `<<MIN_META>>`,
`<<TASK_TYPES>>`, `<<EXCLUIDOS>>`, `<<PORQUE_EXCLUIDOS>>`, `<<JANELAS_JSON>>`.

Os limites e o vocabulário vêm do **lote**, não do arquivo: mexer em
`[pedidos] recorte_max_chars` no `settings.toml` muda o que o despacho promete,
sem editar uma linha aqui. Convenção nova — decidida em diagnóstico — se escreve
LÁ, no `despacho.md`, e não numa cópia sua.

## Diagnóstico de falha

`& $pf run python .claude/skills/destilar-pedidos/diag_lote.py <lote>.json <resposta>.json`

Imprime, por pedido recusado, **o que a validação viu**: o tipo de erro e, no
caso do recorte, onde a cópia divergiu (o prefixo comum, o caractere que quebrou
e o contexto mínimo dos dois lados). Ele mostra dezenas de caracteres, não
milhares — é o único caminho em que texto do material chega até você, e é
deliberadamente estreito.

**O erro mais comum é o recorte.** Copiar 200 a 2.000 caracteres verbatim é a
parte difícil da tarefa, e as falhas típicas são: espaço aparado no fim,
hifenização "consertada", reticências juntando dois trechos, aspas
normalizadas. Se um lote falhar muito nisso:

1. retry dirigido só das janelas ruins, com a nota apontando o padrão;
2. se repetir, escale o modelo (`--model claude-sonnet-5` já é o padrão;
   `claude-opus-5` é o degrau acima);
3. se repetir de novo, **registre o número** e traga ao usuário: uma taxa alta
   de recorte adulterado é evidência a favor de mudar o contrato (o pedido
   passaria a citar âncoras de início e fim em vez do trecho inteiro), e isso é
   decisão dele, não sua.

## Encerramento da sessão (sempre)

1. `& $pf run pf annotate pedidos status` e mostre as tabelas finais.
2. **Commit + push** do `destilacao/manifest.json` (o livro-caixa é o que torna
   a campanha retomável; os lotes e as respostas ficam fora do git de
   propósito).
3. Diga como retomar: "invoque a skill `destilar-pedidos` de novo; o manifest
   guarda o cursor de cada arquivo, e claims órfãos desta sessão voltam sozinhos
   para `pending` depois de `[pedidos] claim_ttl_horas` (hoje 6h)".
4. Reporte a **taxa de erro de validação da rodada** — quantos pedidos vieram,
   quantos foram recusados e por quê. É o número que decide se o contrato do
   recorte precisa mudar.
