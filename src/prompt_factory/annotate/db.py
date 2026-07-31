"""Schema do ``annotate.sqlite`` — o estado da plataforma de anotação.

Um arquivo só (``data/db/annotate.sqlite``), em WAL, aberto por
``prompt_factory.db.connect`` (que já aplica WAL, ``busy_timeout``,
``foreign_keys=ON`` e ``row_factory``). **Não** reimplemente a conexão aqui: o
``foreign_keys`` é PRAGMA por conexão, e sem ele os ``ON DELETE CASCADE`` deste
DDL ficam inertes e os testes passam em falso.

Por que um banco separado do corpus
===================================
O ``prompts.sqlite`` é **reconstruído do zero e trocado por swap de arquivo** a
cada ``pf load-db``. Guardar anotações lá dentro faria toda recarga do corpus
apagar o trabalho humano. Aqui é o inverso: este banco pertence à app, que o
cria sozinha na primeira subida.

Consequência que atravessa o schema inteiro: **a chave estrangeira lógica para
o corpus é ``prompt_uid``, TEXT, nunca um id.** O rowid do corpus muda a cada
recarga; o ``uid`` é estável por construção (sha256 de fonte+id ou do texto).
Não há FK de verdade para o outro arquivo — não existe FK entre bancos, e
inventar um ATTACH transformaria o corpus em escritor concorrente. Um
``prompt_uid`` que sumiu depois de uma recarga é uma tarefa "indisponível" na
tela, nunca um 500.

Nomes em português
==================
As tabelas daqui são em pt-BR de propósito, enquanto as do corpus são em inglês.
São dois vocabulários que nunca se misturam, e ler ``atribuicoes`` numa consulta
já diz de qual banco ela fala.

Vocabulários fechados
=====================
Todo CHECK deste DDL tem uma tupla correspondente neste módulo (``PAPEIS``,
``TIPOS_TAREFA``, ...). O Pydantic valida a partir dessas tuplas, então banco e
API concordam por construção — e não por disciplina de quem editar os dois.
(A exceção do corpus, ``task_type``/``domain`` sem CHECK porque a taxonomia
evolui, não se aplica: estes vocabulários são a mecânica da plataforma.)
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

#: Versão do schema físico da plataforma, gravada em ``app_meta``.
#: Incrementar exige migração — este banco guarda trabalho humano e, ao
#: contrário do corpus, **não** é regenerável a partir da pipeline.
#:
#: **v2 (P3a)**: a revisão em duas passagens (``avaliacoes``,
#: ``edicoes_avaliacao``, ``decisoes_admin``), a máquina de status de
#: ``anotacoes`` e as quatro colunas de OPERAÇÃO — escopo de projeto,
#: versão da diretriz, qualificação e trilha de auditoria. As telas de três
#: delas só chegam depois; o schema entra agora porque este banco já guarda
#: trabalho humano e cada anotação a mais encarece a migração seguinte.
#: Quem sobe a versão é ``annotate/migracao.py`` (``pf annotate migrate``),
#: nunca um UPDATE silencioso.
#:
#: **v3 (P3b)**: UMA coluna — ``anotacoes.gabarito_avaliacao_json``, a nota-alvo
#: escondida de uma anotação SINTÉTICA. Uma coluna só justifica uma migração
#: inteira porque este banco guarda trabalho humano: acrescentá-la depois de a
#: campanha do P4c gravar centenas de anotações custaria mais, e o preço de
#: migrar é exatamente o número de linhas que já existem.
#:
#: **v4 (P4c)**: UM valor a mais no CHECK de ``rubricas.origem`` —
#: ``'importada'``, que ``respostas_modelo`` já aceitava. A campanha de geração
#: escreve os DOIS lados do material (a rubrica e as duas respostas) e eles
#: precisam sair da mesma linha de origem: chamar de ``'fixture'`` uma rubrica
#: gerada sobre um prompt REAL do corpus seria mentir numa coluna, que é
#: exatamente o que este projeto existe para não fazer. Nenhuma coluna nasce,
#: nenhuma linha é transformada — mas o CHECK está no ``CREATE TABLE`` e
#: ``IF NOT EXISTS`` não o reescreve num banco que já existe. Ou seja: sem a
#: migração, o banco do dono continuaria recusando o INSERT com um CHECK velho
#: que nenhum arquivo do repositório mostra mais. Meio-schema de novo.
#:
#: **v5 (P4d)**: as duas tarefas de INFERÊNCIA AO VIVO — ``conversa_modelo`` e
#: ``duelo_modelos`` entram em ``TIPOS_TAREFA`` (dois CHECKs alargados, em
#: ``tarefas`` e em ``diretrizes``) e nasce a tabela ``turnos_conversa``. Ela é a
#: primeira tabela deste schema a guardar trabalho produzido DURANTE a tarefa e
#: não no envio: o anotador conversa por vinte minutos antes de existir uma
#: linha em ``anotacoes``. Guardá-la no servidor (em vez de só no
#: ``localStorage``) é o que faz o duelo ser cego de verdade — o mapa de qual
#: modelo é "A" nunca sai daqui antes de a avaliação ser enviada.
SCHEMA_VERSION_ANOTACAO = 5

#: Chave de ``app_meta`` onde a versão acima mora.
CHAVE_VERSAO = "schema_version_anotacao"

#: Papéis. Sem senha: é teatro de autorização de uma demonstração, e está
#: escrito na tela ("demonstração — sem login"). O servidor ainda assim valida o
#: papel em toda rota mutante — teatro mal feito viraria bug de verdade no dia
#: em que alguém pusesse isto na rede.
PAPEIS: tuple[str, ...] = ("anotador", "revisor", "admin")

#: Os estilos de tarefa (as abas do anotador). São também o prefixo do
#: ``payload_schema`` das anotações: ``comparar_ab@1``.
#:
#: Os quatro primeiros trabalham sobre **texto parado**; os dois últimos (P4d)
#: exigem **inferência ao vivo** — um modelo local respondendo enquanto a pessoa
#: anota. A ordem é a das abas na tela, e as duas novas ficam no fim porque são
#: as únicas que dependem de um serviço fora deste processo.
TIPOS_TAREFA: tuple[str, ...] = (
    "avaliar_rubrica",
    "escrever_rubrica",
    "sft_resposta",
    "comparar_ab",
    "conversa_modelo",
    "duelo_modelos",
)

#: Os dois tipos que falam com o Ollama. Uma tupla, e não um ``in (...)`` solto
#: em cinco módulos: quem acrescentar um terceiro formato de conversa muda um
#: lugar. Toda regra que vale "só para conversa" (rubrica multi-turno, turnos no
#: envelope, injeção dos turnos na submissão) consulta daqui.
TIPOS_CONVERSA: tuple[str, ...] = ("conversa_modelo", "duelo_modelos")

#: Quem falou num turno. ``modelo`` é o Ollama; ``usuario`` é o anotador — e a
#: tradução para os papéis do ``/api/chat`` (``assistant``/``user``) mora em
#: ``conversa.historico``, num lugar só.
PAPEIS_TURNO: tuple[str, ...] = ("usuario", "modelo")

#: Os rótulos CEGOS de uma rodada de duelo. ``""`` é o turno do humano (que não
#: tem lado). São "A"/"B" e não ``modelo-a``/``modelo-b`` de propósito: os
#: primeiros são o que a tela mostra, e o identificador interno do A/B de turno
#: único (``ROTULOS_MODELO``) descreve OUTRA coisa — uma linha de
#: ``respostas_modelo``, que é material parado.
ROTULOS_DUELO: tuple[str, ...] = ("A", "B")

#: A escala da nota de um TURNO PROBLEMÁTICO. Fixa e curta: marcar um turno é um
#: gesto de passagem no meio da leitura da conversa, não uma segunda rubrica.
#: Cinco posições é o mesmo tamanho dos critérios das fixtures, então a mão já
#: sabe onde ficam as pontas.
ESCALA_TURNO: tuple[int, int] = (1, 5)

#: De onde a tarefa veio. ``semente`` = ``pf annotate seed``; ``admin`` = gerada
#: no painel; ``livre`` = o anotador escolheu o prompt no catálogo (find-or-
#: create); ``continuacao`` = oferecida ao concluir outra (rubrica -> SFT).
ORIGENS_TAREFA: tuple[str, ...] = ("semente", "admin", "livre", "continuacao")

#: Ciclo de vida da tarefa (o da ATRIBUIÇÃO é outro, abaixo).
STATUS_TAREFA: tuple[str, ...] = ("aberta", "pausada", "concluida")

#: Ciclo de vida da atribuição — a trava do modo locked. ``expirada`` é escrita
#: preguiçosamente (UPDATE ... WHERE expira_em < agora) antes de cada claim, sem
#: thread de fundo: um relógio de fundo numa app local é complexidade que só
#: aparece quando quebra.
STATUS_ATRIBUICAO: tuple[str, ...] = (
    "em_andamento",
    "submetida",
    "aprovada",
    "abandonada",
    "expirada",
)

#: Estados de uma anotação submetida — a MÁQUINA da revisão em duas passagens.
#: Nada aqui apaga nada: uma devolução grava ``versao + 1`` e a história fica.
#:
#: A máquina, por extenso (``TRANSICOES`` abaixo é a mesma coisa em dados)::
#:
#:   pendente_triagem --devolve--> devolvida --anotador refaz--> pendente_triagem
#:   pendente_triagem --aprova---> pendente_avaliacao
#:   pendente_avaliacao ---------> avaliada | escalada | descartada
#:   escalada --(admin)----------> avaliada | devolvida | descartada
#:
#: **Só a triagem devolve trabalho ao anotador.** A passagem 2 (Rate and
#: Review, P3b) corrige no lugar e, no pior caso, descarta: um item que já foi
#: aprovado na triagem e volta ao anotador dias depois mede o revisor, não quem
#: anotou — e a métrica de QC que nasce disso ficaria embaralhada.
STATUS_ANOTACAO: tuple[str, ...] = (
    "pendente_triagem",
    "devolvida",
    "pendente_avaliacao",
    "avaliada",
    "escalada",
    "descartada",
)

#: A máquina acima como DADO: ``{status: (destinos permitidos)}``. As rotas
#: consultam daqui em vez de repetir a regra em cada ``if``, e o teste da
#: máquina lê isto — uma transição nova aparece nos dois lugares de uma vez.
TRANSICOES: dict[str, tuple[str, ...]] = {
    "pendente_triagem": ("devolvida", "pendente_avaliacao"),
    "devolvida": ("pendente_triagem",),
    "pendente_avaliacao": ("avaliada", "escalada", "descartada"),
    "escalada": ("avaliada", "devolvida", "descartada"),
    "avaliada": (),
    "descartada": (),
}

#: Veredito da TRIAGEM (passagem 1). ``devolvida`` e não ``rejeitada``: o
#: trabalho volta para ajuste, não é recusado — e o status que ele produz na
#: anotação tem exatamente este nome, porque um veredito que não vira estado
#: seria decoração.
VEREDITOS: tuple[str, ...] = ("aprovada", "devolvida")

#: Passagem 2, escala 1 — o trabalho **como chegou**. ``inutilizavel`` tem de
#: existir mesmo esperando-se que a triagem já o tenha barrado: é justamente o
#: caso "a triagem deixou passar" que precisa ficar registrado, e ele mede o
#: revisor da triagem, não o anotador.
AVALIACOES_ANTES: tuple[str, ...] = (
    "inutilizavel",
    "ajustavel",
    "adequado",
    "excepcional",
)

#: Passagem 2, escala 2 — o RESULTADO, depois de o revisor corrigir no lugar.
#: ``borderline_admin`` é o único desfecho que escala; ``incorrigivel`` é
#: terminal e **não** volta ao anotador.
AVALIACOES_DEPOIS: tuple[str, ...] = (
    "incorrigivel",
    "borderline_admin",
    "adequado",
    "excepcional",
)

#: O que o admin decide sobre um item escalado. Encerra o item nos três casos.
#:
#: ``devolvida`` **é** permitida aqui, e não é uma exceção à regra "só a triagem
#: devolve": a regra existe para que a passagem 2 não jogue trabalho de volta
#: dias depois de tê-lo aprovado. O admin decidindo sobre um item que o próprio
#: revisor marcou como duvidoso é o caminho de escalação, não a segunda passagem.
DECISOES_ADMIN: tuple[str, ...] = ("aprovada", "devolvida", "descartada")

#: Como um ``avaliacao_depois`` vira estado de ``anotacoes``. É DADO, e não uma
#: sequência de ``if`` na rota, pela razão de ``TRANSICOES``: a máquina e o teste
#: leem da mesma tabela. Note que ``devolvida`` não aparece — ver ``TRANSICOES``.
DESFECHO_AVALIACAO: dict[str, str] = {
    "incorrigivel": "descartada",
    "borderline_admin": "escalada",
    "adequado": "avaliada",
    "excepcional": "avaliada",
}

#: E o mesmo para a decisão do admin sobre um item escalado.
DESFECHO_DECISAO: dict[str, str] = {
    "aprovada": "avaliada",
    "devolvida": "devolvida",
    "descartada": "descartada",
}

#: As chaves de ``anotacoes.gabarito_avaliacao_json``. Contrato do P4c (que
#: preenche, na campanha de geração) e do P5a (que compara a avaliação do
#: revisor com o alvo). Uma tupla, e não um modelo Pydantic, porque nenhuma rota
#: aceita este dado do cliente: ele nasce de um comando local.
CHAVES_GABARITO_AVALIACAO: tuple[str, ...] = ("avaliacao_antes", "familia_defeito", "nota")

#: O nome da coluna do alvo escondido, em UM lugar. Quem prova que ela não vaza
#: (``tests/test_annotate_p3b.py``) varre as respostas por esta string.
COLUNA_GABARITO_AVALIACAO = "gabarito_avaliacao_json"

#: Ciclo de vida de um projeto/campanha. Sem ele dois clientes não cabem no
#: mesmo banco — e acrescentar o escopo depois seria migrar trabalho humano.
STATUS_PROJETO: tuple[str, ...] = ("ativo", "pausado", "encerrado")

#: Estado de uma qualificação de anotador (a coluna existe e é escrita no P3a;
#: **barrar** quem não é qualificado fica para depois, com a tela).
STATUS_QUALIFICACAO: tuple[str, ...] = ("pendente", "aprovada", "reprovada")

#: Origem de rubricas e respostas de modelo. ``fixture`` = pacote de
#: demonstração, escrito à mão sobre prompts que **não são de ninguém**;
#: ``anotacao`` = materializada da aba escrever-rubrica na aprovação do revisor;
#: ``importada`` = material gerado pela campanha do P4c (``pf annotate gerar``)
#: sobre prompts REAIS do corpus.
#:
#: As duas tuplas terminam iguais desde a v4, e o motivo de elas continuarem
#: separadas é que uma rubrica nunca vai ter uma origem que uma resposta tenha
#: (ou vice-versa) por acaso: a divergência entre elas é informação, não
#: duplicação. ``importada`` entrou em ``rubricas`` porque a campanha escreve o
#: PAR — a rubrica e as duas respostas nascem do mesmo lote e não podem contar
#: histórias diferentes sobre a própria procedência.
ORIGENS_RUBRICA: tuple[str, ...] = ("fixture", "anotacao", "importada")
ORIGENS_RESPOSTA: tuple[str, ...] = ("fixture", "importada")

#: Rótulos cegos do A/B. Cegos de propósito: "modelo-a"/"modelo-b" na tela, o
#: nome real (e o defeito plantado) só no ``meta_json``, visível ao admin.
ROTULOS_MODELO: tuple[str, ...] = ("modelo-a", "modelo-b")

#: Funil do modo criar. ``exportada`` é carimbada pelo ``pf ingest plataforma``
#: (P6) DEPOIS de a linha entrar em ``data/raw/`` — antes disso, aprovada.
STATUS_CRIACAO: tuple[str, ...] = ("submetida", "aprovada", "rejeitada", "exportada")

#: Prefixo do uid dos prompts do pacote de demonstração. É o que o resolvedor lê
#: para decidir se busca o texto no corpus read-only ou em ``prompts_demo`` — por
#: isso o DDL o exige com um CHECK, em vez de confiar em quem inserir.
PREFIXO_DEMO = "demo:"


def _agora() -> str:
    """A expressão SQL do carimbo de tempo (UTC, ISO-8601 com milissegundos).

    A mesma de ``db.DDL``, e por isso comparável entre os dois bancos numa
    leitura humana. Não há trigger de ``atualizado_em`` em lugar nenhum: quem
    escreve pela API é quem seta a coluna.
    """
    return "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def _lista(valores: tuple[str, ...]) -> str:
    """``('a','b')`` — a lista literal de um CHECK ... IN, sem risco de digitação."""
    return ", ".join(f"'{v}'" for v in valores)


DDL = f"""
-- ---------------------------------------------------------------------------
-- QUEM
-- ---------------------------------------------------------------------------
-- Sem senha, sem e-mail, sem sessão: a identidade é o nome, escolhido na tela
-- de entrada e guardado no localStorage do navegador. `nome` é UNIQUE porque é
-- ele que o cliente manda de volta em toda rota, e dois "Ana" seriam duas
-- pessoas indistinguíveis nas métricas do admin.
CREATE TABLE IF NOT EXISTS anotadores (
  id        INTEGER PRIMARY KEY,
  nome      TEXT NOT NULL UNIQUE,
  papel     TEXT NOT NULL CHECK (papel IN ({_lista(PAPEIS)})),
  -- Desativar em vez de apagar: apagar um anotador levaria junto o histórico
  -- que as métricas do admin somam.
  ativo     INTEGER NOT NULL DEFAULT 1 CHECK (ativo IN (0,1)),
  -- QUALIFICAÇÃO por estilo de tarefa: um JSON no formato
  -- 'avaliar_rubrica' -> 'aprovada' | 'pendente' | 'reprovada'.
  -- A coluna existe e É ESCRITA desde o P3a; barrar quem não é qualificado
  -- fica para quando a tela existir. JSON (e não tabela) porque são 4 chaves
  -- por pessoa, lidas sempre inteiras junto do perfil — uma tabela custaria um
  -- JOIN em toda listagem para guardar o mesmo dicionário.
  qualificacoes_json TEXT NOT NULL DEFAULT '{{}}',
  criado_em TEXT NOT NULL DEFAULT ({_agora()})
);

