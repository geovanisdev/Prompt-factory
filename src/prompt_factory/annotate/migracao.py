"""``pf annotate migrate`` — schema 1 → 2 **preservando o trabalho humano**.

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

O arquivo antigo **não é apagado**: vira ``<nome>.v1.bak`` ao lado. Custo: um
arquivo de alguns MB. Benefício: a operação inteira é reversível com um ``mv``.

O QUE MUDA NOS DADOS
====================
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


def _migrar_para(velho: sqlite3.Connection, novo: sqlite3.Connection) -> dict[str, Any]:
    """Copia a v1 no arquivo já inicializado em v2. Devolve o relatório."""
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
        if versao != 1:
            raise MigracaoImpossivel(
                f"{alvo} está na versão {versao}; esta migração só sabe ir de 1 para "
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
                relatorio = _migrar_para(velho, novo)
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
                    if tabela == "app_meta":
                        continue  # a v2 grava chaves novas de propósito
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

    # A troca. O antigo vira `.v1.bak` — a migração inteira se desfaz com um mv.
    backup = alvo.with_name(alvo.name + ".v1.bak")
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
            "de": 1,
            "para": adb.SCHEMA_VERSION_ANOTACAO,
            "arquivo": str(alvo),
            "backup": str(backup),
            "antes": antes,
        }
    )
    return relatorio


__all__ = [
    "COPIA_DIRETA",
    "MAPA_STATUS",
    "MAPA_VEREDITO",
    "VERSAO_DIRETRIZ_HERDADA",
    "MigracaoImpossivel",
    "migrar",
    "precisa_migrar",
]
