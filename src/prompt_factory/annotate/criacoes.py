"""Modo criar — o anotador escreve o prompt, e ele entra no corpus de verdade.

É AQUI QUE OS DOIS OBJETIVOS DO PROJETO SE ENCONTRAM
====================================================
A Trilha A construiu um banco de **prompts escritos por pessoas reais**, com
licença e proveniência por linha, justamente porque "não posso usar prompts
criados por IA para treinar uma IA". A Trilha B construiu uma plataforma onde
pessoas trabalham. Este módulo é a junção: o texto escrito aqui atravessa
revisão, ingestão (``pf ingest plataforma``, P6) e a pipeline inteira, e
reaparece no corpus com uid determinístico.

Os outros dois modos de seleção CONSOMEM o corpus (a fila entrega, o catálogo
deixa escolher). Este o ALIMENTA — e é o que fecha o argumento do projeto na
tela, em vez de deixá-lo num README.

A CADEIA CANÔNICA É REUSADA, NUNCA REIMPLEMENTADA
=================================================
``norm_display`` → ``norm_for_hash`` → ``sha256`` → ``make_uid`` são as funções
do corpus, importadas. Reescrever qualquer uma delas aqui produziria um texto
que a pipeline normaliza de outro jeito, um ``hash_norm`` que não casa com o do
dedup e um uid que nunca aparece no banco — e os três falhariam **em silêncio**,
porque cada um deles produz um valor plausível. O sintoma seria o badge "no
corpus" que nunca acende, semanas depois, sem nada apontando para a causa.

O AVISO DE DUPLICATA É AVISO, E ISSO É DECISÃO
===============================================
Se o texto escrito colapsa com um prompt que já está no corpus, a plataforma
**avisa e deixa passar**. Bloquear seria tratar como erro o momento em que o
dedup exato prova que funciona: a mesma cadeia de normalização que junta 44 mil
duplicatas do WildChat acabou de reconhecer que duas pessoas, independentemente,
escreveram a mesma coisa. Isso é resultado, não falha — e a tela diz isso com
todas as letras.

O que o aviso impede é o outro erro: aprovar às cegas uma criação que não
acrescenta linha nenhuma ao corpus, e depois procurar por que a contagem não
subiu.

A LICENÇA É CC0, E ELA É DECLARADA NO ENVIO
============================================
Todo prompt criado aqui entra como **CC0 1.0** (domínio público). Não é detalhe
de configuração: é a única licença que pode ser afirmada por quem escreve o
texto na hora em que escreve, e o projeto inteiro se apoia em licença por linha.
A dedicação aparece no formulário — quem envia sabe o que está cedendo.

``exportada`` é carimbada pelo P6, DEPOIS de a linha entrar em ``data/raw/``.
Antes disso o funil para em ``aprovada``: dizer "exportada" antes de o arquivo
existir faria o painel do admin prometer uma ingestão que ninguém rodou.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from .. import schema, textnorm
from . import db as adb
from . import eventos as evmod

#: O nome da fonte no ``sources.toml`` (P6) e o primeiro ingrediente do uid.
#: Uma constante porque ela precisa ser **a mesma string** aqui e no ingester:
#: divergir daria um ``uid_previsto`` que a pipeline nunca produz, e o badge "no
#: corpus" nunca acenderia — sem erro nenhum em lugar nenhum.
FONTE = "plataforma"

#: A licença de tudo que sai daqui. Ver o cabeçalho.
LICENCA = "cc0-1.0"

#: Colunas que a API devolve. Uma constante porque são três consultas, e um
#: ``SELECT *`` traria ``hash_norm`` (ruído) sem trazer o nome do autor (útil).
COLUNAS = (
    "c.id, c.autor_id, c.texto, c.lang, c.task_type_sugerido, c.domain_sugerido, "
    "c.brief, c.hash_norm, c.duplicata_corpus, c.status, c.revisor_id, "
    "c.comentario_revisao, c.revisada_em, c.uid_previsto, c.exportada_em, c.criada_em"
)


def chave(texto: str) -> tuple[str, str]:
    """``(texto normalizado para exibição, hash_norm)`` — a cadeia do corpus.

    O ``hash_norm`` é ``sha256(norm_for_hash(norm_display(texto)))``, exatamente
    como o s01 o calcula. É esta igualdade que faz o aviso de duplicata comparar
    coisas comparáveis; qualquer atalho aqui produziria um hash que não casa com
    nada e um aviso que nunca dispara.
    """
    display = textnorm.norm_display(texto)
    bruto = textnorm.norm_for_hash(display)
    return display, hashlib.sha256(bruto.encode("utf-8")).hexdigest()


def uid_previsto(criacao_id: int, texto: str) -> str:
    """O uid que a pipeline vai produzir para esta criação.

    ``make_uid(FONTE, str(id), texto)`` — com ``source_id`` presente, o uid sai
    de ``"plataforma:<id>"`` e **não depende do texto**. É o comportamento
    correto e vale registrar por que: o id da linha é estável, então reingerir a
    mesma criação reproduz o mesmo uid, que é a propriedade que o dedup e o
    badge "no corpus" precisam. O texto entra na assinatura só quando a fonte
    não tem id próprio.
    """
    return schema.make_uid(FONTE, str(criacao_id), texto)


def duplicata_no_corpus(conn_corpus: sqlite3.Connection, hash_norm: str) -> str | None:
    """O uid do prompt do corpus que tem este ``hash_norm``, ou ``None``.

    Chave vazia NÃO casa, e a guarda é explícita: ``norm_for_hash("!!!") == ""``,
    então todo prompt só de pontuação compartilha o sha256 da string vazia. Sem
    esta linha, escrever "???" acusaria duplicata contra uma linha do corpus que
    não tem nada a ver — o mesmo caso que o s04 trata deixando essas linhas
    passarem inteiras.
    """
    if not hash_norm or hash_norm == hashlib.sha256(b"").hexdigest():
        return None
    linha = conn_corpus.execute(
        "SELECT uid FROM prompts WHERE hash_norm = ? LIMIT 1", (hash_norm,)
    ).fetchone()
    return None if linha is None else str(linha["uid"])


def _linha(linha: sqlite3.Row, autor: str | None = None, revisor: str | None = None) -> dict[str, Any]:
    return {
        "id": int(linha["id"]),
        "autor_id": int(linha["autor_id"]),
        "autor": autor,
        "texto": str(linha["texto"]),
        "lang": str(linha["lang"]),
        "task_type_sugerido": linha["task_type_sugerido"],
        "domain_sugerido": linha["domain_sugerido"],
        "brief": linha["brief"],
        # `hash_norm` sai porque ele é a PROVA do aviso de duplicata: sem ele na
        # tela, "isto já existe no corpus" é uma afirmação que ninguém confere.
        "hash_norm": str(linha["hash_norm"]),
        "duplicata_corpus": bool(linha["duplicata_corpus"]),
        "status": str(linha["status"]),
        "revisor_id": linha["revisor_id"],
        "revisor": revisor,
        "comentario_revisao": linha["comentario_revisao"],
        "revisada_em": linha["revisada_em"],
        "uid_previsto": linha["uid_previsto"],
        "exportada_em": linha["exportada_em"],
        "criada_em": linha["criada_em"],
        "licenca": LICENCA,
    }


def criar(
    conn: sqlite3.Connection,
    conn_corpus: sqlite3.Connection,
    *,
    autor_id: int,
    texto: str,
    lang: str,
    task_type_sugerido: str | None = None,
    domain_sugerido: str | None = None,
    brief: str | None = None,
) -> dict[str, Any]:
    """Grava a criação e devolve a linha **com o aviso de duplicata resolvido**.

    O aviso é calculado ANTES do INSERT e gravado junto: ele é um fato sobre o
    momento em que a pessoa escreveu, e o corpus troca por swap embaixo desta
    app. Quem revisa depois recebe uma checagem AO VIVO por cima (ver
    ``listar``), e as duas juntas contam a história inteira — inclusive o caso
    em que o texto virou duplicata porque o corpus foi recarregado no meio.
    """
    display, hash_norm = chave(texto)
    dup = duplicata_no_corpus(conn_corpus, hash_norm)

    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO criacoes (autor_id, texto, lang, task_type_sugerido, "
            "                      domain_sugerido, brief, hash_norm, duplicata_corpus) "
            "VALUES (:autor, :texto, :lang, :tt, :dom, :brief, :hash, :dup)",
            {
                "autor": autor_id,
                "texto": display,
                "lang": lang,
                "tt": task_type_sugerido,
                "dom": domain_sugerido,
                "brief": brief,
                "hash": hash_norm,
                "dup": 1 if dup else 0,
            },
        )
        novo = int(cur.lastrowid or 0)
        evmod.registrar(
            conn,
            acao="criacao_submetida",
            entidade="criacao",
            entidade_id=novo,
            ator_id=autor_id,
            lang=lang,
            n_chars=len(display),
            duplicata_corpus=bool(dup),
            uid_duplicata=dup,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    linha = conn.execute(f"SELECT {COLUNAS} FROM criacoes c WHERE c.id = ?", (novo,)).fetchone()
    return {**_linha(linha), "uid_duplicata": dup}


def listar(
    conn: sqlite3.Connection,
    conn_corpus: sqlite3.Connection | None = None,
    *,
    autor_id: int | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """As criações, com o nome de quem escreveu e de quem revisou.

    Com ``conn_corpus``, cada linha ganha ``uid_duplicata`` recalculado AO VIVO.
    A releitura importa no momento da decisão: o corpus é reconstruído do zero a
    cada ``pf load-db`` (o universo desta máquina foi de 144.754 para 159.733
    numa recarga), então o que era inédito na terça pode ser duplicata na
    quinta. O campo gravado continua ao lado, como o registro histórico que ele
    é — e quando os dois divergem, é isso que se quer ver.
    """
    cond, params = ["1=1"], {}
    if autor_id is not None:
        cond.append("c.autor_id = :autor")
        params["autor"] = autor_id
    if status is not None:
        cond.append("c.status = :status")
        params["status"] = status
    linhas = conn.execute(
        f"SELECT {COLUNAS}, au.nome AS autor, rv.nome AS revisor "
        "FROM criacoes c "
        "JOIN anotadores au ON au.id = c.autor_id "
        "LEFT JOIN anotadores rv ON rv.id = c.revisor_id "
        f"WHERE {' AND '.join(cond)} ORDER BY c.id DESC",
        params,
    ).fetchall()
    saida = []
    for linha in linhas:
        item = _linha(linha, autor=str(linha["autor"]), revisor=linha["revisor"])
        if conn_corpus is not None:
            item["uid_duplicata"] = duplicata_no_corpus(conn_corpus, item["hash_norm"])
        saida.append(item)
    return saida


def revisar(
    conn: sqlite3.Connection,
    criacao_id: int,
    *,
    revisor_id: int,
    aprovar: bool,
    comentario: str | None,
) -> dict[str, Any]:
    """Aprova (gravando ``uid_previsto``) ou recusa. Numa transação, com evento.

    O ``uid_previsto`` só existe a partir daqui, e é de propósito: ele é a
    promessa de qual linha o corpus vai ganhar, e uma promessa sobre trabalho
    que ainda pode ser recusado não vale nada. É calculado com o **mesmo**
    ``schema.make_uid`` que a ingestão do P6 vai usar — se os dois divergirem, o
    badge "no corpus" nunca acende, e é exatamente assim que o erro aparece.
    """
    linha = conn.execute(
        f"SELECT {COLUNAS} FROM criacoes c WHERE c.id = ?", (criacao_id,)
    ).fetchone()
    if linha is None:
        raise LookupError(f"criação {criacao_id} não existe")
    atual = str(linha["status"])
    if atual != "submetida":
        raise ValueError(
            f"esta criação está em {atual!r}; só uma 'submetida' pode ser revisada"
        )

    novo = "aprovada" if aprovar else "rejeitada"
    uid = uid_previsto(criacao_id, str(linha["texto"])) if aprovar else None

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE criacoes SET status = :status, revisor_id = :revisor, "
            "  comentario_revisao = :comentario, uid_previsto = :uid, "
            f"  revisada_em = {adb.SQL_AGORA} WHERE id = :id",
            {
                "status": novo,
                "revisor": revisor_id,
                "comentario": comentario,
                "uid": uid,
                "id": criacao_id,
            },
        )
        evmod.registrar(
            conn,
            acao="criacao_revisada",
            entidade="criacao",
            entidade_id=criacao_id,
            ator_id=revisor_id,
            status=novo,
            uid_previsto=uid,
            comentario=comentario,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return _linha(
        conn.execute(f"SELECT {COLUNAS} FROM criacoes c WHERE c.id = ?", (criacao_id,)).fetchone()
    )


def funil(conn: sqlite3.Connection) -> dict[str, int]:
    """``{status: n}`` com **todos** os status presentes, inclusive os zerados.

    Zero explícito e não chave ausente: o painel do admin desenha o funil na
    ordem de ``db.STATUS_CRIACAO``, e um degrau que some da tela porque ninguém
    chegou nele parece um degrau que não existe.
    """
    contagem = dict.fromkeys(adb.STATUS_CRIACAO, 0)
    for linha in conn.execute("SELECT status, count(*) AS n FROM criacoes GROUP BY status"):
        contagem[str(linha["status"])] = int(linha["n"])
    return contagem


__all__ = [
    "COLUNAS",
    "FONTE",
    "LICENCA",
    "chave",
    "criar",
    "duplicata_no_corpus",
    "funil",
    "listar",
    "revisar",
    "uid_previsto",
]
