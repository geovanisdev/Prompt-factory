"""plataforma — as criações da Bancada viram linhas do corpus.

A ÚNICA FONTE SEM HUB, E É ELA QUE FECHA O CÍRCULO
==================================================
As outras nove fontes trazem prompts de fora: alguém publicou um dataset e nós
baixamos. Esta traz o que foi escrito **aqui**, no modo criar da plataforma de
anotação (``annotate/criacoes.py``, P4). O insumo não é um repo do HuggingFace,
é o ``data/db/annotate.sqlite`` desta máquina — por isso ``hf_id`` está vazio no
``sources.toml``.

É o último elo do argumento do projeto: um banco de prompts escritos por gente,
com licença e proveniência por linha, que agora também **recebe** o que as
pessoas escrevem na ferramenta em vez de só consumir o que outros publicaram.

O CONTRATO É ``run_ingest`` E NÃO ``iter_rows`` — POR CAUSA DA ORDEM
====================================================================
O ``write_raw`` sabe montar o parquet, e é ele quem monta. O que ele não sabe
fazer é o passo seguinte: carimbar ``exportada`` nas criações que entraram. E
esse carimbo **tem de vir depois** de o arquivo existir no disco, pela mesma
disciplina do part-file do WildChat (part → state, nunca o contrário): marcar
antes e falhar na escrita produziria um funil dizendo "exportada" sobre uma
linha que nunca chegou a ``data/raw/`` — e o painel do admin prometeria uma
ingestão que ninguém rodou.

O PASSE LÊ ``aprovada`` **E** ``exportada``, E ISSO PARECE UM BUG
=================================================================
Parece que reingerir o que já foi exportado é trabalho repetido. É o contrário:
o ``write_raw`` **reescreve o parquet inteiro** a cada passe. Um passe que
levasse só as ``aprovada`` novas produziria um ``plataforma.parquet`` sem as
antigas — e o próximo ``pf run`` apagaria do corpus todas as criações
anteriores, sem erro nenhum, porque a fonte simplesmente teria menos linhas.

O efeito colateral bom: o carimbo é **auto-curável**. Se alguém marcar
``exportada`` cedo demais (um passe de ensaio contra o banco de verdade), basta
rodar o passe canônico de novo — as linhas continuam sendo lidas, e o parquet
sai completo.

TRÊS VALORES TÊM DE CASAR COM A PLATAFORMA, E OS TRÊS FALHAM EM SILÊNCIO
========================================================================
1. ``source`` = ``criacoes.FONTE``  2. ``license`` = ``criacoes.LICENCA``
3. ``source_id`` = o id da criação — é ele que faz ``schema.make_uid`` reproduzir
   exatamente o ``uid_previsto`` que a aprovação gravou (com ``source_id``
   presente, o uid sai de ``"plataforma:<id>"`` e não depende do texto).

Nenhum dos três levanta exceção se divergir: o parquet sai bonito, a pipeline
roda, e o uid prometido pela plataforma simplesmente nunca aparece no corpus.
Por isso os três vêm importados de ``annotate.criacoes`` e do ``sources.toml``,
nunca reescritos aqui — e ``_conferir_uid`` recalcula o uid de cada linha e
**para o passe** quando ele não bate com o que está gravado. É a única maneira
de essa classe de erro fazer barulho.

O QUE **NÃO** VAI PARA O RAW
=============================
* **Texto livre que não seja o prompt.** O ``brief`` do autor e o comentário do
  revisor ficam de fora do ``meta_json``: o s03 limpa PII de ``text``, e só de
  ``text``. Campo livre que entra pelo metadado atravessa a pipeline inteira
  sem passar por essa peneira.
* **Nome de quem escreveu.** CC0 dispensa crédito, e um nome de pessoa numa
  coluna que ninguém limpa é o mesmo problema acima com consequência maior.
* **As sugestões como ``native_category``.** Elas VÃO no ``meta_json``, mas não
  na coluna que o s08 promove a rótulo pelos ``labeling/mappings/``: são palpite
  de quem escreveu, e o contrato do formulário diz que viajam "como pista para
  quem revisa, e não como rótulo". O rótulo do corpus sai da campanha.

``lang``, ao contrário, VAI — como ``lang_source``. Não é um veredito: o s01 só
copia ``pt``/``en`` exatos, e o s02 roda os dois detectores em toda linha de
qualquer jeito. O que a declaração faz é servir de PRIOR: quando o detector
discorda dela, o árbitro caro é acionado. É o melhor uso possível de um idioma
declarado por quem escreveu o texto.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .. import db as dbmod
from .. import paths
from ..annotate import criacoes as crimod
from ..annotate import db as adb
from ..annotate import eventos as evmod
from . import base

SOURCE = crimod.FONTE

#: Os status que entram no parquet. ``aprovada`` é o trabalho novo;
#: ``exportada`` é o que já entrou antes e **precisa** continuar entrando (ver
#: o cabeçalho). ``submetida`` e ``rejeitada`` nunca entram: uma ainda não foi
#: julgada, a outra foi recusada.
STATUS_INGERIVEIS: tuple[str, ...] = ("aprovada", "exportada")

#: As colunas lidas de ``criacoes``. Explícitas em vez de ``SELECT *`` para que
#: uma coluna nova na plataforma não comece a viajar para o corpus sozinha —
#: o que entra no raw é decisão, não consequência de um ALTER TABLE.
_COLUNAS = (
    "id, texto, lang, task_type_sugerido, domain_sugerido, "
    "status, duplicata_corpus, uid_previsto, criada_em, revisada_em"
)


def selecionar(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """As criações ingeríveis, em ordem de id (que é a ordem de criação).

    Ordem estável porque o parquet é comparado entre execuções: o mesmo banco
    tem de produzir o mesmo arquivo, e ``ORDER BY`` ausente no SQLite é uma
    ordem que **costuma** ser a do rowid — costuma não é contrato.
    """
    marcas = ",".join("?" * len(STATUS_INGERIVEIS))
    return list(
        conn.execute(
            f"SELECT {_COLUNAS} FROM criacoes WHERE status IN ({marcas}) ORDER BY id",
            STATUS_INGERIVEIS,
        ).fetchall()
    )


def _conferir_uid(linha: sqlite3.Row) -> str:
    """O uid desta criação, recalculado — e comparado com o que foi prometido.

    A divergência é FATAL de propósito. Ela só pode acontecer se ``FONTE``, o id
    ou a fórmula de ``make_uid`` mudarem entre a aprovação e a ingestão, e o
    sintoma natural seria mudo: o corpus ganharia uma linha com outro uid e a
    plataforma continuaria exibindo o uid antigo para sempre, sem nada errado à
    vista. Parar aqui é o que transforma isso num erro que alguém lê.
    """
    uid = crimod.uid_previsto(int(linha["id"]), str(linha["texto"]))
    prometido = linha["uid_previsto"]
    if prometido and str(prometido) != uid:
        raise SystemExit(
            f"[{SOURCE}] criação {linha['id']}: uid prometido na aprovação "
            f"({prometido!r}) != uid recalculado agora ({uid!r}). A cadeia do "
            "uid divergiu entre a plataforma e a pipeline — conserte antes de "
            "ingerir, senão o corpus ganha uma linha que a Bancada nunca "
            "reconhece."
        )
    if not prometido:
        print(
            f"[{SOURCE}] AVISO criação {linha['id']}: sem uid_previsto gravado "
            f"(aprovada antes do P4?). Usando o recalculado: {uid}"
        )
    return uid


def montar_linha(linha: sqlite3.Row) -> dict[str, Any]:
    """Uma criação -> as 12 chaves do ``RAW_SCHEMA``.

    ``text_raw`` recebe o texto **como a plataforma o guardou**, que já passou
    por ``norm_display`` no envio. Não é contradição com "raw é cru": raw é o
    que a FONTE tem, e a fonte aqui é o banco da Bancada. O s01 vai aplicar
    ``norm_display`` de novo, e o resultado é o mesmo texto — a normalização é
    idempotente, e há teste cobrando isso.
    """
    _conferir_uid(linha)
    return base.make_row(
        source=SOURCE,
        source_id=int(linha["id"]),
        source_split="",  # a plataforma não tem splits
        text_raw=str(linha["texto"]),
        lang_source=str(linha["lang"] or ""),
        created_ts=base.to_iso(linha["criada_em"]),
        native_category="",  # ver "O QUE NÃO VAI PARA O RAW", no cabeçalho
        meta={
            "criacao_id": int(linha["id"]),
            "task_type_sugerido": linha["task_type_sugerido"],
            "domain_sugerido": linha["domain_sugerido"],
            "duplicata_corpus": bool(linha["duplicata_corpus"]),
            "revisada_em": linha["revisada_em"],
        },
    )


def marcar_exportadas(conn: sqlite3.Connection, ids: Sequence[int]) -> int:
    """Carimba ``exportada`` (+ ``exportada_em``) nas criações que entraram.

    Só as que ainda estão em ``aprovada``: as que já eram ``exportada`` foram
    reescritas no parquet, e mexer no ``exportada_em`` delas moveria a data de
    uma coisa que aconteceu na primeira vez.

    Numa transação com o evento, como toda escrita desta plataforma. A conexão
    é de ESCRITA e isso é seguro mesmo com a Bancada no ar: ela não guarda
    agregado em cache (decisão registrada em ``annotate/main.py``), então um
    escritor externo não tem como deixar número velho na tela.
    """
    if not ids:
        return 0
    marcas = ",".join("?" * len(ids))
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            f"UPDATE criacoes SET status = 'exportada', exportada_em = {adb.SQL_AGORA} "
            f"WHERE id IN ({marcas}) AND status = 'aprovada'",
            tuple(int(i) for i in ids),
        )
        n = int(cur.rowcount or 0)
        if n:
            evmod.registrar(
                conn,
                acao="criacao_exportada",
                entidade="criacao",
                # Um evento por PASSE, e não por linha: o fato registrado é a
                # ingestão, e ela é atômica. Os ids vão no detalhe.
                entidade_id=None,
                fonte=SOURCE,
                n=n,
                ids=[int(i) for i in ids],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return n


def exportar(
    conn: sqlite3.Connection,
    *,
    raw_dir: Path | None = None,
    marcar: bool = True,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """O passe inteiro: lê, monta, grava o parquet e (talvez) carimba.

    Devolve ``{parquet, linhas, aprovadas, reexportadas, carimbadas}``.

    ``marcar=False`` é o modo ENSAIO, e ele existe por um footgun concreto:
    ``--data-dir tmp`` contra o banco de verdade escreveria um parquet numa
    pasta descartável e mesmo assim moveria o funil de ``aprovada`` para
    ``exportada``. O prejuízo é auto-curável (ver o cabeçalho), mas um funil que
    mente até alguém reparar é pior que um passe que não carimba e diz isso.
    """
    linhas = selecionar(conn)
    if max_rows is not None:
        linhas = linhas[:max_rows]
    novas = [int(x["id"]) for x in linhas if str(x["status"]) == "aprovada"]

    destino = base.write_raw(
        SOURCE, (montar_linha(x) for x in linhas), max_rows, raw_dir=raw_dir
    )
    carimbadas = marcar_exportadas(conn, novas) if marcar else 0
    return {
        "parquet": destino,
        "linhas": len(linhas),
        "aprovadas": len(novas),
        "reexportadas": len(linhas) - len(novas),
        "carimbadas": carimbadas,
    }


def run_ingest(
    source_name: str, cfg: Mapping[str, Any], args: argparse.Namespace
) -> int:
    """``pf ingest plataforma`` — a casca da CLI sobre ``exportar``.

    O carimbo só acontece no passe **canônico e completo**: sem ``--data-dir``
    (o parquet tem de ir para a árvore de verdade) e sem ``--max-rows`` (um
    recorte carimbaria as linhas que couberam e deixaria o parquet incompleto no
    lugar do bom). Uma regra só, dita na tela quando ela se aplica.
    """
    banco = Path(getattr(args, "annotate_db", None) or paths.ANNOTATE_DB_FILE)
    if not banco.is_file():
        print(
            f"[{SOURCE}] banco da plataforma não encontrado em {banco}.\n"
            f"[{SOURCE}] Quem o cria é a própria Bancada: rode `pf annotate serve` "
            "(ou `pf annotate seed`) uma vez."
        )
        return 2

    raw_dir = Path(args.data_dir).resolve() / "raw" if getattr(args, "data_dir", None) else None
    max_rows = getattr(args, "max_rows", None)
    marcar = raw_dir is None and max_rows is None
    if not marcar:
        motivo = "--data-dir" if raw_dir is not None else "--max-rows"
        print(
            f"[{SOURCE}] ENSAIO ({motivo}): o parquet é escrito, mas NADA é "
            "carimbado como exportada no banco da plataforma."
        )

    conn = dbmod.connect(banco)
    try:
        resumo = exportar(conn, raw_dir=raw_dir, marcar=marcar, max_rows=max_rows)
    finally:
        conn.close()

    if resumo["linhas"] == 0:
        print(
            f"[{SOURCE}] nenhuma criação aprovada ainda — o parquet saiu vazio.\n"
            f"[{SOURCE}] O funil está em `pf annotate status`: quem escreve é o "
            "anotador (aba 'Escrever um prompt'), quem aprova é o revisor."
        )
        return 0
    print(
        f"[{SOURCE}] {resumo['aprovadas']} nova(s) + {resumo['reexportadas']} "
        f"já exportada(s) = {resumo['linhas']} linha(s); "
        f"{resumo['carimbadas']} carimbada(s) como exportada"
    )
    if marcar:
        print(
            f"[{SOURCE}] agora rode `pf run s01-s06` e `pf load-db` para que elas "
            "cheguem ao corpus (o uid já está prometido em uid_previsto)"
        )
    return 0


__all__ = [
    "SOURCE",
    "STATUS_INGERIVEIS",
    "exportar",
    "marcar_exportadas",
    "montar_linha",
    "run_ingest",
    "selecionar",
]
