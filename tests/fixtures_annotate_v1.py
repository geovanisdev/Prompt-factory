"""O schema v1 do ``annotate.sqlite``, **congelado**, para testar a migração.

Por que uma cópia e não ``git show``: a migração 1 → 2 tem de continuar
funcionando sobre o schema que existe no disco de alguém, e esse schema não muda
mais. Um teste que gerasse a v1 a partir do ``db.py`` de hoje testaria a
migração contra um alvo móvel — e passaria justamente no dia em que alguém
mudasse os dois lados juntos, que é o dia em que ela quebraria de verdade.

Este arquivo é uma FOTOGRAFIA do commit ``ae531c8`` (P2). Ele nunca deve ser
"atualizado": uma v3 futura ganha o próprio congelado, ao lado deste.
"""

from __future__ import annotations

import sqlite3
from typing import Any

#: A versão que este DDL representa.
VERSAO_V1 = 1

#: O DDL do P2, literal. Só as tabelas — os defaults de tempo foram mantidos
#: como estavam, porque a migração copia carimbos e não os recalcula.
DDL_V1 = """
CREATE TABLE IF NOT EXISTS anotadores (
  id        INTEGER PRIMARY KEY,
  nome      TEXT NOT NULL UNIQUE,
  papel     TEXT NOT NULL CHECK (papel IN ('anotador', 'revisor', 'admin')),
  ativo     INTEGER NOT NULL DEFAULT 1 CHECK (ativo IN (0,1)),
  criado_em TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS prompts_demo (
  uid       TEXT PRIMARY KEY CHECK (uid LIKE 'demo:%'),
  text      TEXT NOT NULL,
  lang      TEXT NOT NULL CHECK (lang IN ('pt','en')),
  criado_em TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS tarefas (
  id               INTEGER PRIMARY KEY,
  tipo             TEXT NOT NULL CHECK (tipo IN ('avaliar_rubrica', 'escrever_rubrica', 'sft_resposta', 'comparar_ab')),
  prompt_uid       TEXT NOT NULL,
  origem           TEXT NOT NULL CHECK (origem IN ('semente', 'admin', 'livre', 'continuacao')),
  payload_json     TEXT NOT NULL DEFAULT '{}',
  gabarito_json    TEXT,
  n_anotacoes_alvo INTEGER NOT NULL DEFAULT 1 CHECK (n_anotacoes_alvo >= 1),
  prioridade       INTEGER NOT NULL DEFAULT 0,
  status           TEXT NOT NULL DEFAULT 'aberta'
                     CHECK (status IN ('aberta', 'pausada', 'concluida')),
  db_build_id      TEXT,
  criado_em        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_tarefas_fila   ON tarefas(status, tipo, prioridade DESC, id);
CREATE INDEX IF NOT EXISTS idx_tarefas_prompt ON tarefas(prompt_uid, tipo);

CREATE TABLE IF NOT EXISTS atribuicoes (
  id           INTEGER PRIMARY KEY,
  tarefa_id    INTEGER NOT NULL REFERENCES tarefas(id) ON DELETE CASCADE,
  anotador_id  INTEGER NOT NULL REFERENCES anotadores(id),
  status       TEXT NOT NULL DEFAULT 'em_andamento'
                 CHECK (status IN ('em_andamento', 'submetida', 'aprovada', 'abandonada', 'expirada')),
  iniciada_em  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  expira_em    TEXT,
  terminada_em TEXT,
  UNIQUE (tarefa_id, anotador_id)
);
CREATE INDEX IF NOT EXISTS idx_atribuicoes_anotador ON atribuicoes(anotador_id, status);
CREATE INDEX IF NOT EXISTS idx_atribuicoes_vagas    ON atribuicoes(tarefa_id, status);
CREATE INDEX IF NOT EXISTS idx_atribuicoes_prazo    ON atribuicoes(status, expira_em);

CREATE TABLE IF NOT EXISTS anotacoes (
  id             INTEGER PRIMARY KEY,
  atribuicao_id  INTEGER NOT NULL REFERENCES atribuicoes(id) ON DELETE CASCADE,
  versao         INTEGER NOT NULL DEFAULT 1 CHECK (versao >= 1),
  payload_schema TEXT NOT NULL,
  payload_json   TEXT NOT NULL,
  tempo_ativo_ms INTEGER NOT NULL DEFAULT 0 CHECK (tempo_ativo_ms >= 0),
  iniciada_em    TEXT,
  submetida_em   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  status         TEXT NOT NULL DEFAULT 'pendente_revisao'
                   CHECK (status IN ('pendente_revisao', 'aprovada', 'rejeitada')),
  UNIQUE (atribuicao_id, versao)
);
CREATE INDEX IF NOT EXISTS idx_anotacoes_fila ON anotacoes(status, id);

CREATE TABLE IF NOT EXISTS revisoes (
  id          INTEGER PRIMARY KEY,
  anotacao_id INTEGER NOT NULL UNIQUE REFERENCES anotacoes(id) ON DELETE CASCADE,
  revisor_id  INTEGER NOT NULL REFERENCES anotadores(id),
  veredito    TEXT NOT NULL CHECK (veredito IN ('aprovada', 'rejeitada')),
  comentario  TEXT,
  criada_em   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  CHECK (veredito <> 'rejeitada' OR (comentario IS NOT NULL AND trim(comentario) <> ''))
);
CREATE INDEX IF NOT EXISTS idx_revisoes_revisor ON revisoes(revisor_id, criada_em);

CREATE TABLE IF NOT EXISTS rubricas (
  id             INTEGER PRIMARY KEY,
  prompt_uid     TEXT NOT NULL,
  titulo         TEXT NOT NULL,
  criterios_json TEXT NOT NULL,
  origem         TEXT NOT NULL CHECK (origem IN ('fixture', 'anotacao')),
  anotacao_id    INTEGER REFERENCES anotacoes(id) ON DELETE SET NULL,
  status         TEXT NOT NULL DEFAULT 'ativa' CHECK (status IN ('ativa','arquivada')),
  criada_em      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_rubricas_prompt ON rubricas(prompt_uid, status);

CREATE TABLE IF NOT EXISTS respostas_modelo (
  id            INTEGER PRIMARY KEY,
  prompt_uid    TEXT NOT NULL,
  rotulo_modelo TEXT NOT NULL CHECK (rotulo_modelo IN ('modelo-a', 'modelo-b')),
  texto         TEXT NOT NULL,
  origem        TEXT NOT NULL CHECK (origem IN ('fixture', 'importada')),
  meta_json     TEXT NOT NULL DEFAULT '{}',
  criada_em     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_respostas_prompt ON respostas_modelo(prompt_uid, rotulo_modelo);

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
                       CHECK (status IN ('submetida', 'aprovada', 'rejeitada', 'exportada')),
  revisor_id         INTEGER REFERENCES anotadores(id),
  comentario_revisao TEXT,
  revisada_em        TEXT,
  uid_previsto       TEXT,
  exportada_em       TEXT,
  criada_em          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_criacoes_status ON criacoes(status, id);
CREATE INDEX IF NOT EXISTS idx_criacoes_hash   ON criacoes(hash_norm);
CREATE INDEX IF NOT EXISTS idx_criacoes_autor  ON criacoes(autor_id, status);

CREATE TABLE IF NOT EXISTS app_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def init_v1(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Aplica o DDL congelado e carimba ``schema_version_anotacao = 1``."""
    conn.executescript(DDL_V1)
    conn.execute(
        "INSERT INTO app_meta (key, value) VALUES ('schema_version_anotacao', '1') "
        "ON CONFLICT(key) DO NOTHING"
    )
    return conn


