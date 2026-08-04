"""``pf annotate migrate`` — sobe o schema **preservando o trabalho humano**.

O ``annotate.sqlite`` é o único banco deste projeto que a pipeline **não sabe
refazer**. O corpus se reconstrói com um ``pf run`` de algumas horas; uma
anotação, não: quem a escreveu já foi embora e não dá para perguntar de novo. Por
isso o lifespan recusa subir sobre um schema divergente (decisão do P1) — e por
isso a recusa precisa vir acompanhada de um caminho, que é este comando.

RECONSTRUIR AO LADO E TROCAR, EM VEZ DE ALTERAR NO LUGAR
=========================================================
É o mesmo padrão que o ``s11`` já usa para o corpus: constrói um arquivo novo e
faz ``os.replace``. Três propriedades que ``ALTER TABLE`` não daria:

1. **O schema migrado é IDÊNTICO ao de um banco novo**, por construção — o
   arquivo novo nasce do ``db.DDL``, não de uma sequência de remendos. Há um
   teste que compara os dois ``sqlite_master``, e ele só é possível assim. Um
   ``ALTER`` daria colunas na ordem errada, CHECKs velhos sobrevivendo e um
   ``anotacoes`` que aceita ``pendente_revisao`` num banco que diz falar v2.
2. **Falhar no meio não estraga nada**: o original não é tocado até o fim.
3. **A cópia é explícita, coluna a coluna.** Um ``SELECT *`` funcionaria hoje e
   perderia dados em silêncio na próxima migração; aqui, uma coluna nova sem
   tratamento é um ``KeyError`` na hora, não um NULL descoberto meses depois.

O arquivo antigo **não é apagado**: vira ``<nome>.v<N>.bak`` ao lado. Custo: um
arquivo de alguns MB. Benefício: a operação inteira é reversível com um ``mv``.

N ORIGENS, UM DESTINO
=====================
O destino é **sempre** o schema corrente, e o arquivo novo nasce do ``db.DDL``
— nunca de uma sequência de remendos encadeados. Não há "migrar 1→2, 2→3 e
3→4": há ``_da_v1``, ``_da_v2`` e ``_da_v3``, cada um copiando o que aquele
schema tinha para dentro de um banco já na versão de hoje. Encadear passos
exigiria manter vivo o DDL de cada versão intermediária, e o teste que compara
``sqlite_master`` com um banco novo deixaria de valer no meio da cadeia.

O QUE MUDA NOS DADOS, VINDO DA v1 (P1/P2)
=========================================
* ``anotacoes.status``: ``pendente_revisao`` → ``pendente_triagem``;
  ``aprovada`` → ``pendente_avaliacao`` (a triagem aprovou, o Rate and Review
  ainda não passou); ``rejeitada`` → ``devolvida``.
* ``revisoes.veredito``: ``rejeitada`` → ``devolvida``.
* ``tarefas.projeto_id``: o projeto padrão, criado pela migração.
* ``anotacoes.versao_diretriz``: 1 — o texto que estava literal no
  ``index.html`` **é** a versão 1 do ``fixtures/diretrizes.json``, então
  carimbar 1 é a verdade, não um chute.
* ``anotadores.qualificacoes_json``: ``{}``.

Um status fora do mapa **recusa a migração inteira**, com o id da linha. Migrar
"quase tudo" e deixar duas linhas com status inválido é o desfecho que este
módulo existe para impedir.

O QUE MUDA NOS DADOS, VINDO DA v2 (P3a/P3i)
===========================================
Nada. A v3 acrescenta ``anotacoes.gabarito_avaliacao_json``, que é NULL em todo
trabalho humano — e é justamente essa nulidade que o distingue de uma anotação
sintética. Projetos, diretrizes, avaliações, edições e decisões vêm como estão:
``projetos`` e ``diretrizes`` são copiadas em vez de re-semeadas, porque um
``semear`` sobre a v2 recusaria (com razão) reescrever uma diretriz que já
gravou trabalho, e porque os **ids** de projeto são referenciados por
``tarefas.projeto_id``.

O QUE MUDA NOS DADOS, VINDO DA v3 (P3b)
=======================================
Nada, e nem o formato de nenhuma tabela. A v4 acrescenta um VALOR ao CHECK de
``rubricas.origem`` (``'importada'``). É o caso mais fácil de subestimar: como
não há coluna nova, a tentação é não migrar — só que ``CREATE TABLE IF NOT
EXISTS`` não reescreve o CHECK de uma tabela que já existe, e o banco do dono
continuaria recusando o INSERT da campanha com uma regra que nenhum arquivo do
repositório mostra mais. **Um CHECK velho não parece quebrado: ele parece um
bug do código que está tentando escrever.**

O QUE MUDA NOS DADOS, VINDO DA v4 (P4d)
=======================================
Nada de novo, e pela mesma razão: a v5 alarga DOIS CHECKs (``tarefas.tipo`` e
``diretrizes.tipo`` ganham ``conversa_modelo`` e ``duelo_modelos``) e cria
``turnos_conversa``, que nasce vazia porque nunca houve conversa num banco v4.
As diretrizes dos dois tipos novos ficam para o ``pf annotate seed``: publicar
regra nova no meio de uma migração misturaria preservar com publicar.

O QUE MUDA NOS DADOS, VINDO DA v5 (P9)
======================================
``anotacoes.versao_brief`` nasce **NULL** em todo o trabalho anterior, e isso é o
certo: não havia brief publicado quando aquelas anotações foram feitas, e
carimbar 1 diria que elas seguiram um enquadramento que ainda não existia. É o
oposto do ``versao_diretriz`` da v1, onde carimbar 1 **era** a verdade porque o
texto literal do HTML era, palavra por palavra, a v1 do arquivo.

A tabela ``briefs`` nasce vazia. É a primeira migração deste módulo em que a
lista de cópia de ``anotacoes`` diverge entre duas versões de origem — a coluna
nova simplesmente não é listada na ``COPIA_V5_ANTES``, e é isso que faz a cópia
explícita coluna a coluna valer o incômodo dela.

E ``turnos_conversa`` entra pela primeira vez numa lista de cópia: ela existe
desde a v5, então uma migração v5→v6 que a esquecesse apagaria conversas inteiras
— o único trabalho humano deste schema produzido ANTES de existir uma linha em
``anotacoes``.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from .. import db as dbmod
from . import db as adb
from . import diretrizes as dirmod
from . import eventos as evmod
from . import projetos as projmod

#: ``status`` de ``anotacoes`` da v1 → v2. Sem entrada = migração recusada.
MAPA_STATUS: dict[str, str] = {
    "pendente_revisao": "pendente_triagem",
    # Aprovada na v1 significava "a revisão passou". Na v2 a revisão tem duas
    # passagens, e o que existia era só a primeira: o destino honesto é a fila da
    # segunda, não `avaliada` (que afirmaria uma avaliação que ninguém fez).
    "aprovada": "pendente_avaliacao",
    "rejeitada": "devolvida",
}

#: ``veredito`` de ``revisoes`` da v1 → v2.
MAPA_VEREDITO: dict[str, str] = {"aprovada": "aprovada", "rejeitada": "devolvida"}

#: A versão de diretriz carimbada nas anotações que já existiam. Ver o cabeçalho.
VERSAO_DIRETRIZ_HERDADA = 1

#: As tabelas copiadas como estão, ANTES de ``anotacoes``. A lista de colunas é
#: explícita de propósito — ver o item 3 do cabeçalho.
COPIA_ANTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("prompts_demo", ("uid", "text", "lang", "criado_em")),
    (
        "atribuicoes",
        ("id", "tarefa_id", "anotador_id", "status", "iniciada_em", "expira_em", "terminada_em"),
    ),
    (
        "respostas_modelo",
        ("id", "prompt_uid", "rotulo_modelo", "texto", "origem", "meta_json", "criada_em"),
    ),
    (
        "criacoes",
        ("id", "autor_id", "texto", "lang", "task_type_sugerido", "domain_sugerido", "brief",
         "hash_norm", "duplicata_corpus", "status", "revisor_id", "comentario_revisao",
         "revisada_em", "uid_previsto", "exportada_em", "criada_em"),
    ),
)

#: E as que precisam vir DEPOIS. ``rubricas.anotacao_id`` aponta para
#: ``anotacoes``: com ``foreign_keys=ON`` (que ``db.connect`` liga sempre),
#: copiar uma rubrica materializada de uma anotação antes da anotação falharia.
#: Hoje todas as rubricas são fixture com ``anotacao_id`` nulo e a ordem não
#: apareceria — a partir deste marco a triagem materializa rubricas de verdade,
#: e aí apareceria.
COPIA_DEPOIS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "rubricas",
        ("id", "prompt_uid", "titulo", "criterios_json", "origem", "anotacao_id", "status",
         "criada_em"),
    ),
)

#: A união, para quem quiser inventariar o que a migração cobre.
COPIA_DIRETA: tuple[tuple[str, tuple[str, ...]], ...] = COPIA_ANTES + COPIA_DEPOIS

#: A cópia vinda da **v2**: tudo que a v2 já tinha, na ordem em que as FKs
#: fecham. ``anotacoes`` é tratada à parte (ela ganha a coluna nova) e por isso
#: parte a lista em duas, como na v1.
#:
#: ``pool`` entra na cópia — e não é cache indevido: ela é uma projeção com
#: chave de invalidação explícita (``pool_assinatura`` em ``app_meta``, copiada
#: junto). Deixá-la para trás faria a primeira subida depois da migração
#: reconstruir 500 linhas por nada, e faria as contagens de conferência não
#: baterem sem um caso especial.
COPIA_V2_ANTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("projetos", ("id", "nome", "cliente", "descricao", "status", "criado_em")),
    ("diretrizes", ("id", "tipo", "versao", "texto_json", "criada_em")),
    (
        "anotadores",
        ("id", "nome", "papel", "ativo", "qualificacoes_json", "criado_em"),
    ),
    ("prompts_demo", ("uid", "text", "lang", "criado_em")),
    (
        "tarefas",
        ("id", "tipo", "prompt_uid", "origem", "projeto_id", "payload_json", "gabarito_json",
         "n_anotacoes_alvo", "prioridade", "status", "db_build_id", "criado_em"),
    ),
    (
        "atribuicoes",
        ("id", "tarefa_id", "anotador_id", "status", "iniciada_em", "expira_em", "terminada_em"),
    ),
    (
        "respostas_modelo",
        ("id", "prompt_uid", "rotulo_modelo", "texto", "origem", "meta_json", "criada_em"),
    ),
    (
        "criacoes",
        ("id", "autor_id", "texto", "lang", "task_type_sugerido", "domain_sugerido", "brief",
         "hash_norm", "duplicata_corpus", "status", "revisor_id", "comentario_revisao",
         "revisada_em", "uid_previsto", "exportada_em", "criada_em"),
    ),
    ("pool", ("uid", "ordem")),
)

#: E o que depende de ``anotacoes`` já existir. A ordem importa por causa do
#: ``foreign_keys=ON`` que ``db.connect`` liga sempre: ``edicoes_avaliacao``
#: referencia ``avaliacoes``, que referencia ``anotacoes``.
COPIA_V2_DEPOIS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "revisoes",
        ("id", "anotacao_id", "revisor_id", "veredito", "comentario", "autorrevisao",
         "criada_em"),
    ),
    (
        "avaliacoes",
        ("id", "anotacao_id", "revisor_id", "avaliacao_antes", "avaliacao_depois",
         "justificativa", "payload_corrigido_json", "autorrevisao", "tempo_ativo_ms",
         "criada_em"),
    ),
    (
        "edicoes_avaliacao",
        ("id", "avaliacao_id", "campo", "valor_antes", "valor_depois", "motivo"),
    ),
    (
        "decisoes_admin",
        ("id", "avaliacao_id", "admin_id", "decisao", "comentario", "criada_em"),
    ),
    (
        "rubricas",
        ("id", "prompt_uid", "titulo", "criterios_json", "origem", "anotacao_id", "status",
         "criada_em"),
    ),
    ("eventos", ("id", "ator_id", "acao", "entidade", "entidade_id", "detalhe_json",
                 "criado_em")),
)

#: A cópia vinda da **v3**. Diferente das duas anteriores, aqui NENHUMA tabela
#: muda de forma: a v4 só alarga um CHECK. Por isso ``anotacoes`` deixa de ser
#: caso especial e entra na lista, agora com ``gabarito_avaliacao_json`` — que
#: na v3 já existe e pode estar PREENCHIDA (uma campanha do P4c rodada antes
#: desta migração), e deixá-la de fora apagaria em silêncio exatamente o alvo
#: escondido que dá valor às sintéticas.
#:
#: Escrita por extenso, e não fatiada de ``COPIA_V2_ANTES``: a regra 3 do
#: cabeçalho vale aqui também. Uma lista derivada por índice quebraria calada no
#: dia em que alguém inserisse uma tabela no meio da outra.
COPIA_V3_ANTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("projetos", ("id", "nome", "cliente", "descricao", "status", "criado_em")),
    ("diretrizes", ("id", "tipo", "versao", "texto_json", "criada_em")),
    ("anotadores", ("id", "nome", "papel", "ativo", "qualificacoes_json", "criado_em")),
    ("prompts_demo", ("uid", "text", "lang", "criado_em")),
    (
        "tarefas",
        ("id", "tipo", "prompt_uid", "origem", "projeto_id", "payload_json", "gabarito_json",
         "n_anotacoes_alvo", "prioridade", "status", "db_build_id", "criado_em"),
    ),
    (
        "atribuicoes",
        ("id", "tarefa_id", "anotador_id", "status", "iniciada_em", "expira_em", "terminada_em"),
    ),
    (
        "anotacoes",
        ("id", "atribuicao_id", "versao", "payload_schema", "versao_diretriz", "payload_json",
         "gabarito_avaliacao_json", "tempo_ativo_ms", "iniciada_em", "submetida_em", "status"),
    ),
    (
        "respostas_modelo",
        ("id", "prompt_uid", "rotulo_modelo", "texto", "origem", "meta_json", "criada_em"),
    ),
    (
        "criacoes",
        ("id", "autor_id", "texto", "lang", "task_type_sugerido", "domain_sugerido", "brief",
         "hash_norm", "duplicata_corpus", "status", "revisor_id", "comentario_revisao",
         "revisada_em", "uid_previsto", "exportada_em", "criada_em"),
    ),
    ("pool", ("uid", "ordem")),
)

#: E o resto, que depende de ``anotacoes`` já existir — as mesmas da v2, porque
#: nenhuma delas mudou de forma.
COPIA_V3_DEPOIS: tuple[tuple[str, tuple[str, ...]], ...] = COPIA_V2_DEPOIS

#: A cópia vinda da **v4**. Como na v3, nenhuma tabela existente muda de forma:
#: a v5 alarga dois CHECKs (``tarefas.tipo`` e ``diretrizes.tipo``, que ganham
#: ``conversa_modelo`` e ``duelo_modelos``) e cria ``turnos_conversa`` — que
#: **não** entra em lista de cópia nenhuma, porque nasce vazia num banco que
#: nunca teve conversa.
#:
#: Escrita por extenso e não reaproveitada de ``COPIA_V3_ANTES`` pela regra 3 do
#: cabeçalho, e desta vez com um motivo concreto: as duas listas são idênticas
#: HOJE e vão divergir na primeira coluna que a v6 acrescentar. Um alias faria a
#: v4 passar a copiar uma coluna que ela não tem.
COPIA_V4_ANTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("projetos", ("id", "nome", "cliente", "descricao", "status", "criado_em")),
    ("diretrizes", ("id", "tipo", "versao", "texto_json", "criada_em")),
    ("anotadores", ("id", "nome", "papel", "ativo", "qualificacoes_json", "criado_em")),
    ("prompts_demo", ("uid", "text", "lang", "criado_em")),
    (
        "tarefas",
        ("id", "tipo", "prompt_uid", "origem", "projeto_id", "payload_json", "gabarito_json",
         "n_anotacoes_alvo", "prioridade", "status", "db_build_id", "criado_em"),
    ),
    (
        "atribuicoes",
        ("id", "tarefa_id", "anotador_id", "status", "iniciada_em", "expira_em", "terminada_em"),
    ),
    (
        "anotacoes",
        ("id", "atribuicao_id", "versao", "payload_schema", "versao_diretriz", "payload_json",
         "gabarito_avaliacao_json", "tempo_ativo_ms", "iniciada_em", "submetida_em", "status"),
    ),
    (
        "respostas_modelo",
        ("id", "prompt_uid", "rotulo_modelo", "texto", "origem", "meta_json", "criada_em"),
    ),
    (
        "criacoes",
        ("id", "autor_id", "texto", "lang", "task_type_sugerido", "domain_sugerido", "brief",
         "hash_norm", "duplicata_corpus", "status", "revisor_id", "comentario_revisao",
         "revisada_em", "uid_previsto", "exportada_em", "criada_em"),
    ),
    ("pool", ("uid", "ordem")),
)

#: E o que depende de ``anotacoes`` já existir. Iguais às da v2/v3 — nenhuma
#: dessas tabelas mudou de forma desde então.
COPIA_V4_DEPOIS: tuple[tuple[str, tuple[str, ...]], ...] = COPIA_V2_DEPOIS

#: A cópia vinda da **v5**. Aqui as listas finalmente DIVERGEM, e é o caso que o
#: comentário do ``COPIA_V4_ANTES`` previu: ``anotacoes`` ganhou
#: ``versao_brief``, que um banco v5 não tem — então ela **não** aparece nesta
#: lista, e a coluna nasce NULL no trabalho anterior ao brief. NULL é a verdade
#: ali: não havia brief publicado quando aquela anotação foi feita.
#:
#: ``turnos_conversa`` entra pela primeira vez em lista de cópia: ela existe
#: desde a v5, então migrar de v5 sem copiá-la apagaria conversas inteiras — o
#: único trabalho humano deste schema que é produzido ANTES de existir uma linha
#: em ``anotacoes``.
COPIA_V5_ANTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("projetos", ("id", "nome", "cliente", "descricao", "status", "criado_em")),
    ("diretrizes", ("id", "tipo", "versao", "texto_json", "criada_em")),
    ("anotadores", ("id", "nome", "papel", "ativo", "qualificacoes_json", "criado_em")),
    ("prompts_demo", ("uid", "text", "lang", "criado_em")),
    (
        "tarefas",
        ("id", "tipo", "prompt_uid", "origem", "projeto_id", "payload_json", "gabarito_json",
         "n_anotacoes_alvo", "prioridade", "status", "db_build_id", "criado_em"),
    ),
    (
        "atribuicoes",
        ("id", "tarefa_id", "anotador_id", "status", "iniciada_em", "expira_em", "terminada_em"),
    ),
    (
        "anotacoes",
        ("id", "atribuicao_id", "versao", "payload_schema", "versao_diretriz", "payload_json",
         "gabarito_avaliacao_json", "tempo_ativo_ms", "iniciada_em", "submetida_em", "status"),
    ),
    (
        "turnos_conversa",
        ("id", "atribuicao_id", "ordem", "papel", "rotulo", "texto", "raciocinio", "truncado",
         "modelo", "digest", "escolhida", "justificativa", "duracao_ms", "criado_em"),
    ),
    (
        "respostas_modelo",
        ("id", "prompt_uid", "rotulo_modelo", "texto", "origem", "meta_json", "criada_em"),
    ),
    (
        "criacoes",
        ("id", "autor_id", "texto", "lang", "task_type_sugerido", "domain_sugerido", "brief",
         "hash_norm", "duplicata_corpus", "status", "revisor_id", "comentario_revisao",
         "revisada_em", "uid_previsto", "exportada_em", "criada_em"),
    ),
    ("pool", ("uid", "ordem")),
)

#: O que depende de ``anotacoes``. Ver a nota de ``COPIA_V2_DEPOIS``.
COPIA_V5_DEPOIS: tuple[tuple[str, tuple[str, ...]], ...] = COPIA_V2_DEPOIS

#: As versões de origem que este módulo sabe ler. Uma lista, e não um ``if``
#: solto: acrescentar uma versão é acrescentar uma função e uma chave.
ORIGENS_CONHECIDAS: tuple[int, ...] = (1, 2, 3, 4, 5)

#: As tabelas que a própria migração faz crescer, e que por isso são conferidas
#: por "nunca menos" em vez de "exatamente igual": ``app_meta`` ganha chaves e
#: ``eventos`` ganha o registro ``banco_migrado``. Ver a conferência em
#: ``migrar``.
TABELAS_QUE_CRESCEM: frozenset[str] = frozenset({"app_meta", "eventos"})

#: ``versão de origem -> as tabelas que AINDA NÃO EXISTIAM nela``, e que por isso
#: não aparecem em lista de cópia nenhuma: elas nascem vazias do ``db.DDL``.
#:
#: Isto é DADO, e não uma exceção escrita à mão dentro de cada teste, pelo mesmo
#: motivo de ``TRANSICOES``: o teste "a cópia cobre todas as tabelas" existe para
#: pegar uma tabela que alguém esqueceu de copiar, e a única forma de ele
#: continuar valendo depois de uma tabela nova é descontar aqui — no código, uma
#: vez — em vez de relaxar o teste.
SEM_ORIGEM: dict[int, frozenset[str]] = {
    # `turnos_conversa` nasce na v5 (P4d): um banco v1..v4 nunca teve conversa.
    # `briefs` nasce na v6 (P9): nenhum banco anterior teve brief de projeto.
    1: frozenset({"turnos_conversa", "briefs"}),
    2: frozenset({"turnos_conversa", "briefs"}),
    3: frozenset({"turnos_conversa", "briefs"}),
    4: frozenset({"turnos_conversa", "briefs"}),
    5: frozenset({"briefs"}),
}


class MigracaoImpossivel(RuntimeError):
    """Algo no banco antigo não cabe no schema novo. Nada foi alterado."""


def _copiar(origem: sqlite3.Connection, destino: sqlite3.Connection, tabela: str,
            colunas: tuple[str, ...]) -> int:
    lista = ", ".join(colunas)
    marcas = ", ".join("?" * len(colunas))
    linhas = origem.execute(f"SELECT {lista} FROM {tabela} ORDER BY rowid").fetchall()
    destino.executemany(
        f"INSERT INTO {tabela} ({lista}) VALUES ({marcas})",
        [tuple(linha[c] for c in colunas) for linha in linhas],
    )
    return len(linhas)


def _contagens_brutas(conn: sqlite3.Connection) -> dict[str, int]:
    """``count(*)`` das tabelas que EXISTEM neste banco (a v1 tem menos)."""
    nomes = {
        str(linha["name"])
        for linha in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    return {
        t: int(conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"])
        for t in sorted(nomes)
    }


def _da_v1(velho: sqlite3.Connection, novo: sqlite3.Connection) -> dict[str, Any]:
    """Copia a v1 no arquivo já inicializado no schema de HOJE.

    Devolve o relatório que a CLI imprime.
    """
    relatorio: dict[str, Any] = {"copiadas": {}, "avisos": []}

    # 1. os DOIS projetos padrão: toda tarefa passa a ter escopo, e o escopo
    #    separa o pacote de demonstração do trabalho real. Ver `projetos.py`.
    ids = projmod.garantir_padroes(novo)
    relatorio["projetos"] = {
        projmod.nome_padrao(): ids["padrao"],
        projmod.nome_demonstracao(): ids["demonstracao"],
    }

    # 2. as diretrizes versionadas: o texto que estava literal no HTML é a v1.
    relatorio["diretrizes"] = dirmod.semear(novo)

    # 3. anotadores (+ qualificacoes_json vazio)
    linhas = velho.execute(
        "SELECT id, nome, papel, ativo, criado_em FROM anotadores ORDER BY id"
    ).fetchall()
    novo.executemany(
        "INSERT INTO anotadores (id, nome, papel, ativo, qualificacoes_json, criado_em) "
        "VALUES (?, ?, ?, ?, '{}', ?)",
        [(x["id"], x["nome"], x["papel"], x["ativo"], x["criado_em"]) for x in linhas],
    )
    relatorio["copiadas"]["anotadores"] = len(linhas)

    # 4. tarefas (+ projeto_id). A ALOCAÇÃO é pelo prefixo do uid, que é o
    #    contrato do resolvedor: `demo:` é fixture (projeto Demonstração), o
    #    resto é prompt real do corpus (projeto Portfólio). Nenhum chute — é a
    #    mesma regra que decide em qual banco buscar o texto.
    linhas = velho.execute(
        "SELECT id, tipo, prompt_uid, origem, payload_json, gabarito_json, "
        "       n_anotacoes_alvo, prioridade, status, db_build_id, criado_em "
        "FROM tarefas ORDER BY id"
    ).fetchall()
    novo.executemany(
        "INSERT INTO tarefas (id, tipo, prompt_uid, origem, projeto_id, payload_json, "
        "                     gabarito_json, n_anotacoes_alvo, prioridade, status, "
        "                     db_build_id, criado_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (x["id"], x["tipo"], x["prompt_uid"], x["origem"],
             ids["demonstracao"] if adb.e_demo(str(x["prompt_uid"])) else ids["padrao"],
             x["payload_json"], x["gabarito_json"], x["n_anotacoes_alvo"], x["prioridade"],
             x["status"], x["db_build_id"], x["criado_em"])
            for x in linhas
        ],
    )
    relatorio["copiadas"]["tarefas"] = len(linhas)
    relatorio["alocacao"] = {
        projmod.nome_demonstracao(): sum(
            1 for x in linhas if adb.e_demo(str(x["prompt_uid"]))
        ),
        projmod.nome_padrao(): sum(
            1 for x in linhas if not adb.e_demo(str(x["prompt_uid"]))
        ),
    }

    # 5. as tabelas sem mudança de forma que não dependem de `anotacoes`
    for tabela, colunas in COPIA_ANTES:
        relatorio["copiadas"][tabela] = _copiar(velho, novo, tabela, colunas)

    # 6. anotacoes: o backfill do status + a versão da diretriz
    linhas = velho.execute(
        "SELECT id, atribuicao_id, versao, payload_schema, payload_json, tempo_ativo_ms, "
        "       iniciada_em, submetida_em, status FROM anotacoes ORDER BY id"
    ).fetchall()
    desconhecidos = sorted(
        {str(x["status"]) for x in linhas} - set(MAPA_STATUS)
    )
    if desconhecidos:
        raise MigracaoImpossivel(
            f"anotações com status que o schema 2 não conhece: {', '.join(desconhecidos)}. "
            "Nada foi alterado — o arquivo original está intacto."
        )
    novo.executemany(
        "INSERT INTO anotacoes (id, atribuicao_id, versao, payload_schema, versao_diretriz, "
        "                       payload_json, tempo_ativo_ms, iniciada_em, submetida_em, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (x["id"], x["atribuicao_id"], x["versao"], x["payload_schema"],
             VERSAO_DIRETRIZ_HERDADA, x["payload_json"], x["tempo_ativo_ms"],
             x["iniciada_em"], x["submetida_em"], MAPA_STATUS[str(x["status"])])
            for x in linhas
        ],
    )
    relatorio["copiadas"]["anotacoes"] = len(linhas)
    relatorio["status_backfill"] = {
        antes: sum(1 for x in linhas if str(x["status"]) == antes)
        for antes in sorted({str(x["status"]) for x in linhas})
    }

    # 7. revisoes: o veredito muda de nome, e a autorrevisão passa a ser gravada.
    #    Nas linhas antigas ela é DERIVADA uma única vez (o revisor era o autor?)
    #    — depois disso a coluna manda, porque `atribuicoes.anotador_id` pode ser
    #    corrigido e a derivação passaria a mentir sobre uma revisão já feita.
    linhas = velho.execute(
        "SELECT r.id, r.anotacao_id, r.revisor_id, r.veredito, r.comentario, r.criada_em, "
        "       a.anotador_id "
        "FROM revisoes r "
        "JOIN anotacoes an ON an.id = r.anotacao_id "
        "JOIN atribuicoes a ON a.id = an.atribuicao_id ORDER BY r.id"
    ).fetchall()
    desconhecidos = sorted({str(x["veredito"]) for x in linhas} - set(MAPA_VEREDITO))
    if desconhecidos:
        raise MigracaoImpossivel(
            f"revisões com veredito desconhecido: {', '.join(desconhecidos)}. "
            "Nada foi alterado."
        )
    novo.executemany(
        "INSERT INTO revisoes (id, anotacao_id, revisor_id, veredito, comentario, "
        "                      autorrevisao, criada_em) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (x["id"], x["anotacao_id"], x["revisor_id"], MAPA_VEREDITO[str(x["veredito"])],
             x["comentario"], int(int(x["revisor_id"]) == int(x["anotador_id"])),
             x["criada_em"])
            for x in linhas
        ],
    )
    relatorio["copiadas"]["revisoes"] = len(linhas)

    # 8. o que depende de `anotacoes` já existir
    for tabela, colunas in COPIA_DEPOIS:
        relatorio["copiadas"][tabela] = _copiar(velho, novo, tabela, colunas)

    # 9. app_meta, menos a versão (que o init_db já escreveu) e menos as chaves
    #    do pool materializado, que não existiam e serão recalculadas na subida.
    for linha in velho.execute("SELECT key, value FROM app_meta ORDER BY key"):
        chave = str(linha["key"])
        if chave == adb.CHAVE_VERSAO or chave.startswith("pool_"):
            continue
        adb.set_meta(novo, chave, linha["value"])

    evmod.registrar(
        novo,
        acao="banco_migrado",
        entidade="banco",
        de=1,
        para=adb.SCHEMA_VERSION_ANOTACAO,
        anotacoes_preservadas=relatorio["copiadas"]["anotacoes"],
        projetos=relatorio["projetos"],
        alocacao=relatorio["alocacao"],
    )
    return relatorio


def _da_v2(velho: sqlite3.Connection, novo: sqlite3.Connection) -> dict[str, Any]:
    """Copia a v2 no arquivo já inicializado no schema de HOJE.

    A v2 já tem projetos, diretrizes e as três tabelas da passagem 2. Nada é
    transformado: a v3 acrescenta UMA coluna anulável, e o valor certo dela em
    todo trabalho humano é NULL.

    Projetos e diretrizes são **copiados**, não re-semeados. Re-semear
    reescreveria (ou recusaria reescrever) uma diretriz que já gravou trabalho,
    e ainda por cima poderia trocar os ids que ``tarefas.projeto_id`` referencia.
    """
    relatorio: dict[str, Any] = {"copiadas": {}, "avisos": []}

    for tabela, colunas in COPIA_V2_ANTES:
        relatorio["copiadas"][tabela] = _copiar(velho, novo, tabela, colunas)

    # `anotacoes` à parte: é a única tabela com forma diferente entre as duas
    # versões. A coluna nova é omitida do INSERT de propósito — ela nasce NULL,
    # que é o valor que significa "trabalho humano".
    linhas = velho.execute(
        "SELECT id, atribuicao_id, versao, payload_schema, versao_diretriz, payload_json, "
        "       tempo_ativo_ms, iniciada_em, submetida_em, status FROM anotacoes ORDER BY id"
    ).fetchall()
    desconhecidos = sorted({str(x["status"]) for x in linhas} - set(adb.STATUS_ANOTACAO))
    if desconhecidos:
        raise MigracaoImpossivel(
            f"anotações com status que o schema {adb.SCHEMA_VERSION_ANOTACAO} não conhece: "
            f"{', '.join(desconhecidos)}. Nada foi alterado."
        )
    novo.executemany(
        "INSERT INTO anotacoes (id, atribuicao_id, versao, payload_schema, versao_diretriz, "
        "                       payload_json, tempo_ativo_ms, iniciada_em, submetida_em, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (x["id"], x["atribuicao_id"], x["versao"], x["payload_schema"], x["versao_diretriz"],
             x["payload_json"], x["tempo_ativo_ms"], x["iniciada_em"], x["submetida_em"],
             x["status"])
            for x in linhas
        ],
    )
    relatorio["copiadas"]["anotacoes"] = len(linhas)

    for tabela, colunas in COPIA_V2_DEPOIS:
        relatorio["copiadas"][tabela] = _copiar(velho, novo, tabela, colunas)

    # app_meta inteira, MENOS a versão (que o `init_db` já escreveu). As chaves
    # do pool vêm junto porque a tabela `pool` veio junto: a assinatura continua
    # descrevendo exatamente o que está materializado.
    for linha in velho.execute("SELECT key, value FROM app_meta ORDER BY key"):
        if str(linha["key"]) == adb.CHAVE_VERSAO:
            continue
        adb.set_meta(novo, str(linha["key"]), linha["value"])

    # O relatório tem a MESMA forma da v1: a CLI imprime um só bloco.
    projetos = novo.execute("SELECT id, nome FROM projetos ORDER BY id").fetchall()
    relatorio["projetos"] = {str(p["nome"]): int(p["id"]) for p in projetos}
    relatorio["alocacao"] = {
        str(p["nome"]): int(
            novo.execute(
                "SELECT count(*) AS n FROM tarefas WHERE projeto_id = ?", (int(p["id"]),)
            ).fetchone()["n"]
        )
        for p in projetos
    }
    relatorio["diretrizes"] = relatorio["copiadas"].get("diretrizes", 0)
    # Sem backfill: nenhum status mudou de nome entre a v2 e a v3.
    relatorio["status_backfill"] = {}

    evmod.registrar(
        novo,
        acao="banco_migrado",
        entidade="banco",
        de=2,
        para=adb.SCHEMA_VERSION_ANOTACAO,
        anotacoes_preservadas=relatorio["copiadas"]["anotacoes"],
        avaliacoes_preservadas=relatorio["copiadas"]["avaliacoes"],
        projetos=relatorio["projetos"],
        alocacao=relatorio["alocacao"],
    )
    return relatorio


def _da_v3(velho: sqlite3.Connection, novo: sqlite3.Connection) -> dict[str, Any]:
    """Copia a v3 no arquivo já inicializado no schema de HOJE.

    A migração mais barata que este módulo tem, e a mais fácil de dispensar por
    engano: **nenhuma tabela muda de forma**. A v4 só acrescenta ``'importada'``
    ao CHECK de ``rubricas.origem``, e o único jeito de trocar um CHECK no
    SQLite é reconstruir a tabela — que é exatamente o que este módulo faz de
    graça, para todas elas, desde a v1.

    ``gabarito_avaliacao_json`` vem junto e é a única coluna desta cópia que
    pode estar preenchida por algo que não é trabalho humano: uma campanha do
    P4c que tenha rodado ANTES desta migração. Perder a coluna aqui apagaria o
    alvo escondido de cada sintética e deixaria um banco em que tudo parece
    humano — a mentira exata que a nulidade dela existe para impedir.
    """
    relatorio: dict[str, Any] = {"copiadas": {}, "avisos": []}

    for tabela, colunas in COPIA_V3_ANTES:
        relatorio["copiadas"][tabela] = _copiar(velho, novo, tabela, colunas)
    for tabela, colunas in COPIA_V3_DEPOIS:
        relatorio["copiadas"][tabela] = _copiar(velho, novo, tabela, colunas)

    # app_meta inteira, MENOS a versão (que o `init_db` já escreveu).
    for linha in velho.execute("SELECT key, value FROM app_meta ORDER BY key"):
        if str(linha["key"]) == adb.CHAVE_VERSAO:
            continue
        adb.set_meta(novo, str(linha["key"]), linha["value"])

    projetos = novo.execute("SELECT id, nome FROM projetos ORDER BY id").fetchall()
    relatorio["projetos"] = {str(p["nome"]): int(p["id"]) for p in projetos}
    relatorio["alocacao"] = {
        str(p["nome"]): int(
            novo.execute(
                "SELECT count(*) AS n FROM tarefas WHERE projeto_id = ?", (int(p["id"]),)
            ).fetchone()["n"]
        )
        for p in projetos
    }
    relatorio["diretrizes"] = relatorio["copiadas"].get("diretrizes", 0)
    # Sem backfill: nenhum status, veredito ou origem mudou de NOME entre a v3 e
    # a v4 — a v4 só passou a aceitar um nome a mais.
    relatorio["status_backfill"] = {}

    evmod.registrar(
        novo,
        acao="banco_migrado",
        entidade="banco",
        de=3,
        para=adb.SCHEMA_VERSION_ANOTACAO,
        anotacoes_preservadas=relatorio["copiadas"]["anotacoes"],
        sinteticas_preservadas=int(
            novo.execute(
                "SELECT count(*) AS n FROM anotacoes "
                f"WHERE {adb.COLUNA_GABARITO_AVALIACAO} IS NOT NULL"
            ).fetchone()["n"]
        ),
        avaliacoes_preservadas=relatorio["copiadas"]["avaliacoes"],
        projetos=relatorio["projetos"],
        alocacao=relatorio["alocacao"],
    )
    return relatorio


def _da_v4(velho: sqlite3.Connection, novo: sqlite3.Connection) -> dict[str, Any]:
    """Copia a v4 no arquivo já inicializado no schema de HOJE.

    A v5 acrescenta dois VALORES ao CHECK de ``tarefas.tipo`` e de
    ``diretrizes.tipo`` (``conversa_modelo`` e ``duelo_modelos``) e cria
    ``turnos_conversa``. É o mesmo caso da v3→v4, e a mesma armadilha: sem
    migrar, o banco do dono continuaria recusando o INSERT das tarefas novas com
    um CHECK que nenhum arquivo do repositório mostra mais — e ``CREATE TABLE IF
    NOT EXISTS`` não reescreve o CHECK de uma tabela que já existe.

    ``turnos_conversa`` não é copiada: ela não existe na v4, e nasce vazia. A
    conferência de contagens do ``migrar`` a ignora sozinha, porque ela compara o
    que existia ANTES.

    As diretrizes dos dois tipos novos **não** são semeadas aqui: quem semeia é o
    ``pf annotate seed``, e semear no meio de uma migração misturaria "preservar
    o que existe" com "publicar regra nova". A CLI diz isso na saída.
    """
    relatorio: dict[str, Any] = {"copiadas": {}, "avisos": []}

    for tabela, colunas in COPIA_V4_ANTES:
        relatorio["copiadas"][tabela] = _copiar(velho, novo, tabela, colunas)
    for tabela, colunas in COPIA_V4_DEPOIS:
        relatorio["copiadas"][tabela] = _copiar(velho, novo, tabela, colunas)

    for linha in velho.execute("SELECT key, value FROM app_meta ORDER BY key"):
        if str(linha["key"]) == adb.CHAVE_VERSAO:
            continue
        adb.set_meta(novo, str(linha["key"]), linha["value"])

    projetos = novo.execute("SELECT id, nome FROM projetos ORDER BY id").fetchall()
    relatorio["projetos"] = {str(p["nome"]): int(p["id"]) for p in projetos}
    relatorio["alocacao"] = {
        str(p["nome"]): int(
            novo.execute(
                "SELECT count(*) AS n FROM tarefas WHERE projeto_id = ?", (int(p["id"]),)
            ).fetchone()["n"]
        )
        for p in projetos
    }
    relatorio["diretrizes"] = relatorio["copiadas"].get("diretrizes", 0)
    relatorio["status_backfill"] = {}
    relatorio["avisos"].append(
        "os dois tipos de conversa (conversa_modelo, duelo_modelos) ainda não têm "
        "diretriz publicada neste banco — rode `pf annotate seed` para publicá-las"
    )

    evmod.registrar(
        novo,
        acao="banco_migrado",
        entidade="banco",
        de=4,
        para=adb.SCHEMA_VERSION_ANOTACAO,
        anotacoes_preservadas=relatorio["copiadas"]["anotacoes"],
        avaliacoes_preservadas=relatorio["copiadas"]["avaliacoes"],
        tipos_novos=list(adb.TIPOS_CONVERSA),
        projetos=relatorio["projetos"],
        alocacao=relatorio["alocacao"],
    )
    return relatorio


def _da_v5(velho: sqlite3.Connection, novo: sqlite3.Connection) -> dict[str, Any]:
    """Copia a v5 no arquivo já inicializado no schema de HOJE.

    A v6 (P9) acrescenta a tabela ``briefs`` — que nasceria de graça, porque
    ``CREATE TABLE IF NOT EXISTS`` a cria num banco existente — e a coluna
    ``anotacoes.versao_brief``, que é o que de fato cobra a migração. É o mesmo
    lugar em que a v4 doeu: o que ``IF NOT EXISTS`` não conserta.

    ``versao_brief`` fica NULL em todo trabalho anterior, e isso não é perda: não
    havia brief publicado quando aquelas anotações foram feitas, e escrever 1 ali
    diria que elas seguiram um enquadramento que ainda não existia. É a mesma
    honestidade do ``versao_diretriz`` num banco sem diretrizes semeadas.

    O brief em si **não** é semeado aqui, pela mesma razão que as diretrizes do
    P4d não foram: semear no meio de uma migração misturaria "preservar o que
    existe" com "publicar instrução nova". A CLI avisa na saída.
    """
    relatorio: dict[str, Any] = {"copiadas": {}, "avisos": []}

    for tabela, colunas in COPIA_V5_ANTES:
        relatorio["copiadas"][tabela] = _copiar(velho, novo, tabela, colunas)
    for tabela, colunas in COPIA_V5_DEPOIS:
        relatorio["copiadas"][tabela] = _copiar(velho, novo, tabela, colunas)

    for linha in velho.execute("SELECT key, value FROM app_meta ORDER BY key"):
        if str(linha["key"]) == adb.CHAVE_VERSAO:
            continue
        adb.set_meta(novo, str(linha["key"]), linha["value"])

    projetos = novo.execute("SELECT id, nome FROM projetos ORDER BY id").fetchall()
    relatorio["projetos"] = {str(p["nome"]): int(p["id"]) for p in projetos}
    relatorio["alocacao"] = {
        str(p["nome"]): int(
            novo.execute(
                "SELECT count(*) AS n FROM tarefas WHERE projeto_id = ?", (int(p["id"]),)
            ).fetchone()["n"]
        )
        for p in projetos
    }
    relatorio["diretrizes"] = relatorio["copiadas"].get("diretrizes", 0)
    relatorio["status_backfill"] = {}
    relatorio["avisos"].append(
        "nenhum projeto tem brief publicado neste banco, e as anotações antigas "
        "ficaram com versao_brief = NULL (não havia brief quando foram feitas) — "
        "rode `pf annotate seed` para publicar a v1"
    )

    evmod.registrar(
        novo,
        acao="banco_migrado",
        entidade="banco",
        de=5,
        para=adb.SCHEMA_VERSION_ANOTACAO,
        anotacoes_preservadas=relatorio["copiadas"]["anotacoes"],
        turnos_preservados=relatorio["copiadas"].get("turnos_conversa", 0),
        avaliacoes_preservadas=relatorio["copiadas"]["avaliacoes"],
        projetos=relatorio["projetos"],
        alocacao=relatorio["alocacao"],
    )
    return relatorio


#: versão de origem → a função que sabe lê-la. Ver ``ORIGENS_CONHECIDAS``.
PASSOS = {1: _da_v1, 2: _da_v2, 3: _da_v3, 4: _da_v4, 5: _da_v5}


def precisa_migrar(caminho: Path) -> int | None:
    """A versão gravada, se ela diverge. ``None`` quando não há o que fazer."""
    if not Path(caminho).is_file():
        return None
    conn = dbmod.connect(caminho)
    try:
        versao = adb.versao_do_banco(conn)
    finally:
        conn.close()
    if versao is None or versao == adb.SCHEMA_VERSION_ANOTACAO:
        return None
    return versao


def migrar(caminho: str | Path) -> dict[str, Any]:
    """Migra o arquivo para a versão corrente. Idempotente.

    Devolve o relatório que a CLI imprime. Levanta ``MigracaoImpossivel`` quando
    algum dado do banco antigo não cabe no schema novo — e nesse caso **o
    arquivo original não foi tocado**.
    """
    alvo = Path(caminho)
    if not alvo.is_file():
        raise MigracaoImpossivel(
            f"{alvo} não existe. Um banco novo já nasce na versão "
            f"{adb.SCHEMA_VERSION_ANOTACAO} — rode `pf annotate seed`."
        )

    velho = dbmod.connect(alvo)
    try:
        versao = adb.versao_do_banco(velho)
        if versao == adb.SCHEMA_VERSION_ANOTACAO:
            return {"ja_estava": True, "versao": versao, "arquivo": str(alvo)}
        if versao is None:
            raise MigracaoImpossivel(
                f"{alvo} não tem {adb.CHAVE_VERSAO} em app_meta — não dá para saber "
                "de qual versão migrar. Mova o arquivo para o lado."
            )
        if versao not in PASSOS:
            raise MigracaoImpossivel(
                f"{alvo} está na versão {versao}; esta migração sabe ler "
                f"{', '.join(str(v) for v in ORIGENS_CONHECIDAS)} e escrever "
                f"{adb.SCHEMA_VERSION_ANOTACAO}."
            )
        antes = _contagens_brutas(velho)

        tmp = alvo.with_name(alvo.name + ".migrando.tmp")
        for sufixo in ("", "-wal", "-shm"):
            resto = tmp.with_name(tmp.name + sufixo)
            if resto.exists():
                resto.unlink()

        novo = dbmod.connect(tmp)
        try:
            adb.init_db(novo)
            novo.execute("BEGIN")
            try:
                relatorio = PASSOS[versao](velho, novo)
                # A prova, DENTRO da transação: as FKs fecham e as contagens das
                # tabelas que existiam batem uma a uma. Uma linha perdida na
                # cópia aborta a migração em vez de virar um banco menor.
                quebras = novo.execute("PRAGMA foreign_key_check").fetchall()
                if quebras:
                    raise MigracaoImpossivel(
                        f"a cópia deixou {len(quebras)} referência(s) órfã(s) — "
                        "nada foi alterado."
                    )
                depois = _contagens_brutas(novo)
                for tabela, n in antes.items():
                    if tabela in TABELAS_QUE_CRESCEM:
                        # Estas DUAS crescem de propósito durante a migração:
                        # `app_meta` ganha chaves novas e `eventos` ganha o
                        # registro `banco_migrado`. A conferência existe para
                        # detectar trabalho PERDIDO, e é isso que ela continua
                        # detectando aqui — só nunca menos.
                        if depois.get(tabela, -1) < n:
                            raise MigracaoImpossivel(
                                f"{tabela}: {n} linha(s) antes, {depois.get(tabela)} depois. "
                                "Nada foi alterado."
                            )
                        continue
                    if depois.get(tabela, -1) != n:
                        raise MigracaoImpossivel(
                            f"{tabela}: {n} linha(s) antes, {depois.get(tabela)} depois. "
                            "Nada foi alterado."
                        )
                novo.execute("COMMIT")
            except Exception:
                novo.execute("ROLLBACK")
                raise
            novo.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            novo.close()
    finally:
        velho.close()

    # A troca. O antigo vira `.v<N>.bak` — a migração se desfaz com um mv. O
    # número é o da versão de ORIGEM, e não um sufixo fixo: com duas migrações
    # possíveis, `.v1.bak` ao lado de um banco que veio da v2 mentiria sobre o
    # que aquele arquivo é.
    backup = alvo.with_name(f"{alvo.name}.v{versao}.bak")
    if backup.exists():
        backup.unlink()
    os.replace(alvo, backup)
    for sufixo in ("-wal", "-shm"):
        resto = alvo.with_name(alvo.name + sufixo)
        if resto.exists():
            try:
                resto.unlink()
            except OSError:  # pragma: no cover - handle preso por outro processo
                pass
    os.replace(tmp, alvo)

    relatorio.update(
        {
            "ja_estava": False,
            "de": versao,
            "para": adb.SCHEMA_VERSION_ANOTACAO,
            "arquivo": str(alvo),
            "backup": str(backup),
            "antes": antes,
        }
    )
    return relatorio


__all__ = [
    "COPIA_DIRETA",
    "COPIA_V2_ANTES",
    "COPIA_V2_DEPOIS",
    "COPIA_V3_ANTES",
    "COPIA_V3_DEPOIS",
    "COPIA_V4_ANTES",
    "COPIA_V4_DEPOIS",
    "MAPA_STATUS",
    "MAPA_VEREDITO",
    "ORIGENS_CONHECIDAS",
    "PASSOS",
    "SEM_ORIGEM",
    "TABELAS_QUE_CRESCEM",
    "VERSAO_DIRETRIZ_HERDADA",
    "MigracaoImpossivel",
    "migrar",
    "precisa_migrar",
]
