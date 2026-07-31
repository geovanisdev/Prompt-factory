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
from . import diretrizes as dirmod
from . import pool as poolmod
from . import projetos as projmod

#: O pacote de demonstração, dentro do pacote Python.
PACOTE = Path(__file__).resolve().parent / "fixtures" / "demo_pack.json"

#: Qualificação (P3a). A coluna existe e É ESCRITA; **barrar** quem não é
#: qualificado fica para quando a tela existir. Estas são as combinações que a
#: demonstração precisa sustentar: todo anotador é qualificado nos quatro tipos,
#: menos Carla no A/B — para que exista um caso "não qualificada" visível no
#: dado desde o primeiro dia, em vez de uma coluna uniforme que não prova nada.
QUALIFICACOES: dict[str, dict[str, str]] = {
    "Ana Ribeiro": dict.fromkeys(adb.TIPOS_TAREFA, "aprovada"),
    "Bruno Tavares": dict.fromkeys(adb.TIPOS_TAREFA, "aprovada"),
    "Carla Nunes": {
        **dict.fromkeys(adb.TIPOS_TAREFA, "aprovada"),
        "comparar_ab": "pendente",
    },
}

#: As seis personas do plano: 3 anotadores, 2 revisores, 1 admin.
#:
#: Dois revisores existem por um motivo de produto, não de simetria: o revisor
#: **não pode revisar a própria anotação** (P3a), então um revisor sozinho
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
#:
#: ``@2`` (P3i) acrescentou os sidecares de exibição ``titulo_i18n``,
#: ``nome_i18n``, ``descricao_i18n`` e ``rotulo_i18n``. Eles são **opcionais**:
#: a rubrica escrita por um anotador tem uma língua só, e é isso mesmo — ela é
#: dado, não cromo, e a plataforma não inventa tradução de dado. O que NÃO mudou
#: é o campo canônico (``titulo``, ``nome``, ``descricao``, ``rotulo``), que é a
#: identidade referenciada por ``anotacoes.payload_json``.
SCHEMA_RUBRICA = "rubrica@2"

#: Prioridade base das tarefas geradas sobre o pool real. **Abaixo** das do
#: pacote (60 a 90) de propósito: quem abre a demonstração precisa cair primeiro
#: nas fixtures escritas à mão, que têm rubrica pronta e resposta com defeito
#: plantado. As do corpus real são o segundo ato.
PRIORIDADE_POOL = 40