-- ---------------------------------------------------------------------------
-- ESCOPO: projeto/campanha
-- ---------------------------------------------------------------------------
-- Sem isto, dois clientes não cabem no mesmo banco, e acrescentar o escopo
-- DEPOIS seria migrar trabalho humano — o que este marco existe para evitar.
-- A tela do admin para gerenciar projetos vem depois; a coluna e o projeto
-- padrão entram agora, enquanto são baratos.
CREATE TABLE IF NOT EXISTS projetos (
  id        INTEGER PRIMARY KEY,
  nome      TEXT NOT NULL UNIQUE,
  cliente   TEXT NOT NULL DEFAULT '',
  descricao TEXT NOT NULL DEFAULT '',
  status    TEXT NOT NULL DEFAULT 'ativo'
              CHECK (status IN ({_lista(STATUS_PROJETO)})),
  criado_em TEXT NOT NULL DEFAULT ({_agora()})
);

-- ---------------------------------------------------------------------------
-- A REGRA SOB A QUAL O TRABALHO FOI FEITO
-- ---------------------------------------------------------------------------
-- As diretrizes eram texto literal no `index.html`. O cenário que isto resolve:
-- a regra do A/B é ajustada na terça; o cliente reclama de inconsistência na
-- quinta; sem versão gravada por anotação, a única saída é refazer o lote
-- inteiro, porque não há como separar "antes" de "depois".
--
-- `texto_json` guarda um objeto com a chave `linhas` — a diretriz é uma LISTA
-- de regras, não um parágrafo, e é assim que a tela a desenha.
CREATE TABLE IF NOT EXISTS diretrizes (
  id         INTEGER PRIMARY KEY,
  tipo       TEXT NOT NULL CHECK (tipo IN ({_lista(TIPOS_TAREFA)})),
  versao     INTEGER NOT NULL CHECK (versao >= 1),
  texto_json TEXT NOT NULL,
  criada_em  TEXT NOT NULL DEFAULT ({_agora()}),
  UNIQUE (tipo, versao)
);

