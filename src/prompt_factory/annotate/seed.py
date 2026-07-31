"""``pf annotate seed`` — personas, pacote de demonstração e tarefas do pool real.

Três camadas, nesta ordem, e cada uma idempotente pela CHAVE NATURAL do que
insere:

1. **Personas**, por ``nome`` (UNIQUE no DDL).
2. **Pacote de demonstração** (``fixtures/demo_pack.json``), por ``uid_demo`` nos
   prompts, ``(prompt_uid, titulo)`` nas rubricas, ``(prompt_uid, rotulo)`` nas
   respostas e ``(tipo, prompt_uid)`` nas tarefas.
3. **Tarefas sobre o corpus real**, ``[annotate] seed_pool_tarefas`` de
   ``escrever_rubrica`` e o mesmo tanto de ``sft_resposta``, sobre os primeiros
   N uids do pool **na ordem determinística dele**.

IDEMPOTENTE SIGNIFICA "SEMEAR É COMEÇAR, NÃO REINICIALIZAR"
============================================================
Rodar duas vezes não duplica nada e **não sobrescreve** nada: um papel que
alguém mudou na tela continua mudado, uma tarefa que o admin pausou continua
pausada. Quem quer reinicializar pede ``--force`` — e o ``--force`` recusa se
houver anotação submetida, porque refazer as fixtures por cima de trabalho
humano é a única operação verdadeiramente irreversível desta app.

POR QUE O PACOTE É JSON DENTRO DO PACOTE PYTHON
================================================
``data/`` é gitignorado: uma fixture ali não sobreviveria a um clone. E o
conteúdo do pacote é parte do produto — as oito rubricas e os defeitos plantados
são a prova de domínio que um avaliador lê antes de olhar o código. Ele é
versionado, revisável em diff e igual em qualquer máquina.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..config import get as _cfg
from . import db as adb
from . import pool as poolmod

#: O pacote de demonstração, dentro do pacote Python.
PACOTE = Path(__file__).resolve().parent / "fixtures" / "demo_pack.json"

#: As seis personas do plano: 3 anotadores, 2 revisores, 1 admin.
#:
#: Dois revisores existem por um motivo de produto, não de simetria: o revisor
#: **não pode revisar a própria anotação** (P3), então um revisor sozinho
#: travaria a fila assim que ele mesmo anotasse alguma coisa. Três anotadores
#: são o mínimo para uma tarefa com ``n_anotacoes_alvo = 2`` ainda deixar alguém
#: de fora e o agreement ter de quem discordar.
PERSONAS: tuple[tuple[str, str], ...] = (
    ("Ana Ribeiro", "anotador"),
    ("Bruno Tavares", "anotador"),
    ("Carla Nunes", "anotador"),
    ("Diego Prado", "revisor"),
    ("Elisa Marques", "revisor"),
    ("Marina Alves", "admin"),
)

#: Contrato do ``criterios_json`` das rubricas. Vive numa COLUNA e não no código
#: pelo mesmo motivo do ``payload_schema``: a versão viaja com o dado.
SCHEMA_RUBRICA = "rubrica@1"

#: Prioridade base das tarefas geradas sobre o pool real. **Abaixo** das do
#: pacote (60 a 90) de propósito: quem abre a demonstração precisa cair primeiro
#: nas fixtures escritas à mão, que têm rubrica pronta e resposta com defeito
#: plantado. As do corpus real são o segundo ato.
PRIORIDADE_POOL = 40


def semear_personas(conn: sqlite3.Connection) -> int:
    """Insere as personas que faltam. Devolve quantas ENTRARAM de fato.

    A conta é a diferença do total, e não ``changes()``: depois de um
    ``executemany`` o ``changes()`` reporta só a última instrução.
    """
    antes = int(conn.execute("SELECT count(*) AS n FROM anotadores").fetchone()["n"])
    conn.executemany(
        "INSERT INTO anotadores (nome, papel) VALUES (?, ?) ON CONFLICT(nome) DO NOTHING",
        PERSONAS,
    )
    depois = int(conn.execute("SELECT count(*) AS n FROM anotadores").fetchone()["n"])
    return depois - antes


def tem_trabalho(conn: sqlite3.Connection) -> bool:
    """``True`` se já existe anotação submetida neste banco.

    O ``--force`` do seed consulta isto **antes** de destruir qualquer coisa.
    """
    linha = conn.execute("SELECT EXISTS(SELECT 1 FROM anotacoes) AS tem").fetchone()
    return bool(linha["tem"])


def carregar_pacote(caminho: Path | None = None) -> dict[str, Any]:
    """Lê e confere o ``demo_pack.json``.

    A conferência é estrutural e falha ALTO: um pacote quebrado tem de aparecer
    no ``pf annotate seed``, no terminal, e não como uma tela vazia depois.
    """
    alvo = Path(caminho) if caminho is not None else PACOTE
    dados = json.loads(alvo.read_text(encoding="utf-8"))
    itens = dados.get("itens") or []
    chaves = {item["chave"] for item in itens}
    if len(chaves) != len(itens):
        raise ValueError(f"{alvo}: duas entradas com a mesma 'chave'")
    for tarefa in dados.get("tarefas") or []:
        if tarefa["item"] not in chaves:
            raise ValueError(f"{alvo}: tarefa aponta para item inexistente {tarefa['item']!r}")
        if tarefa["tipo"] not in adb.TIPOS_TAREFA:
            raise ValueError(f"{alvo}: tipo desconhecido {tarefa['tipo']!r}")
    return dados


# ---------------------------------------------------------------------------
# camada 2: o pacote de demonstração
# ---------------------------------------------------------------------------


def _existe(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> bool:
    return conn.execute(sql, params).fetchone() is not None


def semear_pacote(conn: sqlite3.Connection, dados: dict[str, Any] | None = None) -> dict[str, int]:
    """Prompts, rubricas, respostas e tarefas do pacote. Devolve o que ENTROU."""
    pacote = dados if dados is not None else carregar_pacote()
    conta = dict.fromkeys(("prompts_demo", "rubricas", "respostas_modelo", "tarefas"), 0)
    uid_por_chave: dict[str, str] = {}

    for item in pacote["itens"]:
        texto = item["prompt"]
        # Endereçado pelo CONTEÚDO: editar o texto de uma fixture cria um item
        # novo em vez de mudar o antigo por baixo de uma anotação existente.
        uid = adb.uid_demo(texto)
        uid_por_chave[item["chave"]] = uid
        cur = conn.execute(
            "INSERT INTO prompts_demo (uid, text, lang) VALUES (?, ?, ?) "
            "ON CONFLICT(uid) DO NOTHING",
            (uid, texto, item["lang"]),
        )
        conta["prompts_demo"] += int(cur.rowcount or 0)

        rub = item["rubrica"]
        if not _existe(
            conn,
            "SELECT 1 FROM rubricas WHERE prompt_uid = ? AND titulo = ?",
            (uid, rub["titulo"]),
        ):
            conn.execute(
                "INSERT INTO rubricas (prompt_uid, titulo, criterios_json, origem, status) "
                "VALUES (?, ?, ?, 'fixture', 'ativa')",
                (
                    uid,
                    rub["titulo"],
                    json.dumps(
                        {"schema": SCHEMA_RUBRICA, "criterios": rub["criterios"]},
                        ensure_ascii=False,
                    ),
                ),
            )
            conta["rubricas"] += 1

        for resposta in item["respostas"]:
            if _existe(
                conn,
                "SELECT 1 FROM respostas_modelo WHERE prompt_uid = ? AND rotulo_modelo = ?",
                (uid, resposta["rotulo_modelo"]),
            ):
                continue
            conn.execute(
                "INSERT INTO respostas_modelo (prompt_uid, rotulo_modelo, texto, origem, meta_json) "
                "VALUES (?, ?, ?, 'fixture', ?)",
                (
                    uid,
                    resposta["rotulo_modelo"],
                    resposta["texto"],
                    # O defeito plantado mora AQUI, e o `meta_json` nunca sai
                    # pela API do anotador (ver `tarefas._respostas`).
                    json.dumps(resposta["meta"], ensure_ascii=False),
                ),
            )
            conta["respostas_modelo"] += 1

    for tarefa in pacote["tarefas"]:
        uid = uid_por_chave[tarefa["item"]]
        if _existe(
            conn, "SELECT 1 FROM tarefas WHERE tipo = ? AND prompt_uid = ?", (tarefa["tipo"], uid)
        ):
            continue
        payload: dict[str, Any] = {}
        if tarefa.get("resposta"):
            # Qual das duas respostas está sendo avaliada. Sem isto a hidratação
            # não saberia o que mostrar numa tarefa de `avaliar_rubrica`.
            payload["resposta"] = tarefa["resposta"]
        conn.execute(
            "INSERT INTO tarefas (tipo, prompt_uid, origem, payload_json, gabarito_json, "
            "                     n_anotacoes_alvo, prioridade, status) "
            "VALUES (?, ?, 'semente', ?, ?, ?, ?, 'aberta')",
            (
                tarefa["tipo"],
                uid,
                json.dumps(payload, ensure_ascii=False),
                None
                if tarefa.get("gabarito") is None
                else json.dumps(tarefa["gabarito"], ensure_ascii=False),
                int(tarefa.get("n_anotacoes_alvo", 1)),
                int(tarefa.get("prioridade", 0)),
            ),
        )
        conta["tarefas"] += 1

    return conta


# ---------------------------------------------------------------------------
# camada 3: tarefas sobre o corpus real
# ---------------------------------------------------------------------------


def semear_pool(
    conn: sqlite3.Connection, conn_corpus: sqlite3.Connection
) -> dict[str, int]:
    """``seed_pool_tarefas`` de ``escrever_rubrica`` + o mesmo tanto de ``sft_resposta``.

    **Prompts diferentes para cada tipo**, e não os mesmos: com os mesmos, a fila
    de SFT já traria a rubrica pronta e a oferta de continuação (rubrica → SFT)
    nunca teria o que criar. Separando, a continuação vira uma tarefa nova de
    ``origem='continuacao'`` e o mecanismo fica visível na demonstração.

    Determinístico: os uids do pool saem sempre na mesma ordem (amostra de passo
    constante sobre o corpus), então rodar em duas máquinas com o mesmo corpus
    gera as mesmas tarefas — e rodar duas vezes na mesma não gera nenhuma.
    """
    quantas = int(_cfg("annotate", "seed_pool_tarefas", default=8))
    uids = poolmod.resolver(conn_corpus, com_uids=True).uids
    if not uids:
        return {"escrever_rubrica": 0, "sft_resposta": 0}

    # Fatias disjuntas. Num pool menor que 2*N (só acontece em teste ou num
    # corpus minúsculo) as fatias se sobrepõem — e o find-or-create por
    # (tipo, prompt_uid) resolve, porque os TIPOS são diferentes.
    para_rubrica = uids[:quantas]
    para_sft = uids[quantas : quantas * 2] or uids[:quantas]

    conta = {"escrever_rubrica": 0, "sft_resposta": 0}
    for tipo, lista in (("escrever_rubrica", para_rubrica), ("sft_resposta", para_sft)):
        for i, uid in enumerate(lista):
            if _existe(
                conn, "SELECT 1 FROM tarefas WHERE tipo = ? AND prompt_uid = ?", (tipo, uid)
            ):
                continue
            conn.execute(
                "INSERT INTO tarefas (tipo, prompt_uid, origem, prioridade, status) "
                "VALUES (?, ?, 'semente', ?, 'aberta')",
                # Prioridade decrescente na ordem do pool: a fila sai na mesma
                # ordem em duas execuções, o que é o que torna a demonstração
                # reproduzível ("a primeira tarefa é sempre esta").
                (tipo, uid, PRIORIDADE_POOL - i),
            )
            conta[tipo] += 1
    return conta


# ---------------------------------------------------------------------------
# --force
# ---------------------------------------------------------------------------

#: O que o ``--force`` apaga, na ordem que respeita as FKs. ``anotadores`` NÃO
#: está aqui: personas não são fixture descartável, elas são a identidade que o
#: navegador guardou no localStorage — apagá-las deslogaria quem estivesse com a
#: tela aberta, por nada.
TABELAS_FIXTURE: tuple[str, ...] = ("tarefas", "rubricas", "respostas_modelo", "prompts_demo")


def apagar_fixtures(conn: sqlite3.Connection) -> dict[str, int]:
    """Apaga tarefas e material de demonstração. **Recuse antes se houver trabalho.**

    ``tarefas`` leva junto ``atribuicoes`` e, por elas, ``anotacoes``
    (ON DELETE CASCADE) — que é exatamente por que ``tem_trabalho`` é consultado
    antes desta função, e não dentro dela: quem chama precisa poder recusar com
    uma mensagem, em vez de descobrir o estrago depois.
    """
    conta: dict[str, int] = {}
    for tabela in TABELAS_FIXTURE:
        cur = conn.execute(f"DELETE FROM {tabela}")
        conta[tabela] = int(cur.rowcount or 0)
    return conta


# ---------------------------------------------------------------------------
# orquestração
# ---------------------------------------------------------------------------


def semear(
    conn: sqlite3.Connection,
    conn_corpus: sqlite3.Connection | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """As três camadas de uma vez. Devolve o relatório que a CLI imprime.

    ``conn_corpus=None`` semeia só personas + pacote e avisa: sem corpus não há
    pool, e as tarefas sobre prompts reais simplesmente não existem. É um estado
    legítimo (clone limpo antes do ``pf load-db``), não um erro.
    """
    relatorio: dict[str, Any] = {"forcado": force, "apagados": None, "avisos": []}
    if force:
        if tem_trabalho(conn):
            raise RuntimeError(
                "há anotação submetida neste banco — o --force apagaria trabalho humano, "
                "que não se recria a partir da pipeline. Mova o arquivo para o lado se "
                "quiser mesmo começar do zero."
            )
        relatorio["apagados"] = apagar_fixtures(conn)

    relatorio["personas"] = semear_personas(conn)
    relatorio["pacote"] = semear_pacote(conn)
    if conn_corpus is None:
        relatorio["pool"] = {"escrever_rubrica": 0, "sft_resposta": 0}
        relatorio["avisos"].append(
            "sem corpus: só o pacote de demonstração foi semeado. "
            "Rode `pf load-db` e depois `pf annotate seed` de novo para as tarefas "
            "sobre prompts reais."
        )
    else:
        relatorio["pool"] = semear_pool(conn, conn_corpus)
    relatorio["contagens"] = adb.contagens(conn)
    return relatorio


__all__ = [
    "PACOTE",
    "PERSONAS",
    "PRIORIDADE_POOL",
    "SCHEMA_RUBRICA",
    "TABELAS_FIXTURE",
    "apagar_fixtures",
    "carregar_pacote",
    "semear",
    "semear_pacote",
    "semear_personas",
    "semear_pool",
    "tem_trabalho",
]