def semear_personas(conn: sqlite3.Connection) -> int:
    """Insere as personas que faltam. Devolve quantas ENTRARAM de fato.

    A conta é a diferença do total, e não ``changes()``: depois de um
    ``executemany`` o ``changes()`` reporta só a última instrução.

    A qualificação é escrita **só nas personas que entram agora**: um papel ou
    uma qualificação que alguém mudou na tela continua mudado, porque semear é
    começar, não reinicializar.
    """
    antes = int(conn.execute("SELECT count(*) AS n FROM anotadores").fetchone()["n"])
    conn.executemany(
        "INSERT INTO anotadores (nome, papel, qualificacoes_json) VALUES (?, ?, ?) "
        "ON CONFLICT(nome) DO NOTHING",
        [
            (nome, papel, json.dumps(QUALIFICACOES.get(nome, {}), ensure_ascii=False))
            for nome, papel in PERSONAS
        ],
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


#: Os sidecares de exibição do P3i. Tudo que termina assim é TRADUÇÃO, e nada
#: que termina assim entra na identidade de um critério.
SUFIXO_I18N = "_i18n"

#: Chaves que nomeiam a FORMA do payload, não o conteúdo dele. Ficam de fora da
#: comparação de ``_canonico`` porque a pergunta que ela responde é "isto continua
#: dizendo a mesma coisa?", e o nome da forma não é o que ela diz.
CHAVES_DE_FORMA = frozenset({"schema"})


def _canonico(valor: Any) -> Any:
    """O mesmo objeto sem os ``*_i18n`` e sem o rótulo de schema — a IDENTIDADE.

    É contra isto que ``_traduzir`` compara antes de reescrever uma fixture:
    acrescentar tradução não muda a rubrica, mas mudar um critério muda — e
    mudar um critério por baixo de uma anotação já gravada quebraria a ligação
    entre ``notas[].criterio`` e a rubrica que a produziu.
    """
    if isinstance(valor, dict):
        return {
            k: _canonico(v)
            for k, v in valor.items()
            if not str(k).endswith(SUFIXO_I18N) and k not in CHAVES_DE_FORMA
        }
    if isinstance(valor, list):
        return [_canonico(x) for x in valor]
    return valor


def _traduzir(
    conn: sqlite3.Connection,
    tabela: str,
    linha: sqlite3.Row,
    coluna: str,
    novo_json: str,
) -> bool:
    """Acrescenta as traduções a uma fixture já semeada. ``True`` se atualizou.

    O PROBLEMA REAL QUE ISTO RESOLVE (P3i)
    ======================================
    ``semear_pacote`` é idempotente pela chave natural — ``(prompt_uid, titulo)``
    na rubrica, ``(prompt_uid, rotulo_modelo)`` na resposta. Ótimo enquanto o
    pacote só cresce; inútil quando ele ganha uma DIMENSÃO. O banco do dono já
    tem as oito rubricas semeadas na forma monolíngue, e "pular porque já
    existe" as deixaria monolíngues para sempre — a interface abriria em inglês
    com rubricas em português, que é exatamente o defeito que este marco existe
    para não ter.

    A regra da atualização é estreita e verificável: **só se o canônico for
    idêntico** (``_canonico`` tira os ``*_i18n`` dos dois lados e compara) e só
    se a linha for ``origem = 'fixture'``. Acrescentar tradução não muda a
    rubrica; mudar um critério muda, e continuaria proibido — ele é a identidade
    que ``anotacoes.payload_json`` referencia em ``notas[].criterio``.
    """
    if str(linha["origem"]) != "fixture":
        return False
    gravado = str(linha[coluna] or "")
    if gravado == novo_json:
        return False
    try:
        antes, depois = json.loads(gravado or "{}"), json.loads(novo_json)
    except ValueError:  # pragma: no cover - JSON corrompido à mão
        return False
    if _canonico(antes) != _canonico(depois):
        return False
    conn.execute(f"UPDATE {tabela} SET {coluna} = ? WHERE id = ?", (novo_json, int(linha["id"])))
    return True


def semear_pacote(conn: sqlite3.Connection, dados: dict[str, Any] | None = None) -> dict[str, int]:
    """Prompts, rubricas, respostas e tarefas do pacote. Devolve o que ENTROU."""
    pacote = dados if dados is not None else carregar_pacote()
    conta = dict.fromkeys(
        ("prompts_demo", "rubricas", "respostas_modelo", "tarefas", "traduzidas"), 0
    )
    uid_por_chave: dict[str, str] = {}
    # O pacote vai para o projeto DEMONSTRAÇÃO, não para o do trabalho real:
    # misturar fixture com autoria na mesma lista apagaria justamente a
    # distinção que dá valor ao segundo. Ver `projetos.py`.
    projeto_id = projmod.garantir_demonstracao(conn)

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
        # `titulo_i18n` entra no JSON, e não numa coluna: o título CANÔNICO é a
        # coluna `titulo` (chave natural da idempotência) e a tradução é
        # exibição. Uma coluna a mais faria a chave natural virar um par.
        conteudo: dict[str, Any] = {"schema": SCHEMA_RUBRICA}
        # Ausente, e não `null`: uma rubrica sem tradução é uma rubrica de uma
        # língua só (o caso das escritas por anotadores), e um `"titulo_i18n":
        # null` gravado diria que alguém apagou a tradução dela.
        if rub.get("titulo_i18n"):
            conteudo["titulo_i18n"] = rub["titulo_i18n"]
        conteudo["criterios"] = rub["criterios"]
        criterios_json = json.dumps(conteudo, ensure_ascii=False)
        linha = conn.execute(
            "SELECT id, criterios_json, origem FROM rubricas "
            "WHERE prompt_uid = ? AND titulo = ?",
            (uid, rub["titulo"]),
        ).fetchone()
        if linha is None:
            conn.execute(
                "INSERT INTO rubricas (prompt_uid, titulo, criterios_json, origem, status) "
                "VALUES (?, ?, ?, 'fixture', 'ativa')",
                (uid, rub["titulo"], criterios_json),
            )
            conta["rubricas"] += 1
        elif _traduzir(conn, "rubricas", linha, "criterios_json", criterios_json):
            conta["traduzidas"] += 1

        for resposta in item["respostas"]:
            # O defeito plantado mora AQUI, e o `meta_json` nunca sai pela API do
            # anotador (ver `tarefas._respostas`).
            meta_json = json.dumps(resposta["meta"], ensure_ascii=False)
            linha = conn.execute(
                "SELECT id, meta_json, origem FROM respostas_modelo "
                "WHERE prompt_uid = ? AND rotulo_modelo = ?",
                (uid, resposta["rotulo_modelo"]),
            ).fetchone()
            if linha is None:
                conn.execute(
                    "INSERT INTO respostas_modelo "
                    "(prompt_uid, rotulo_modelo, texto, origem, meta_json) "
                    "VALUES (?, ?, ?, 'fixture', ?)",
                    (uid, resposta["rotulo_modelo"], resposta["texto"], meta_json),
                )
                conta["respostas_modelo"] += 1
            elif _traduzir(conn, "respostas_modelo", linha, "meta_json", meta_json):
                conta["traduzidas"] += 1

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
            "INSERT INTO tarefas (tipo, prompt_uid, origem, projeto_id, payload_json, "
            "                     gabarito_json, n_anotacoes_alvo, prioridade, status) "
            "VALUES (?, ?, 'semente', ?, ?, ?, ?, ?, 'aberta')",
            (
                tarefa["tipo"],
                uid,
                projeto_id,
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


def fatias_por_passo(
    uids: list[str], quantas: int
) -> tuple[list[str], list[str]]:
    """Duas fatias disjuntas de ``quantas`` uids, **espalhadas pelo pool inteiro**.

    O DEFEITO QUE ISTO CONSERTA (medido)
    ====================================
    A versão anterior fazia ``uids[:8]`` e ``uids[8:16]``. O pool é ordenado por
    ``id`` do corpus, e o universo é **gravado agrupado por fonte**: os
    dezesseis primeiros saíam praticamente todos da mesma fonte. Medido no banco
    real: **16 das 17 tarefas do corpus eram ``arena140k``** — e uma delas era o
    texto "Você não tem nada pra responder. Isso não é realmente um prompt…",
    impossível de rotular. A plataforma passava a mentir sobre o corpus que ela
    representa logo na primeira tela que um avaliador abre.

    É exatamente o mesmo defeito que o P1 já havia consertado em
    ``pool._uids_fallback``, com a mesma solução: **passo constante sobre o
    conjunto inteiro**. O passo do pool devolve a proporção real das fontes; o
    passo daqui preserva essa proporção dentro da amostra que vira tarefa.

    Depois de escolher ``2 * quantas`` posições com passo constante, as duas
    fatias saem **alternadas** (pares e ímpares). Alternar, e não cortar ao
    meio, é o que impede que a segunda metade caia toda numa fonte só quando o
    pool tem blocos grandes — que é o caso do corpus de hoje (411 dos 500 uids
    são ``wildchat_pt``).

    Determinístico: mesma lista, mesmas fatias, sempre.
    """
    if quantas <= 0 or not uids:
        return [], []
    alvo = min(len(uids), quantas * 2)
    passo = len(uids) / alvo
    escolhidos = [uids[int(i * passo)] for i in range(alvo)]
    return escolhidos[0::2], escolhidos[1::2]


#: Os tipos que nascem de PROMPT CRU do corpus, sem material a mais. Os outros
#: dois (``avaliar_rubrica`` e ``comparar_ab``) exigem rubrica ativa e respostas
#: de modelo, e por isso só existem no pacote de demonstração e no material que
#: a campanha do P4c gera.
TIPOS_DO_POOL: tuple[str, ...] = (
    "escrever_rubrica",
    "sft_resposta",
    "conversa_modelo",
    "duelo_modelos",
)


def semear_pool(
    conn: sqlite3.Connection, conn_corpus: sqlite3.Connection
) -> dict[str, int]:
    """``seed_pool_tarefas`` tarefas de cada tipo que um prompt cru sustenta.

    **Prompts diferentes para cada tipo**, e não os mesmos: com os mesmos, a fila
    de SFT já traria a rubrica pronta e a oferta de continuação (rubrica → SFT)
    nunca teria o que criar. Separando, a continuação vira uma tarefa nova de
    ``origem='continuacao'`` e o mecanismo fica visível na demonstração.

    Determinístico: os uids do pool saem sempre na mesma ordem, e as duas fatias
    também (``fatias_por_passo``), então rodar em duas máquinas com o mesmo
    corpus gera as mesmas tarefas — e rodar duas vezes na mesma não gera nenhuma.
    """
    quantas = int(_cfg("annotate", "seed_pool_tarefas", default=8))
    uids = poolmod.uids(conn, conn_corpus)
    vazio = dict.fromkeys(TIPOS_DO_POOL, 0)
    if not uids:
        return dict(vazio)

    para_rubrica, para_sft = fatias_por_passo(uids, quantas)
    # As tarefas sobre o corpus REAL vão para o projeto do portfólio: é este o
    # trabalho que um avaliador vai ler.
    projeto_id = projmod.garantir(conn)

    conta = dict(vazio)
    for tipo, lista in (
        ("escrever_rubrica", para_rubrica),
        ("sft_resposta", para_sft),
        # As duas de CONVERSA (P4d) reusam as MESMAS fatias, e não uma terceira:
        # elas não precisam de material nenhum (a resposta é gerada na hora), e
        # o mesmo prompt em estilos diferentes é justamente o desenho desta
        # plataforma — "o mesmo prompt em dois tipos são duas linhas". Uma
        # terceira fatia só empurraria a amostra para outra faixa do pool sem
        # nada em troca.
        ("conversa_modelo", para_rubrica),
        ("duelo_modelos", para_sft),
    ):
        for i, uid in enumerate(lista):
            if _existe(
                conn, "SELECT 1 FROM tarefas WHERE tipo = ? AND prompt_uid = ?", (tipo, uid)
            ):
                continue
            conn.execute(
                "INSERT INTO tarefas (tipo, prompt_uid, origem, projeto_id, prioridade, status) "
                "VALUES (?, ?, 'semente', ?, ?, 'aberta')",
                # Prioridade decrescente na ordem do pool: a fila sai na mesma
                # ordem em duas execuções, o que é o que torna a demonstração
                # reproduzível ("a primeira tarefa é sempre esta").
                (tipo, uid, projeto_id, PRIORIDADE_POOL - i),
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

    ids = projmod.garantir_padroes(conn)
    relatorio["projetos"] = {
        projmod.nome_padrao(): ids["padrao"],
        projmod.nome_demonstracao(): ids["demonstracao"],
    }
    # As diretrizes ANTES das tarefas: uma anotação submetida antes delas
    # existirem gravaria `versao_diretriz = NULL`, que é honesto mas evitável.
    relatorio["diretrizes"] = dirmod.semear(conn)
    relatorio["personas"] = semear_personas(conn)
    relatorio["pacote"] = semear_pacote(conn)
    if conn_corpus is None:
        relatorio["pool"] = dict.fromkeys(TIPOS_DO_POOL, 0)
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
    "QUALIFICACOES",
    "SCHEMA_RUBRICA",
    "TABELAS_FIXTURE",
    "apagar_fixtures",
    "carregar_pacote",
    "fatias_por_passo",
    "semear",
    "semear_pacote",
    "semear_personas",
    "semear_pool",
    "tem_trabalho",
]