-- ---------------------------------------------------------------------------
-- O QUE SE ANOTA
-- ---------------------------------------------------------------------------
-- Pacote de demonstração: prompts escritos à mão para as fixtures, que NÃO vêm
-- do corpus. Ficam aqui (e não em `data/`) porque o corpus é somente leitura
-- para esta app e porque uma fixture não é um prompt real de ninguém — misturar
-- os dois dentro do corpus destruiria a proveniência, que é o argumento inteiro
-- do projeto.
--
-- O CHECK do prefixo é contrato de RESOLVEDOR, não estética: quem hidrata uma
-- tarefa decide entre o corpus read-only e esta tabela olhando o começo do uid.
CREATE TABLE IF NOT EXISTS prompts_demo (
  uid       TEXT PRIMARY KEY CHECK (uid LIKE '{PREFIXO_DEMO}%'),
  text      TEXT NOT NULL,
  lang      TEXT NOT NULL CHECK (lang IN ('pt','en')),
  criado_em TEXT NOT NULL DEFAULT ({_agora()})
);

-- Uma unidade de trabalho: ESTE prompt, NESTE estilo de tarefa. O mesmo prompt
-- em dois tipos são duas linhas — avaliar com rubrica e escrever a rubrica são
-- trabalhos diferentes sobre o mesmo texto.
--
-- `gabarito_json` é NULL exceto nas tarefas-OURO: é o gabarito escondido na
-- fila que permite calcular agreement sem pedir nada a mais ao anotador (e é o
-- que o painel do admin mostra para provar que existe QC aqui).
--
-- `db_build_id` registra CONTRA QUAL build do corpus a tarefa nasceu. Depois de
-- um `pf load-db` o uid pode ter sumido (o dedup evoluiu, a linha saiu do
-- universo); com o build_id a tela sabe dizer "indisponível desde a recarga de
-- <data>" em vez de só falhar.
CREATE TABLE IF NOT EXISTS tarefas (
  id               INTEGER PRIMARY KEY,
  tipo             TEXT NOT NULL CHECK (tipo IN ({_lista(TIPOS_TAREFA)})),
  prompt_uid       TEXT NOT NULL,
  origem           TEXT NOT NULL CHECK (origem IN ({_lista(ORIGENS_TAREFA)})),
  -- NULL é permitido porque `ALTER TABLE ADD COLUMN` com REFERENCES exige
  -- default NULL, e o schema de um banco NOVO tem de ser idêntico ao de um
  -- MIGRADO (há um teste que compara os dois `sqlite_master`). Quem garante o
  -- preenchimento é o seed e as rotas, que sempre usam o projeto padrão.
  projeto_id       INTEGER REFERENCES projetos(id),
  payload_json     TEXT NOT NULL DEFAULT '{{}}',
  gabarito_json    TEXT,
  -- >= 1 no CHECK: uma tarefa que aceita zero anotações nunca sairia da fila e
  -- seria um buraco silencioso. 2 é o que gera par para medir agreement.
  n_anotacoes_alvo INTEGER NOT NULL DEFAULT 1 CHECK (n_anotacoes_alvo >= 1),
  prioridade       INTEGER NOT NULL DEFAULT 0,
  status           TEXT NOT NULL DEFAULT 'aberta'
                     CHECK (status IN ({_lista(STATUS_TAREFA)})),
  db_build_id      TEXT,
  criado_em        TEXT NOT NULL DEFAULT ({_agora()})
);
-- O índice da FILA, na ordem exata em que o claim do modo locked procura:
-- status='aberta' AND tipo=? ORDER BY prioridade DESC, id.
CREATE INDEX IF NOT EXISTS idx_tarefas_fila   ON tarefas(status, tipo, prioridade DESC, id);
-- E o de "que tarefas existem para este prompt?", que o catálogo do modo livre
-- usa para o find-or-create e para marcar "você já anotou este".
CREATE INDEX IF NOT EXISTS idx_tarefas_prompt ON tarefas(prompt_uid, tipo);
CREATE INDEX IF NOT EXISTS idx_tarefas_projeto ON tarefas(projeto_id, status);