__all__ = [
    "DDL_V1",
    "FALTAVA_NA_VERSAO",
    "VERSAO_V1",
    "contagens_do_disco",
    "init_v1",
    "rebaixar",
]


#: O que cada versão do schema **ainda não tinha**, para rebaixar um banco de
#: hoje ao estado real de uma versão antiga.
#:
#: POR QUE ISTO PRECISOU EXISTIR
#: =============================
#: Os testes de migração constroem o banco antigo do jeito mais fiel possível:
#: criam um banco de HOJE, fazem trabalho de verdade nele pelas rotas, e só então
#: carimbam a versão antiga. O carimbo sozinho, porém, não rebaixa nada — a
#: tabela e a coluna do marco novo continuam lá. Enquanto o marco novo só
#: acrescentava tabela VAZIA isso passava despercebido; no dia em que o seed
#: passou a POPULAR a tabela nova (os dois briefs do P9), a conferência de
#: contagens da migração acusou "2 linhas antes, 0 depois" — e estava certa: o
#: banco de origem não devia tê-las.
#:
#: Chaveado pela versão de ORIGEM que se quer simular. Uma entrada nova a cada
#: marco que acrescente tabela ou coluna.
#:
#: ``colunas`` é ``{tabela: (colunas)}`` e não uma lista solta de colunas de
#: ``anotacoes``: até a v6 só ``anotacoes`` ganhava coluna, e a chave chamada
#: ``colunas_anotacoes`` escondia essa premissa em vez de declará-la. A v7
#: acrescenta coluna em ``criacoes``, e uma premissa escondida quebra calada.
FALTAVA_NA_VERSAO: dict[int, dict[str, Any]] = {
    2: {
        "tabelas": ("briefs", "pedidos"),
        "colunas": {
            "anotacoes": ("gabarito_avaliacao_json", "versao_brief"),
            "criacoes": ("pedido_id", "material_json"),
        },
    },
    3: {
        "tabelas": ("briefs", "pedidos"),
        "colunas": {
            "anotacoes": ("versao_brief",),
            "criacoes": ("pedido_id", "material_json"),
        },
    },
    4: {
        "tabelas": ("briefs", "turnos_conversa", "pedidos"),
        "colunas": {
            "anotacoes": ("versao_brief",),
            "criacoes": ("pedido_id", "material_json"),
        },
    },
    5: {
        "tabelas": ("briefs", "pedidos"),
        "colunas": {
            "anotacoes": ("versao_brief",),
            "criacoes": ("pedido_id", "material_json"),
        },
    },
    6: {
        "tabelas": ("pedidos",),
        "colunas": {"criacoes": ("pedido_id", "material_json")},
    },
}


