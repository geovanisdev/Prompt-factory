# Prompt de handoff — Central de Briefs, marco F4-2

Copie o bloco abaixo inteiro como primeira mensagem de uma sessão nova, com o
diretório de trabalho em `G:\Github\Prompt-factory-briefs`.

Ele é auto contido no sentido que importa: não depende de nenhuma conversa
anterior, e aponta para os documentos do repositório em vez de repetí-los. O que
ele carrega por escrito é só o que **ainda não está** neles.

---

Você vai continuar a **Central de Briefs Pedagógicos**, um módulo da plataforma
de anotação ("Bancada") do projeto Prompt Factory. Está tudo no repositório —
sua primeira tarefa é ler, não codar.

## 1. Leia primeiro, nesta ordem

1. `CLAUDE.md` (raiz) — o mapa do projeto inteiro. As seções **"Central de
   Briefs Pedagógicos (F1)"** até **"(F4-1)"**, no fim, são o marco em que você
   está entrando. Leia também "Pegadinhas" (o ambiente desta máquina) e
   "Bancada — plataforma de anotação".
2. `docs/central-de-briefs/HANDOFF.md` — a tese do projeto e como as peças que
   este módulo toca funcionam hoje.
3. `docs/central-de-briefs/PLANO.md` — o desenho do módulo, a **§6 (licença,
   que decide tudo)**, os marcos F1–F6 com o que já foi medido, e a §9 com as
   seis perguntas do dono **já respondidas** (não as reabra).

Não leia `data/`, `destilacao/lotes/` nem `destilacao/respostas/` — ver a regra
de licença no item 4.

## 2. Onde as coisas estão

Você está numa **worktree** (`G:\Github\Prompt-factory-briefs`), branch
`central-de-briefs`. O checkout principal (`G:\Github\Prompt-factory`) tem uma
campanha de rotulagem do dono rodando e um `annotate.sqlite` de verdade com
trabalho humano dentro. **Não toque nele.** Esta worktree tem banco próprio em
`data/db/annotate.sqlite`, já na v7, com **42 pedidos reais** dentro.

Feito até aqui (commits nesta branch):

| marco | commit | o que entregou |
| --- | --- | --- |
| F1 | `ccb7893` | schema **v7**: tabela `pedidos`, `criacoes.pedido_id`/`material_json`, `ORIGENS_RUBRICA + 'criacao'`, `migracao._da_v6` |
| F2 | `0938e9f` | `annotate/destilacao.py` + `pf annotate pedidos preparar/importar/status` |
| F3 | `4fe590c` | skill `destilar-pedidos` (headless) + a primeira rodada real: 42 pedidos, 2 disciplinas |
| F4-1 | `4801ca9` | `annotate/pedidos.py` (fila/reserva) + contrato `material_criacao@1` + 3 rotas |

Suíte: **1.424 testes passando**, ruff limpo. Mantenha assim.

## 3. Sua tarefa: F4-2 — a tela da vista criar

O backend está pronto e testado. Falta a interface, em
`src/prompt_factory/annotate/static/index.html` (**10.903 linhas**, arquivo
único, sem build, sem framework, sem CDN).

O que construir, na 4ª vista do anotador (o "modo criar", que já existe):

- **um painel do pedido no topo**, com o mesmo desenho do painel de brief do P9
  (que fica acima dele). Conteúdo: recorte (serifado, como todo conteúdo), tema,
  meta pedagógica, papel como selo, `task_type` como chip **nome legível + id**
  (o contrato do chip do P9: o id é o que atravessa), habilidades BNCC, e os
  `avisos` do pedido quando houver;
- **"puxar próximo pedido"** e **"devolver"**, com filtros de disciplina e papel
  vindos das facetas;
- **um aviso de autoria permanente** no painel (não um recado que some):
  *escreva com as suas palavras; o recorte é leitura, não matéria-prima — texto
  colado será recusado na revisão*. A dedicação CC0 continua acima do botão;
- **com pedido ativo, o formulário ganha os blocos da tríade**: rubrica
  (3 a 5 critérios, escala ancorada nas duas pontas) e gold (três listas +
  observações). O botão de enviar cobra **na ordem do trabalho**, como a cascata
  do P9: prompt → rubrica → gold;
- **i18n completo** — ver a regra 4 abaixo.

### As rotas que a tela chama (já existem, contrato em `/docs`)

```
GET  /api/pedidos?anotador_id=N   -> {meu, facetas:{disponiveis,disciplina,papel}, reserva_ttl_min}
POST /api/pedidos/proximo         {anotador_id, disciplina?, papel?}
                                  -> {pedido|null, motivo, motivo_chave}
POST /api/pedidos/{id}/devolver   {anotador_id} -> o pedido
POST /api/criacoes                {..., pedido_id, material:{rubrica, gold}} -> 201
```

`motivo_chave` é `vazio` | `vazio_filtrado` | `ja_reservado` | `null`. **A tela
traduz a CHAVE**; o `motivo` em português existe para o `/docs`. Frase de tela
mora na tela — é a lição do P3i, e está no `CLAUDE.md`.

### DoD (a definição de pronto, binária)

No navegador, contra o banco desta worktree: puxar um pedido, escrever a tríade,
enviar; a criação aparece na lista **com o pedido ao lado**; o toggle `en`/`pt`
**mantém o rascunho digitado e o pedido aberto** (não recarrega, não refaz
chamada); **zero `innerHTML`**; **zero erro de console**. Mais: suíte verde e
ruff limpo.