-- ---------------------------------------------------------------------------
-- QUEM PEGOU O QUÊ  (a trava)
-- ---------------------------------------------------------------------------
-- UNIQUE(tarefa_id, anotador_id) é a trava anti-repetição: ninguém anota a
-- mesma tarefa duas vezes, e o re-trabalho depois de uma devolução REUSA esta
-- linha (o que versiona é `anotacoes.versao`). Sem o UNIQUE, uma devolução
-- viraria uma segunda atribuição e o mesmo trabalho contaria duas vezes nas
-- métricas.
--
-- CASCADE só de `tarefas`: apagar uma tarefa leva as atribuições e, por elas,
-- as anotações. Apagar um ANOTADOR não cascateia de propósito — o histórico
-- dele é o que as métricas somam; para tirá-lo de circulação existe `ativo=0`.
CREATE TABLE IF NOT EXISTS atribuicoes (
  id           INTEGER PRIMARY KEY,
  tarefa_id    INTEGER NOT NULL REFERENCES tarefas(id) ON DELETE CASCADE,
  anotador_id  INTEGER NOT NULL REFERENCES anotadores(id),
  status       TEXT NOT NULL DEFAULT 'em_andamento'
                 CHECK (status IN ({_lista(STATUS_ATRIBUICAO)})),
  iniciada_em  TEXT NOT NULL DEFAULT ({_agora()}),
  -- Prazo do claim ([annotate] claim_ttl_min). NULL = sem prazo (as do modo
  -- livre, que o anotador escolheu e ninguém mais está esperando).
  expira_em    TEXT,
  terminada_em TEXT,
  UNIQUE (tarefa_id, anotador_id)
);
-- "as minhas", a listagem que o anotador abre em toda sessão.
CREATE INDEX IF NOT EXISTS idx_atribuicoes_anotador ON atribuicoes(anotador_id, status);
-- "quantas vagas ainda tem esta tarefa?" — a conta que o claim faz sob
-- BEGIN IMMEDIATE, e a varredura da expiração preguiçosa.
CREATE INDEX IF NOT EXISTS idx_atribuicoes_vagas    ON atribuicoes(tarefa_id, status);
CREATE INDEX IF NOT EXISTS idx_atribuicoes_prazo    ON atribuicoes(status, expira_em);

