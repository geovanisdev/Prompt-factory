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
SCHEMA_VERSION_ANOTACAO = 1

#: Chave de ``app_meta`` onde a versão acima mora.
CHAVE_VERSAO = "schema_version_anotacao"

#: Papéis. Sem senha: é teatro de autorização de uma demonstração, e está
#: escrito na tela ("demonstração — sem login"). O servidor ainda assim valida o
#: papel em toda rota mutante — teatro mal feito viraria bug de verdade no dia
#: em que alguém pusesse isto na rede.
PAPEIS: tuple[str, ...] = ("anotador", "revisor", "admin")

#: Os quatro estilos de tarefa (as quatro abas do anotador). São também o
#: prefixo do ``payload_schema`` das anotações: ``comparar_ab@1``.
TIPOS_TAREFA: tuple[str, ...] = (
    "avaliar_rubrica",
    "escrever_rubrica",
    "sft_resposta",
    "comparar_ab",
)

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

#: Estados de uma anotação submetida. Rejeitada NÃO apaga nada: o re-trabalho
#: grava ``versao + 1`` e a história fica.
STATUS_ANOTACAO: tuple[str, ...] = ("pendente_revisao", "aprovada", "rejeitada")

#: Veredito do revisor. Os mesmos dois valores do ``status`` da anotação que ele
#: produz, de propósito: um veredito que não vira estado seria decoração.
VEREDITOS: tuple[str, ...] = ("aprovada", "rejeitada")

#: Origem de rubricas e respostas de modelo. ``fixture`` = pacote de
#: demonstração; ``anotacao`` = materializada da aba escrever-rubrica na
#: aprovação; ``importada`` = respostas de modelo reais, quando o dono decidir
#: como alimentar a plataforma. Essa decisão futura **não muda o schema**.
ORIGENS_RUBRICA: tuple[str, ...] = ("fixture", "anotacao")
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
  criado_em TEXT NOT NULL DEFAULT ({_agora()})
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
  payload_json   TEXT NOT NULL,
  tempo_ativo_ms INTEGER NOT NULL DEFAULT 0 CHECK (tempo_ativo_ms >= 0),
  iniciada_em    TEXT,
  submetida_em   TEXT NOT NULL DEFAULT ({_agora()}),
  status         TEXT NOT NULL DEFAULT 'pendente_revisao'
                   CHECK (status IN ({_lista(STATUS_ANOTACAO)})),
  UNIQUE (atribuicao_id, versao)
);
-- A fila do revisor é exatamente `status='pendente_revisao' ORDER BY id`.
CREATE INDEX IF NOT EXISTS idx_anotacoes_fila ON anotacoes(status, id);

-- O veredito. UNIQUE(anotacao_id): uma revisão por VERSÃO da anotação — o
-- re-trabalho cria uma versão nova, que é revisada de novo, e as duas revisões
-- coexistem apontando para linhas diferentes.
--
-- O CHECK do comentário é a regra de produto escrita no banco: rejeitar sem
-- dizer o que corrigir devolve trabalho para o anotador sem devolver
-- informação. A tela reforça ("o comentário volta para o anotador — diga o que
-- corrigir"), mas quem garante é o CHECK.
CREATE TABLE IF NOT EXISTS revisoes (
  id          INTEGER PRIMARY KEY,
  anotacao_id INTEGER NOT NULL UNIQUE REFERENCES anotacoes(id) ON DELETE CASCADE,
  revisor_id  INTEGER NOT NULL REFERENCES anotadores(id),
  veredito    TEXT NOT NULL CHECK (veredito IN ({_lista(VEREDITOS)})),
  comentario  TEXT,
  criada_em   TEXT NOT NULL DEFAULT ({_agora()}),
  CHECK (veredito <> 'rejeitada' OR (comentario IS NOT NULL AND trim(comentario) <> ''))
);
CREATE INDEX IF NOT EXISTS idx_revisoes_revisor ON revisoes(revisor_id, criada_em);

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
CREATE TABLE IF NOT EXISTS app_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

#: Tabelas próprias, na ordem do DDL. O ``pf annotate status`` conta por esta
#: lista, então uma tabela nova aparece no relatório sozinha.
TABELAS: tuple[str, ...] = (
    "anotadores",
    "prompts_demo",
    "tarefas",
    "atribuicoes",
    "anotacoes",
    "revisoes",
    "rubricas",
    "respostas_modelo",
    "criacoes",
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


def init_db(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Aplica o DDL (idempotente) e semeia a versão do schema em ``app_meta``.

    Chamado no lifespan a cada subida: rodar duas vezes tem de ser um no-op, e é
    por isso que **tudo** no DDL é ``IF NOT EXISTS``. O ``DO NOTHING`` na versão
    é o mesmo cuidado: um banco de schema 1 continua dizendo 1 depois de a app
    subir de novo, mesmo que a constante já tenha andado (aí quem decide é a
    migração, não um UPDATE silencioso).
    """
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
    "CHAVE_VERSAO",
    "DDL",
    "ORIGENS_RESPOSTA",
    "ORIGENS_RUBRICA",
    "ORIGENS_TAREFA",
    "PAPEIS",
    "PREFIXO_DEMO",
    "ROTULOS_MODELO",
    "SCHEMA_VERSION_ANOTACAO",
    "STATUS_ANOTACAO",
    "STATUS_ATRIBUICAO",
    "STATUS_CRIACAO",
    "STATUS_TAREFA",
    "TABELAS",
    "TIPOS_TAREFA",
    "VEREDITOS",
    "contagens",
    "e_demo",
    "get_meta",
    "init_db",
    "set_meta",
    "uid_demo",
]