## 4. Regras que não se negociam

1. **O recorte é de terceiros.** O material são 35 apostilas do PNLD com
   copyright integral de Ática, Scipione e Saraiva. Ele pode aparecer na TELA
   (quem escreve precisa lê-lo) e **nunca** entra no prompt, no corpus, no git
   ou em export. `destilacao/lotes/**` e `respostas/**` são gitignorados; não os
   abra "só para ver". A §6 do `PLANO.md` é a análise inteira.
2. **Zero `innerHTML` neste `index.html`, sem exceção.** A interface de curadoria
   (`app/static/index.html`, outro arquivo) tem uma; aqui a regra é absoluta e
   há teste cobrando. Todo nó nasce de `el()` e todo texto entra por
   `textContent`.
3. **Nenhuma referência externa.** Sem CDN, sem fonte remota, sem imagem de fora.
   Há teste.
4. **Todo texto de tela passa pelo dicionário `TEXTOS`** (JSON estrito dentro do
   JS — aspas duplas, sem vírgula sobrando, sem comentário: é assim que o pytest
   o carrega com `json.loads`). Cada nó carrega `data-t`, `data-t-ph`,
   `data-t-title` ou `data-t-aria`. **Chave montada por concatenação é invisível
   para o teste de chave órfã** — tabelas guardam a chave INTEIRA como literal.
   Uma frase solta no HTML ficaria em português para sempre.
5. **Trocar de idioma NÃO recarrega e NÃO refaz chamada**: `escolherIdioma()`
   redesenha a partir de `est`. Campo cujo valor mora no DOM precisa **mudar de
   casa** para `est` — foi o que aconteceu com o comentário de devolução, a busca
   do catálogo e o motivo da fila vazia. Seu rascunho da tríade tem o mesmo
   problema: resolva-o do mesmo jeito.
6. **A escrita de dados sai de dentro do Python, em UTF-8.** Nunca `>` nem
   `Out-File` no PowerShell (grava UTF-16/BOM). O `uv` está fora do PATH:
   `& "$env:USERPROFILE\.local\bin\uv.exe" run ...`.
7. **Não invente número.** Se for afirmar desempenho, tamanho ou proporção,
   meça. O `CLAUDE.md` inteiro é escrito assim, e é a convenção da casa.

## 5. Reuso — o que já existe e não deve ser reescrito

- A tela do P8 desenha critérios de rubrica com `matrizDeCriterios(criterios,
  ctx, opts)`. Mas ela **aplica** uma rubrica (dá notas); aqui a pessoa
  **escreve** uma. O reuso certo é com o formulário da aba `escrever_rubrica`
  (as funções `ws*`), não com a matriz. Confira antes de decidir.
- `comParams(caminho, params)` aceita objeto simples e `URLSearchParams`.
- O painel de brief do P9 (`#brief`) é o modelo visual do painel do pedido, e a
  regra "só UM painel de instrução abre sozinho" já existe — respeite-a.
- `criacoes.listar` já devolve `pedido` e `material` desserializados; não refaça
  a leitura do blob no JS.

## 6. Como rodar e verificar

```powershell
$pf = "$env:USERPROFILE\.local\bin\uv.exe"
& $pf run pytest -q                    # 1.424 verdes hoje
& $pf run ruff check .
& $pf run pf annotate status           # o banco desta worktree, na v7
& $pf run pf annotate pedidos status   # 42 pedidos, funil por disciplina
& $pf run pf annotate                  # sobe a Bancada em http://127.0.0.1:8766
```

Verifique no navegador de verdade (as ferramentas do Chrome estão disponíveis) e
**relate o que mediu**, não o que espera. Se matar o servidor com `taskkill`,
rode `pf annotate status` depois: o `finally` que apaga o `-shm` órfão do corpus
é pulado, e um `-shm` órfão faz o próximo `pf load-db` recusar o swap.

## 7. Decisões já tomadas — não reabra

As seis perguntas do dono estão respondidas na §9 do `PLANO.md`, com o porquê.
As que afetam você: **rubrica e gold em INGLÊS** (o prompt segue em pt-BR), e a
gold **não** é materializada junto da rubrica — ela vive na criação e sai na
entrega.

## 8. Uma decisão PENDENTE do dono (não decida sozinho)

Na rodada do F3, `serie` saiu `indefinido` em **42 de 42** pedidos. Os três
arquivos destilados são de volume único e o ano não aparece no texto — mas as
coleções de três volumes carregam o ano **no nome do arquivo**
(`..._MATEM_VOL1_MP`, `..._PORTUGUES_VOL2_MP`), onde o destilador não enxerga.
Derivar isso depende da premissa "volume 1 = 1º ano", que é conhecimento do
dono sobre o PNLD. Se ele confirmar, o conserto é em
`destilacao.metadados_do_nome` e no lote (como sugestão, não valor forçado).

## 9. Depois do F4-2

**F5** — revisão com anti-cópia no servidor (compara prompt × recorte por
sobreposição literal; o limiar `[pedidos] max_overlap_chars` é para **calibrar
contra submissões reais**, não para adivinhar) e a rubrica materializada na
aprovação com `origem='criacao'` e `prompt_uid=uid_previsto`.
**F6** — entrega: a tríade nos perfis do P5b, sem o recorte, com teste que varre
o arquivo por uma senha plantada.

Comece lendo. Quando tiver o mapa na cabeça, diga o que entendeu e o que pretende
fazer antes de escrever a primeira linha.