-- ---------------------------------------------------------------------------
-- O TRABALHO
-- ---------------------------------------------------------------------------
-- `payload_schema` é o contrato do `payload_json` ("comparar_ab@1"). Ele existe
-- como COLUNA, e não como conhecimento implícito do código, para que uma
-- anotação de 2026 continue legível quando o formato do payload evoluir: a
-- versão viaja com o dado, não com a versão da app que o leu por último.
--
-- UNIQUE(atribuicao_id, versao) + rejeição gerando versao+1: a história inteira
-- do re-trabalho fica no banco. É isso que o painel do admin lê para mostrar
-- taxa de devolução, e é o que um avaliador procura quando quer saber se a
-- plataforma tem QC de verdade.
--
-- `tempo_ativo_ms` é medido, mas NUNCA mostrado a quem anota (só ao admin):
-- cronômetro à vista adiciona pressão sem melhorar decisão, e aqui não há
-- pagamento por hora para justificá-lo.
CREATE TABLE IF NOT EXISTS anotacoes (
  id             INTEGER PRIMARY KEY,
  atribuicao_id  INTEGER NOT NULL REFERENCES atribuicoes(id) ON DELETE CASCADE,
  versao         INTEGER NOT NULL DEFAULT 1 CHECK (versao >= 1),
  payload_schema TEXT NOT NULL,
  -- QUAL REGRA VALIA quando esta anotação foi feita. O `payload_schema` versiona
  -- o FORMATO; esta coluna versiona a INSTRUÇÃO. São coisas diferentes e as duas
  -- mudam sozinhas: dá para ajustar o texto do A/B sem mexer no formato.
  versao_diretriz INTEGER,
  payload_json   TEXT NOT NULL,
  -- A NOTA-ALVO ESCONDIDA de uma anotação SINTÉTICA (P3b; quem preenche é a
  -- campanha do P4c). NULL em 100% do trabalho humano, e é essa nulidade que
  -- define o que é humano: nada aqui marca "sintética" duas vezes.
  --
  -- Formato: um objeto com as chaves de CHAVES_GABARITO_AVALIACAO —
  -- `avaliacao_antes` (uma de AVALIACOES_ANTES), `familia_defeito` (o catálogo
  -- do P4c) e `nota` (texto livre).
  -- Sem CHECK, pela razão do `eventos.acao`: o catálogo de defeitos cresce a
  -- cada rodada da campanha, e migrar um banco com trabalho humano dentro só
  -- para registrar o NOME de uma família nova seria absurdo.
  --
  -- **NUNCA sai pela API do revisor.** É a mesma disciplina do
  -- `tarefas.gabarito_json`: o envelope é montado campo a campo e a coluna não
  -- entra no SELECT (ver `tarefas.hidratar_anotacao`). Um alvo visível mede a
  -- capacidade de ler JSON, não a calibração de quem avalia. Quem consome é o
  -- painel do admin (P5a), comparando a avaliação do revisor com o alvo.
  gabarito_avaliacao_json TEXT,
  tempo_ativo_ms INTEGER NOT NULL DEFAULT 0 CHECK (tempo_ativo_ms >= 0),
  iniciada_em    TEXT,
  submetida_em   TEXT NOT NULL DEFAULT ({_agora()}),
  status         TEXT NOT NULL DEFAULT 'pendente_triagem'
                   CHECK (status IN ({_lista(STATUS_ANOTACAO)})),
  UNIQUE (atribuicao_id, versao)
);
-- A fila da TRIAGEM é exatamente `status='pendente_triagem' ORDER BY id`, e a
-- do Rate and Review é `status='pendente_avaliacao' ORDER BY id`. O mesmo
-- índice serve às duas.
CREATE INDEX IF NOT EXISTS idx_anotacoes_fila ON anotacoes(status, id);

-- ---------------------------------------------------------------------------
-- A CONVERSA COM O MODELO LOCAL  (P4d)
-- ---------------------------------------------------------------------------
-- A única tabela deste schema que guarda trabalho produzido ANTES do envio: o
-- anotador conversa por vinte minutos e só então existe uma linha em
-- `anotacoes`. Três coisas dependem de ela estar aqui, e não no localStorage:
--
-- 1. **O duelo é cego de verdade.** Qual modelo é "A" nesta rodada nunca sai
--    deste banco antes de a avaliação ser enviada. Se a tela soubesse, a
--    preferência seria sobre a marca e não sobre o texto — e o dado inteiro
--    perderia o valor.
-- 2. **Quem respondeu fica registrado com NOME e DIGEST.** A tag (`qwen3:4b`) é
--    reescrita quando o autor republica o modelo; o digest é o conteúdo. Sem
--    ele, "o B era melhor" não significa nada daqui a seis meses.
-- 3. **Trinta turnos não se perdem.** Fechar a aba, recarregar a página ou
--    trocar de máquina não custa a conversa.
--
-- `ordem` é a posição do turno na conversa (0 = o primeiro do humano). Numa
-- rodada de duelo as DUAS respostas compartilham a mesma `ordem` e se
-- distinguem pelo `rotulo` — é isso que o UNIQUE diz.
--
-- `escolhida` marca a vencedora da rodada, e é ela que faz a conversa
-- continuar: o histórico mandado ao modelo no turno seguinte leva a vencedora,
-- nunca as duas.
CREATE TABLE IF NOT EXISTS turnos_conversa (
  id            INTEGER PRIMARY KEY,
  atribuicao_id INTEGER NOT NULL REFERENCES atribuicoes(id) ON DELETE CASCADE,
  ordem         INTEGER NOT NULL CHECK (ordem >= 0),
  papel         TEXT NOT NULL CHECK (papel IN ({_lista(PAPEIS_TURNO)})),
  -- '' no turno do humano e na conversa de um modelo só: eles não têm lado.
  rotulo        TEXT NOT NULL DEFAULT '' CHECK (rotulo IN ('', {_lista(ROTULOS_DUELO)})),
  texto         TEXT NOT NULL,
  -- O `message.thinking` do Ollama, quando existe. Guardado, e NÃO filtrado: o
  -- raciocínio exposto é conteúdo que o anotador pode querer avaliar, e
  -- descartá-lo em silêncio seria decidir por ele que não conta. Fica numa
  -- coluna à parte porque não é a resposta — a tela o mostra recolhido.
  raciocinio    TEXT NOT NULL DEFAULT '',
  -- NOSSO teto de tokens (`ollama_num_predict`) cortou este turno? Marcado para
  -- que a parede cortada não pareça uma decisão do modelo.
  truncado      INTEGER NOT NULL DEFAULT 0 CHECK (truncado IN (0,1)),
  -- Vazios no turno do humano. NOT NULL com default '' pela regra do raw: aqui
  -- também, ausência é string vazia, nunca NULL — um `modelo IS NULL` e um
  -- `modelo = ''` significariam a mesma coisa e seriam consultados diferente.
  modelo        TEXT NOT NULL DEFAULT '',
  digest        TEXT NOT NULL DEFAULT '',
  escolhida     INTEGER NOT NULL DEFAULT 0 CHECK (escolhida IN (0,1)),
  -- Por que ESTA venceu ESTA rodada. É a preferência multi-turno: sem o motivo,
  -- "o B era melhor" não treina nada — a mesma razão do `comparar_ab`.
  justificativa TEXT,
  duracao_ms    INTEGER NOT NULL DEFAULT 0 CHECK (duracao_ms >= 0),
  criado_em     TEXT NOT NULL DEFAULT ({_agora()}),
  UNIQUE (atribuicao_id, ordem, rotulo)
);
CREATE INDEX IF NOT EXISTS idx_turnos_conversa ON turnos_conversa(atribuicao_id, ordem, rotulo);