def rebaixar(conn: sqlite3.Connection, para: int) -> None:
    """Tira do banco o que a versão ``para`` ainda não tinha. NÃO carimba a versão.

    Não carimba de propósito: quem chama costuma querer contar linhas depois do
    rebaixamento e antes do carimbo, e um carimbo escondido aqui faria a ordem
    dessas três coisas virar detalhe implícito.

    **As colunas saem ANTES das tabelas**, e a ordem é obrigatória a partir da
    v7: ``criacoes.pedido_id`` referencia ``pedidos``, e derrubar a tabela
    primeiro deixaria ``criacoes`` apontando para uma tabela inexistente — o
    próximo INSERT nela morreria com ``no such table``, com ``foreign_keys=ON``
    (que ``db.connect`` liga sempre).

    Restrição do SQLite que decide o desenho de quem acrescentar coluna daqui
    para a frente, **medida** (3.49.1) e não suposta: ``ALTER TABLE ... DROP
    COLUMN`` funciona sobre uma coluna com ``REFERENCES``, mas **não** sobre uma
    coluna que tenha ÍNDICE (``error in index ... after drop column``). Uma
    coluna nova indexada em tabela existente não pode ser rebaixada aqui sem que
    o plano derrube o índice junto.
    """
    if para not in FALTAVA_NA_VERSAO:
        raise ValueError(f"não sei rebaixar para a v{para}")
    plano = FALTAVA_NA_VERSAO[para]
    for tabela, colunas in plano["colunas"].items():
        for coluna in colunas:
            conn.execute(f"ALTER TABLE {tabela} DROP COLUMN {coluna}")
    for tabela in plano["tabelas"]:
        conn.execute(f"DROP TABLE IF EXISTS {tabela}")


def contagens_do_disco(conn: sqlite3.Connection) -> dict[str, int]:
    """``{tabela: linhas}`` lido do ``sqlite_master``, não de uma lista fixa.

    ``db.contagens`` percorre ``db.TABELAS``, que descreve o schema de HOJE:
    sobre um banco rebaixado ela pediria uma tabela que aquela versão nunca teve
    e morreria com ``no such table``. A própria migração conta assim
    (``_contagens_brutas``), e é assim que o teste precisa contar para comparar
    as duas pontas da mesma operação.
    """
    tabelas = [
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {
        t: int(conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]) for t in tabelas
    }