-- PASSAGEM 1 — A TRIAGEM. Aprova ou devolve, sem escore. É a ÚNICA passagem que
-- devolve trabalho ao anotador; o que ela aprova não volta mais para quem
-- anotou (segue para o Rate and Review, que corrige no lugar).
--
-- UNIQUE(anotacao_id): uma triagem por VERSÃO da anotação — o re-trabalho cria
-- uma versão nova, que é triada de novo, e as duas coexistem apontando para
-- linhas diferentes.
--
-- O CHECK do comentário é a regra de produto escrita no banco: devolver sem
-- dizer o que corrigir devolve trabalho para o anotador sem devolver
-- informação. A tela reforça ("o comentário volta para o anotador — diga o que
-- corrigir"), mas quem garante é o CHECK.
CREATE TABLE IF NOT EXISTS revisoes (
  id          INTEGER PRIMARY KEY,
  anotacao_id INTEGER NOT NULL UNIQUE REFERENCES anotacoes(id) ON DELETE CASCADE,
  revisor_id  INTEGER NOT NULL REFERENCES anotadores(id),
  veredito    TEXT NOT NULL CHECK (veredito IN ({_lista(VEREDITOS)})),
  comentario  TEXT,
  -- MODO SOLO: o revisor É o autor. Fica GRAVADO, e não deduzido depois, porque
  -- `atribuicoes.anotador_id` pode ser corrigido por um admin e a linha passaria
  -- a mentir sobre uma revisão que já aconteceu. O export e o painel declaram
  -- este número: uma taxa de aprovação calculada sobre autorrevisão não é a
  -- mesma coisa que uma calculada sobre revisão cruzada, e apresentar as duas
  -- como se fossem é o tipo de silêncio que este projeto não pratica.
  autorrevisao INTEGER NOT NULL DEFAULT 0 CHECK (autorrevisao IN (0,1)),
  criada_em   TEXT NOT NULL DEFAULT ({_agora()}),
  CHECK (veredito <> 'devolvida' OR (comentario IS NOT NULL AND trim(comentario) <> ''))
);
CREATE INDEX IF NOT EXISTS idx_revisoes_revisor ON revisoes(revisor_id, criada_em);

-- PASSAGEM 2 — RATE AND REVIEW (as telas chegam no P3b; o schema entra agora).
-- Duas escalas e uma justificativa SEMPRE obrigatória. `avaliacao_antes` mede o
-- trabalho COMO CHEGOU e `avaliacao_depois` mede o RESULTADO depois de o
-- revisor corrigir no lugar — a distância entre as duas é o que o painel do
-- admin lê para dizer quanto de conserto cada anotador custa.
--
-- A métrica mais interessante do conjunto nasce daqui: item que a triagem
-- APROVOU e que o Rate and Review julgou `inutilizavel`/`incorrigivel` mede o
-- REVISOR DA TRIAGEM, não o anotador.
CREATE TABLE IF NOT EXISTS avaliacoes (
  id                     INTEGER PRIMARY KEY,
  anotacao_id            INTEGER NOT NULL UNIQUE REFERENCES anotacoes(id) ON DELETE CASCADE,
  revisor_id             INTEGER NOT NULL REFERENCES anotadores(id),
  avaliacao_antes        TEXT NOT NULL CHECK (avaliacao_antes IN ({_lista(AVALIACOES_ANTES)})),
  avaliacao_depois       TEXT NOT NULL CHECK (avaliacao_depois IN ({_lista(AVALIACOES_DEPOIS)})),
  -- NOT NULL e não-vazio: uma avaliação sem motivo é um número que ninguém
  -- consegue contestar nem aprender com.
  justificativa          TEXT NOT NULL CHECK (trim(justificativa) <> ''),
  -- O payload DEPOIS das correções do revisor. NULL quando ele não mexeu em
  -- nada — e nesse caso `edicoes_avaliacao` também está vazia para esta linha.
  payload_corrigido_json TEXT,
  -- Mesma razão do campo homônimo em `revisoes`: no modo solo, quem avalia é
  -- quem escreveu, e o número que sai daqui precisa dizer isso.
  autorrevisao           INTEGER NOT NULL DEFAULT 0 CHECK (autorrevisao IN (0,1)),
  tempo_ativo_ms         INTEGER NOT NULL DEFAULT 0 CHECK (tempo_ativo_ms >= 0),
  criada_em              TEXT NOT NULL DEFAULT ({_agora()})
);
CREATE INDEX IF NOT EXISTS idx_avaliacoes_revisor ON avaliacoes(revisor_id, criada_em);
CREATE INDEX IF NOT EXISTS idx_avaliacoes_depois  ON avaliacoes(avaliacao_depois, id);

-- O DIFF da passagem 2, campo a campo. `campo` é o CAMINHO no payload
-- ("notas.2.nota", "justificativa"), e não um nome de coluna: o payload é JSON
-- versionado e o caminho é a única referência estável dentro dele.
--
-- `motivo` NOT NULL: cada alteração exige a própria justificativa curta. Sem
-- isso, o Rate and Review vira "o revisor mudou as notas" e ninguém consegue
-- dizer se ele corrigiu um erro ou impôs o gosto dele.
CREATE TABLE IF NOT EXISTS edicoes_avaliacao (
  id           INTEGER PRIMARY KEY,
  avaliacao_id INTEGER NOT NULL REFERENCES avaliacoes(id) ON DELETE CASCADE,
  campo        TEXT NOT NULL,
  valor_antes  TEXT,
  valor_depois TEXT,
  motivo       TEXT NOT NULL CHECK (trim(motivo) <> '')
);
CREATE INDEX IF NOT EXISTS idx_edicoes_avaliacao ON edicoes_avaliacao(avaliacao_id, id);

-- A ESCALAÇÃO. Só `borderline_admin` chega aqui, e o admin ENCERRA o item —
-- aprovando, devolvendo ou descartando. UNIQUE(avaliacao_id) porque a decisão
-- é final: uma segunda decisão sobre a mesma avaliação seria uma reabertura
-- silenciosa do que já foi encerrado.
CREATE TABLE IF NOT EXISTS decisoes_admin (
  id           INTEGER PRIMARY KEY,
  avaliacao_id INTEGER NOT NULL UNIQUE REFERENCES avaliacoes(id) ON DELETE CASCADE,
  admin_id     INTEGER NOT NULL REFERENCES anotadores(id),
  decisao      TEXT NOT NULL CHECK (decisao IN ({_lista(DECISOES_ADMIN)})),
  comentario   TEXT,
  criada_em    TEXT NOT NULL DEFAULT ({_agora()})
);

-- ---------------------------------------------------------------------------
-- TRILHA DE AUDITORIA
-- ---------------------------------------------------------------------------
-- Quem fez o quê, quando. Escrita nas transições que importam: claim,
-- submissão, abandono, triagem, avaliação, decisão do admin.
--
-- `acao` NÃO tem CHECK, e é a única exceção da regra deste schema. O motivo é o
-- mesmo do `task_type` do corpus: o vocabulário de ações cresce a cada marco, e
-- migrar um banco que guarda trabalho humano só para registrar o NOME de um
-- evento novo seria absurdo. As ações conhecidas estão em `eventos.ACOES`.
--
-- `entidade_id` não é FK: um evento sobre uma tarefa apagada continua sendo um
-- fato que aconteceu, e uma FK com CASCADE apagaria justamente o registro de
-- que ela existiu.
CREATE TABLE IF NOT EXISTS eventos (
  id           INTEGER PRIMARY KEY,
  ator_id      INTEGER REFERENCES anotadores(id),
  acao         TEXT NOT NULL,
  entidade     TEXT NOT NULL,
  entidade_id  INTEGER,
  detalhe_json TEXT NOT NULL DEFAULT '{{}}',
  criado_em    TEXT NOT NULL DEFAULT ({_agora()})
);
CREATE INDEX IF NOT EXISTS idx_eventos_entidade ON eventos(entidade, entidade_id, id);
CREATE INDEX IF NOT EXISTS idx_eventos_ator     ON eventos(ator_id, id);

-- ---------------------------------------------------------------------------
-- MATERIAL DE TRABALHO  (fixtures hoje, conteúdo real quando o dono decidir)
-- ---------------------------------------------------------------------------
-- Rubricas: nascem `fixture` (pacote de demonstração) ou `anotacao` (a aba
-- escrever-rubrica, materializada AQUI na aprovação do revisor — antes disso a
-- rubrica proposta vive só no payload da anotação). `criterios_json` segue o
-- contrato 'rubrica@1'.
CREATE TABLE IF NOT EXISTS rubricas (
  id             INTEGER PRIMARY KEY,
  prompt_uid     TEXT NOT NULL,
  titulo         TEXT NOT NULL,
  criterios_json TEXT NOT NULL,
  origem         TEXT NOT NULL CHECK (origem IN ({_lista(ORIGENS_RUBRICA)})),
  -- SET NULL, não CASCADE: uma rubrica aprovada já foi usada para avaliar
  -- outras coisas; apagar a anotação que a originou não pode apagar o
  -- instrumento de medida junto.
  anotacao_id    INTEGER REFERENCES anotacoes(id) ON DELETE SET NULL,
  status         TEXT NOT NULL DEFAULT 'ativa' CHECK (status IN ('ativa','arquivada')),
  criada_em      TEXT NOT NULL DEFAULT ({_agora()})
);
CREATE INDEX IF NOT EXISTS idx_rubricas_prompt ON rubricas(prompt_uid, status);

-- Respostas de modelo do A/B. `rotulo_modelo` é CEGO ('modelo-a'/'modelo-b') e
-- o `meta_json` guarda o que o anotador não pode ver: qual modelo é, e qual
-- defeito foi plantado ali de propósito. Só o admin lê o meta.
--
-- A decisão futura sobre alimentação real NÃO muda este schema: vira
-- origem='importada'.
CREATE TABLE IF NOT EXISTS respostas_modelo (
  id            INTEGER PRIMARY KEY,
  prompt_uid    TEXT NOT NULL,
  rotulo_modelo TEXT NOT NULL CHECK (rotulo_modelo IN ({_lista(ROTULOS_MODELO)})),
  texto         TEXT NOT NULL,
  origem        TEXT NOT NULL CHECK (origem IN ({_lista(ORIGENS_RESPOSTA)})),
  meta_json     TEXT NOT NULL DEFAULT '{{}}',
  criada_em     TEXT NOT NULL DEFAULT ({_agora()})
);
CREATE INDEX IF NOT EXISTS idx_respostas_prompt ON respostas_modelo(prompt_uid, rotulo_modelo);

-- ---------------------------------------------------------------------------
-- MODO CRIAR  (o anotador escreve o prompt, e ele entra no corpus de verdade)
-- ---------------------------------------------------------------------------
-- É aqui que a Trilha B fecha o objetivo do projeto: o texto atravessa revisão,
-- ingestão (`pf ingest plataforma`) e a pipeline inteira, e reaparece no corpus
-- com uid determinístico. `uid_previsto` é gravado na APROVAÇÃO, calculado com
-- o mesmo `schema.make_uid` que a ingestão vai usar — se os dois divergirem, o
-- badge "no corpus" nunca acende, e é assim que o erro aparece.
--
-- `hash_norm` é a chave do aviso de duplicata contra o corpus (aviso, NUNCA
-- bloqueio: se o texto colapsou com um prompt real, o dedup funcionou, e isso é
-- uma coisa boa para mostrar na tela).
CREATE TABLE IF NOT EXISTS criacoes (
  id                 INTEGER PRIMARY KEY,
  autor_id           INTEGER NOT NULL REFERENCES anotadores(id),
  texto              TEXT NOT NULL,
  lang               TEXT NOT NULL CHECK (lang IN ('pt','en')),
  task_type_sugerido TEXT,
  domain_sugerido    TEXT,
  brief              TEXT,
  hash_norm          TEXT NOT NULL,
  duplicata_corpus   INTEGER NOT NULL DEFAULT 0 CHECK (duplicata_corpus IN (0,1)),
  status             TEXT NOT NULL DEFAULT 'submetida'
                       CHECK (status IN ({_lista(STATUS_CRIACAO)})),
  revisor_id         INTEGER REFERENCES anotadores(id),
  comentario_revisao TEXT,
  revisada_em        TEXT,
  uid_previsto       TEXT,
  exportada_em       TEXT,
  criada_em          TEXT NOT NULL DEFAULT ({_agora()})
);
CREATE INDEX IF NOT EXISTS idx_criacoes_status ON criacoes(status, id);
CREATE INDEX IF NOT EXISTS idx_criacoes_hash   ON criacoes(hash_norm);
CREATE INDEX IF NOT EXISTS idx_criacoes_autor  ON criacoes(autor_id, status);

-- ---------------------------------------------------------------------------
-- O POOL MATERIALIZADO
-- ---------------------------------------------------------------------------
-- Cópia local dos uids que a plataforma pode oferecer. Resolver o pool no
-- corpus a cada request custava 607 ms MEDIDOS (não os 100 ms que a docstring
-- prometia), e ele está no caminho de `/api/catalogo`, `/api/prompts/{{uid}}` e
-- `/api/tarefas/livre` — três rotas do caminho principal.
--
-- Isto NÃO é cache de agregado: é uma projeção com CHAVE DE INVALIDAÇÃO
-- explícita (`pool_assinatura` em `app_meta`, ver `pool.materializar`). Quando
-- o corpus troca por swap ou a configuração muda, a assinatura muda e a tabela
-- é reconstruída inteira, dentro do mesmo request. O modo de falha que este
-- projeto já conhece — "cache mente sob escritor externo" — é justamente o que
-- a assinatura impede: ela é derivada do build do corpus, não do relógio.
--
-- `ordem` é a posição determinística: mesma origem, mesma ordem, sempre. É o
-- que faz `pf annotate seed` gerar as mesmas tarefas em duas máquinas.
CREATE TABLE IF NOT EXISTS pool (
  uid   TEXT PRIMARY KEY,
  ordem INTEGER NOT NULL UNIQUE
);

-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

#: Tabelas próprias, na ordem do DDL. O ``pf annotate status`` conta por esta
#: lista, então uma tabela nova aparece no relatório sozinha.
TABELAS: tuple[str, ...] = (
    "anotadores",
    "projetos",
    "diretrizes",
    "prompts_demo",
    "tarefas",
    "atribuicoes",
    "anotacoes",
    "turnos_conversa",
    "revisoes",
    "avaliacoes",
    "edicoes_avaliacao",
    "decisoes_admin",
    "eventos",
    "rubricas",
    "respostas_modelo",
    "criacoes",
    "pool",
    "app_meta",
)


def uid_demo(texto: str) -> str:
    """Uid de um prompt do pacote de demonstração: ``demo:`` + 12 hex do sha256.

    Endereçado pelo conteúdo, então semear duas vezes a mesma fixture não cria
    duas linhas — o ``INSERT ... ON CONFLICT DO NOTHING`` do seed colapsa. O
    hash é do texto EXATO (sem a normalização canônica do corpus) de propósito:
    fixture é escrita à mão e precisa de um id previsível a partir do que está
    escrito no arquivo, não do que a cadeia de normalização faria com ele.
    """
    return PREFIXO_DEMO + hashlib.sha256(texto.encode("utf-8")).hexdigest()[:12]


def e_demo(uid: str) -> bool:
    """``True`` quando o uid é do pacote de demonstração (e não do corpus).

    É este teste que o resolvedor de prompts usa para decidir em qual banco
    buscar o texto. Uma função, e não um ``startswith`` espalhado por cinco
    módulos, porque o prefixo é contrato — inclusive no CHECK do DDL.
    """
    return str(uid).startswith(PREFIXO_DEMO)


class SchemaDivergente(RuntimeError):
    """O banco fala uma versão de schema que esta app não fala.

    Exceção própria (e não ``RuntimeError`` cru) porque **todo** ponto de
    entrada precisa distinguir este caso: a CLI tem de imprimir a linha do
    conserto (``pf annotate migrate``) em vez de um traceback, e a app tem de
    recusar subir. Um banco que guarda trabalho humano nunca é "recriado".
    """

    def __init__(self, versao: int, esperada: int = SCHEMA_VERSION_ANOTACAO) -> None:
        self.versao = versao
        self.esperada = esperada
        super().__init__(
            f"{CHAVE_VERSAO}={versao} mas esta app fala {esperada}. "
            "Este banco guarda trabalho humano e não se recria a partir da "
            "pipeline — rode `pf annotate migrate` para migrar preservando o "
            "que já está lá (o comando guarda uma cópia do arquivo antigo)."
        )


def versao_do_banco(conn: sqlite3.Connection) -> int | None:
    """A versão gravada, ou ``None`` num banco que ainda não tem ``app_meta``.

    Precisa existir separada de ``get_meta`` porque é chamada ANTES do DDL: num
    arquivo recém-criado a tabela ``app_meta`` não existe, e um ``SELECT`` nela
    levantaria ``OperationalError`` em vez de dizer "banco novo".
    """
    tem = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_meta'"
    ).fetchone()
    if tem is None:
        return None
    bruto = get_meta(conn, CHAVE_VERSAO)
    return None if bruto is None else int(bruto)


def init_db(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Aplica o DDL (idempotente) e semeia a versão do schema em ``app_meta``.

    Chamado no lifespan a cada subida: rodar duas vezes tem de ser um no-op, e é
    por isso que **tudo** no DDL é ``IF NOT EXISTS``.

    **Recusa ANTES de tocar no DDL quando a versão diverge.** Sem essa guarda,
    aplicar o DDL da v2 sobre um banco v1 criaria as tabelas novas e deixaria as
    antigas com os CHECKs velhos — um meio-schema que passa em todo teste de
    existência e falha na primeira transição de status. Meio-migrado é pior que
    não migrado, porque não parece quebrado.
    """
    versao = versao_do_banco(conn)
    if versao is not None and versao != SCHEMA_VERSION_ANOTACAO:
        raise SchemaDivergente(versao)
    conn.executescript(DDL)
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
        (CHAVE_VERSAO, str(SCHEMA_VERSION_ANOTACAO)),
    )
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Grava (upsert) uma chave em ``app_meta``."""
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """Lê uma chave de ``app_meta`` (``default`` se ausente)."""
    linha = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return default if linha is None else str(linha["value"])


def contagens(conn: sqlite3.Connection) -> dict[str, int]:
    """``{tabela: linhas}`` de todas as tabelas — o corpo do ``pf annotate status``.

    Volumes desta app são pequenos (dezenas a milhares de linhas), então
    ``count(*)`` ao vivo é mais barato que qualquer cache — e cache de agregado
    numa app com escritor concorrente é justamente o problema que este projeto
    já resolveu uma vez, do lado da curadoria.
    """
    return {
        tabela: int(conn.execute(f"SELECT count(*) AS n FROM {tabela}").fetchone()["n"])
        for tabela in TABELAS
    }


__all__ = [
    "AVALIACOES_ANTES",
    "AVALIACOES_DEPOIS",
    "CHAVES_GABARITO_AVALIACAO",
    "CHAVE_VERSAO",
    "COLUNA_GABARITO_AVALIACAO",
    "DDL",
    "DECISOES_ADMIN",
    "DESFECHO_AVALIACAO",
    "DESFECHO_DECISAO",
    "ESCALA_TURNO",
    "ORIGENS_RESPOSTA",
    "ORIGENS_RUBRICA",
    "ORIGENS_TAREFA",
    "PAPEIS",
    "PAPEIS_TURNO",
    "PREFIXO_DEMO",
    "ROTULOS_DUELO",
    "ROTULOS_MODELO",
    "SCHEMA_VERSION_ANOTACAO",
    "STATUS_ANOTACAO",
    "STATUS_ATRIBUICAO",
    "STATUS_CRIACAO",
    "STATUS_PROJETO",
    "STATUS_QUALIFICACAO",
    "STATUS_TAREFA",
    "TABELAS",
    "TIPOS_CONVERSA",
    "TIPOS_TAREFA",
    "TRANSICOES",
    "VEREDITOS",
    "SchemaDivergente",
    "contagens",
    "e_demo",
    "get_meta",
    "init_db",
    "set_meta",
    "uid_demo",
    "versao_do_banco",
]
