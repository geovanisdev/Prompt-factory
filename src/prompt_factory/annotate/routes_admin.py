"""P5a — o painel do admin: as MÉTRICAS e a geração de tarefas em lote.

Duas coisas que uma plataforma de anotação precisa ter e que, até aqui, só
existiam do lado de fora: um lugar onde os números do QC aparecem sem abrir um
terminal, e um botão que enche a fila sem `pf annotate seed --force`.

AS MÉTRICAS SÃO AS DO ARTEFATO ENTREGUE, E ISSO É O PONTO
=========================================================
``/api/admin/metricas`` não calcula nada de novo: ele chama
``entrega.coletar()`` e passa o resultado pelas MESMAS funções que escrevem o
``quality-report`` do ``pf annotate export`` (``panorama``,
``calibracao_do_revisor``, ``triagem_deixou_passar``). Um painel com contas
próprias divergiria do relatório entregue na primeira mudança — e a divergência
apareceria como "a tela dizia 12 e o cliente recebeu 9", que é o pior lugar
possível para descobrir que havia duas implementações da mesma pergunta.

Duas medidas nascem aqui porque não existem no artefato: ``por_anotador``
(volume, mediana de tempo ativo e taxa de re-trabalho por pessoa) e ``duelos``
(win-rate por modelo e o viés de posição A/B). As duas saem dos itens que
``coletar`` já devolveu — nenhuma segunda consulta.

A terceira, ``concordancia``, é a única que paga uma consulta própria, e paga
por uma razão de MEDIDA e não de arquitetura: ela precisa do payload **como
submetido** e de uma população diferente (tudo que foi submetido, inclusive o que
a triagem devolveu). Ver ``concordancia.py`` — é lá que mora o motivo de o
instrumento não ser igualdade exata.

O TETO, DECLARADO
=================
``entrega.coletar()`` é **em memória, numa passagem** (ver a docstring dele:
"o volume aqui é o trabalho de um punhado de pessoas"). Este painel herda esse
teto de propósito: a alternativa seria uma segunda camada de agregação em SQL,
que é exatamente a segunda implementação que o parágrafo acima proíbe. Quando o
volume exigir, o lugar de paginar continua sendo ``coletar``.

A GERAÇÃO EM LOTE: PRÉVIA E CRIAÇÃO PARTEM DO MESMO PLANO
==========================================================
``_planejar`` é **só leitura** e serve às duas rotas. A prévia mostra quantas
tarefas sairiam, quantas foram puladas e por quê, e uma amostra do que entraria;
o POST cria exatamente aquilo. Duas funções produziriam uma prévia que promete
um número e uma criação que entrega outro — e ninguém confere prévia depois.

Três coisas o plano recusa, e cada uma tem contador próprio na resposta:

* **fora do filtro** — o uid do pool não casa ``lang``/``fonte``;
* **tarefa aberta já existe** para o par ``(tipo, prompt_uid)``. Sem isto, dois
  cliques no mesmo lote dobrariam a fila com o mesmo trabalho e o ``ja_anotei``
  do catálogo passaria a mentir;
* **falta material** — ``avaliar_rubrica`` exige rubrica ativa **e** uma
  resposta, ``comparar_ab`` exige duas. Quem decide é ``catalogo.marcar_material``,
  a MESMA função que desabilita o botão do catálogo: uma segunda regra aqui
  criaria tarefas que abrem numa tela vazia.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from . import catalogo as cat
from . import concordancia as conmod
from . import db as adb
from . import entrega as entmod
from . import eventos as evmod
from . import geracao as germod
from . import pool as poolmod
from . import projetos as projmod
from . import solo as solomod
from . import tarefas as tmod
from .deps import ConAnotacao, ConCorpus, exigir_anotador, exigir_papel
from .models import GerarTarefasIn

router = APIRouter(tags=["admin"])

#: **Só o admin.** O painel mostra a calibração de quem revisa e a taxa de
#: re-trabalho por pessoa: são números sobre o trabalho dos outros, e quem os lê
#: é quem responde por eles. (No modo solo a porta abre, como em toda rota desta
#: plataforma — ver ``deps.exigir_papel``.)
PAPEIS_ADMIN: tuple[str, ...] = ("admin",)

#: Os dois tipos que precisam de material pronto no prompt. A regra de quem
#: sustenta o quê é de ``catalogo.marcar_material``; esta tupla só diz QUANDO
#: perguntar a ela, para não pagar duas consultas nos quatro tipos que sempre dão.
TIPOS_COM_MATERIAL: tuple[str, ...] = ("avaliar_rubrica", "comparar_ab")

#: Quantos prompts a prévia mostra por extenso. Dez é o bastante para reconhecer
#: "isto é o corpus que eu esperava" e curto o bastante para não virar uma
#: segunda listagem dentro de um formulário.
AMOSTRA_PREVIA = 10


def _quem(conn: sqlite3.Connection, admin_id: int) -> sqlite3.Row:
    return exigir_papel(exigir_anotador(conn, admin_id), *PAPEIS_ADMIN)


# ---------------------------------------------------------------------------
# métricas
# ---------------------------------------------------------------------------


def _mediana(valores: list[int]) -> int:
    """Mediana simples dos tempos > 0. Zero fora, e não zero incluído.

    ``tempo_ativo_ms = 0`` é "não medido" (é o default da coluna e o que uma
    sintética grava), não "instantâneo". Incluí-lo puxaria a mediana da equipe
    para baixo em proporção à quantidade de material gerado — que é justamente o
    material que ninguém cronometrou. Mesma regra de ``entrega.panorama``.
    """
    vivos = sorted(v for v in valores if v > 0)
    return vivos[len(vivos) // 2] if vivos else 0


def _por_anotador(itens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Volume, mediana de tempo ativo e re-trabalho, por pessoa.

    ``retrabalho`` conta anotações com ``versao > 1``: elas foram devolvidas na
    triagem e **re-submetidas**. É um proxy, e o proxy é honesto num sentido só —
    ele nunca infla: um item devolvido que ainda não voltou não aparece aqui
    (ele não está entregue, então não está nesta lista). O número é "quanto do
    trabalho ENTREGUE precisou de uma segunda passagem", que é a pergunta que um
    lead de anotação faz.

    ``corrigidas`` é a outra metade e mede coisa diferente: o item passou na
    triagem e mesmo assim o Rate and Review mexeu nele.
    """
    grupos: dict[int, dict[str, Any]] = {}
    for item in itens:
        quem = int(item["anotador_id"])
        g = grupos.setdefault(
            quem,
            {
                "anotador_id": quem,
                "anotador": item["anotador"],
                "n": 0,
                "tempos": [],
                "retrabalho": 0,
                "corrigidas": 0,
                "autorrevisadas": 0,
            },
        )
        g["n"] += 1
        g["tempos"].append(int(item["tempo_ativo_ms"]))
        if int(item["versao"]) > 1:
            g["retrabalho"] += 1
        if item["corrigido"]:
            g["corrigidas"] += 1
        if (item["avaliacao"] or {}).get("autorrevisao"):
            g["autorrevisadas"] += 1

    saida = []
    for g in grupos.values():
        tempos = g.pop("tempos")
        g["mediana_tempo_ativo_ms"] = _mediana(tempos)
        g["taxa_retrabalho"] = round(g["retrabalho"] / g["n"], 4) if g["n"] else None
        saida.append(g)
    saida.sort(key=lambda g: (-g["n"], str(g["anotador"])))
    return saida


def _duelos(itens: list[dict[str, Any]]) -> dict[str, Any]:
    """Win-rate por modelo, viés de posição e o A/B de turno único.

    Tudo sai do ``payload_final`` — nenhuma consulta a ``turnos_conversa``. É o
    payload que carrega o dado ENTREGUE (o corrigido, quando a passagem 2
    corrigiu), e uma segunda leitura pela tabela de turnos mediria a conversa
    como ela foi gerada em vez de como ela foi julgada.

    ``por_rotulo`` é o número que quase nenhuma plataforma mostra: quantas vezes
    o lado **A** venceu, independentemente de quem era. Se ele fugir de 50% com
    volume, o que está sendo medido é a posição na tela e não a resposta.
    """
    por_rotulo: Counter[str] = Counter()
    rodadas_por_modelo: Counter[str] = Counter()
    vitorias_por_modelo: Counter[str] = Counter()
    por_preferencia: Counter[str] = Counter()
    n_duelos = 0
    n_rodadas = 0
    n_ab = 0

    for item in itens:
        payload = item["payload_final"] or {}
        if item["tipo"] == "comparar_ab":
            n_ab += 1
            por_preferencia[str(payload.get("preferencia") or "?")] += 1
            continue
        if item["tipo"] != "duelo_modelos":
            continue
        rodadas = [r for r in (payload.get("rodadas") or []) if isinstance(r, dict)]
        if rodadas:
            n_duelos += 1
        for rodada in rodadas:
            vencedora = str(rodada.get("vencedora") or "")
            if vencedora not in adb.ROTULOS_DUELO:
                continue
            n_rodadas += 1
            por_rotulo[vencedora] += 1
            for resposta in rodada.get("respostas") or []:
                if not isinstance(resposta, dict):  # pragma: no cover - validado
                    continue
                # `modelo` pode vir vazio: o rótulo é CEGO para quem anota, e o
                # nome só é gravado pelo servidor. Um `""` viraria uma linha
                # anônima com win-rate — pior que não existir.
                modelo = str(resposta.get("modelo") or "").strip()
                if not modelo:
                    continue
                rodadas_por_modelo[modelo] += 1
                if str(resposta.get("rotulo") or "") == vencedora:
                    vitorias_por_modelo[modelo] += 1

    return {
        "n_duelos": n_duelos,
        "n_rodadas": n_rodadas,
        "por_rotulo": dict(sorted(por_rotulo.items())),
        "por_modelo": [
            {
                "modelo": modelo,
                "rodadas": total,
                "vitorias": vitorias_por_modelo[modelo],
                "win_rate": round(vitorias_por_modelo[modelo] / total, 4) if total else None,
            }
            for modelo, total in rodadas_por_modelo.most_common()
        ],
        "ab": {"n": n_ab, "por_preferencia": dict(sorted(por_preferencia.items()))},
    }


@router.get("/api/admin/metricas", summary="O painel de QC do admin")
def metricas(
    anot: ConAnotacao,
    corpus: ConCorpus,
    admin_id: Annotated[int, Query(ge=1)],
    projeto: Annotated[str | None, Query(max_length=120)] = None,
) -> dict[str, Any]:
    """Os mesmos números do relatório de qualidade entregue, mais dois cortes.

    ``incluir_sinteticas=True`` e ``incluir_pendentes=True`` porque este é um
    painel de PROVA e não um artefato de dado: a calibração do revisor só existe
    por causa das sintéticas, e esconder o que passou na triagem e ainda não foi
    avaliado tiraria da tela justamente a fila que o admin veio olhar. Cada uma
    das duas populações sai contada à parte (``composicao``, ``panorama``), e a
    política que as mantém fora dos exports continua sendo do ``entrega.py``.

    ``projeto`` é o NOME (é o que ``coletar`` filtra e o que o seletor da barra
    mostra), não o id.
    """
    _quem(anot, admin_id)
    itens = entmod.coletar(
        anot,
        corpus,
        incluir_sinteticas=True,
        incluir_pendentes=True,
        projeto=projeto,
    )
    return {
        "filtros": {"projeto": projeto},
        "n_itens": len(itens),
        "modo_solo": solomod.ligado(),
        "statuses": list(entmod.STATUS_ENTREGUE) + list(entmod.STATUS_PARCIAL),
        "composicao": germod.composicao(anot),
        "cobertura_pool": germod.cobertura_do_pool(anot),
        # AS TRÊS DO ARTEFATO. Mesma fonte, mesma conta, mesmo número.
        "panorama": entmod.panorama(itens),
        "calibracao_revisor": entmod.calibracao_do_revisor(itens),
        "vazamento_triagem": entmod.triagem_deixou_passar(itens),
        # E as três que só existem aqui. A concordância tem POPULAÇÃO PRÓPRIA e
        # por isso não sai de `itens`: ela mede o que os anotadores submeteram,
        # inclusive o que a triagem ainda não viu e o que ela devolveu, e mede
        # pelo payload COMO SUBMETIDO. `coletar` entrega o corrigido em
        # `payload_final` — usá-lo mediria o quanto o revisor convergiu as duas
        # pessoas, que é outra pergunta e de resposta sempre lisonjeira.
        "concordancia": conmod.relatorio(anot, projeto),
        "por_anotador": _por_anotador(itens),
        "duelos": _duelos(itens),
    }


# ---------------------------------------------------------------------------
# gerar tarefas em lote
# ---------------------------------------------------------------------------


def _uids_no_filtro(
    conn_corpus: sqlite3.Connection,
    uids: list[str],
    lang: str | None,
    fonte: str | None,
) -> list[str]:
    """Os uids do pool que casam ``lang``/``fonte``, **na ordem do pool**.

    A ordem importa: ela é determinística (``pool._uids_fallback`` amostra com
    passo constante sobre o corpus inteiro) e é ela que faz duas gerações
    seguidas do mesmo tipo pegarem prompts diferentes em vez de brigar pelos
    mesmos. Filtrar com o resultado do SQL na ordem do SQL devolveria os uids
    agrupados por fonte, que é exatamente o defeito que o passo constante do
    pool existe para evitar.

    Nomes de coluna saem de literais daqui; o que o usuário manda vira parâmetro
    ligado. Mesma disciplina de ``catalogo._condicoes``.
    """
    if not uids:
        return []
    marcas, params = cat._marcas("u", uids)
    cond = [f"uid IN ({marcas})"]
    if lang:
        cond.append("lang = :lang")
        params["lang"] = lang
    if fonte:
        cond.append("source = :fonte")
        params["fonte"] = fonte
    achados = {
        str(linha["uid"])
        for linha in conn_corpus.execute(
            "SELECT uid FROM prompts WHERE " + " AND ".join(cond), params
        )
    }
    return [uid for uid in uids if uid in achados]


def _com_tarefa_aberta(conn: sqlite3.Connection, tipo: str) -> set[str]:
    """Os prompts que já têm tarefa ABERTA deste tipo.

    Aberta, e não "qualquer tarefa": um prompt cuja tarefa foi concluída pode
    receber outra (é assim que uma segunda rodada de anotação acontece). O que
    não pode é duplicar a MESMA vaga esperando na fila.
    """
    return {
        str(linha["prompt_uid"])
        for linha in conn.execute(
            "SELECT DISTINCT prompt_uid FROM tarefas WHERE tipo = ? AND status = 'aberta'",
            (tipo,),
        )
    }


def _planejar(
    conn: sqlite3.Connection, conn_corpus: sqlite3.Connection, corpo: GerarTarefasIn
) -> dict[str, Any]:
    """O plano das duas rotas. **Nada aqui escreve no banco.**"""
    uids = tmod.uids_do_pool(conn, conn_corpus)
    no_filtro = _uids_no_filtro(conn_corpus, uids, corpo.lang, corpo.fonte)

    abertas = _com_tarefa_aberta(conn, corpo.tipo)
    livres = [uid for uid in no_filtro if uid not in abertas]

    com_material = livres
    if corpo.tipo in TIPOS_COM_MATERIAL:
        marcados: list[dict[str, Any]] = [{"uid": uid} for uid in livres]
        cat.marcar_material(conn, marcados)
        com_material = [i["uid"] for i in marcados if corpo.tipo in i["pode"]]

    return {
        "escolhidos": com_material[: corpo.n],
        "n_pool": len(uids),
        "fora_filtro": len(uids) - len(no_filtro),
        "puladas_existentes": len(no_filtro) - len(livres),
        "puladas_material": len(livres) - len(com_material),
    }


@router.get("/api/admin/tarefas/previa", summary="O que a geração criaria (não escreve)")
def previa(
    anot: ConAnotacao,
    corpus: ConCorpus,
    corpo: Annotated[GerarTarefasIn, Query()],
) -> dict[str, Any]:
    """A mesma conta do POST, sem um INSERT.

    O modelo inteiro vem por ``Query()`` de propósito: um segundo modelo "só
    para a prévia" divergiria do de criação exatamente nos limites (``n``,
    ``prioridade``), e a prévia passaria a estimar sob regras que o POST não
    aplica.
    """
    _quem(anot, corpo.admin_id)
    plano = _planejar(anot, corpus, corpo)
    escolhidos = plano["escolhidos"]

    corte = cat.texto_da_lista()
    amostra = []
    for uid in escolhidos[:AMOSTRA_PREVIA]:
        prompt = tmod.resolver_prompt(anot, corpus, uid)
        if prompt is None:  # pragma: no cover - o uid veio do pool, existe
            continue
        amostra.append(
            {
                "uid": uid,
                "trecho": prompt["text"][:corte],
                "lang": prompt["lang"],
                "fonte": prompt["fonte"],
                "licenca": prompt["licenca"],
            }
        )
    return {
        "tipo": corpo.tipo,
        "filtros": {"lang": corpo.lang, "fonte": corpo.fonte},
        "n_pedidas": corpo.n,
        "criaria": len(escolhidos),
        "puladas_material": plano["puladas_material"],
        "puladas_existentes": plano["puladas_existentes"],
        "fora_filtro": plano["fora_filtro"],
        "n_pool": plano["n_pool"],
        "cobertura_pool": germod.cobertura_do_pool(anot),
        "amostra": amostra,
    }


@router.post("/api/admin/tarefas", status_code=201, summary="Gerar tarefas em lote")
def gerar(anot: ConAnotacao, corpus: ConCorpus, corpo: GerarTarefasIn) -> dict[str, Any]:
    """Cria as tarefas do plano, numa transação, com o evento junto.

    O ``INSERT ... SELECT ... WHERE NOT EXISTS`` repete, DENTRO da transação, a
    regra que o plano já aplicou fora dela. Não é redundância decorativa: entre
    o plano e o commit alguém pode ter escolhido o mesmo prompt no catálogo (o
    modo livre cria tarefa), e o plano teria decidido sobre um mundo que mudou.
    O ``rowcount`` é quem diz o que de fato entrou.

    O evento é gravado na MESMA transação (regra de ``eventos.py``): um INSERT
    depois do COMMIT some quando o commit falha, e a trilha passa a mentir
    justamente no caso interessante.
    """
    admin = _quem(anot, corpo.admin_id)
    if corpo.projeto_id is not None and not projmod.existe(anot, corpo.projeto_id):
        raise HTTPException(status_code=404, detail=f"projeto {corpo.projeto_id} não existe")

    plano = _planejar(anot, corpus, corpo)
    escolhidos = plano["escolhidos"]
    # Fora da transação, e antes dela: é idempotente (ON CONFLICT DO NOTHING) e
    # cria o `Portfólio` num banco que ainda não o tem.
    projeto_id = corpo.projeto_id or projmod.garantir(anot)

    # O BUILD DO POOL, e não NULL. As tarefas do seed nascem com NULL porque
    # nasceram antes de o pool ser materializado — verdade histórica que não se
    # conserta por baixo. As geradas aqui saem de um pool que tem build id, e
    # gravá-lo é o que permite dizer, meses depois, sobre qual corpus esta fila
    # foi montada. Quem separa as duas populações é `origem`.
    build = adb.get_meta(anot, poolmod.CHAVE_BUILD) or None

    anot.execute("BEGIN IMMEDIATE")
    try:
        ids: list[int] = []
        for uid in escolhidos:
            cur = anot.execute(
                "INSERT INTO tarefas (tipo, prompt_uid, origem, projeto_id, "
                "                     n_anotacoes_alvo, prioridade, status, db_build_id) "
                "SELECT :tipo, :uid, 'admin', :projeto, :alvo, :prio, 'aberta', :build "
                "WHERE NOT EXISTS (SELECT 1 FROM tarefas t "
                "                  WHERE t.tipo = :tipo AND t.prompt_uid = :uid "
                "                    AND t.status = 'aberta')",
                {
                    "tipo": corpo.tipo,
                    "uid": uid,
                    "projeto": projeto_id,
                    "alvo": corpo.n_anotacoes_alvo,
                    "prio": corpo.prioridade,
                    "build": build,
                },
            )
            if cur.rowcount:
                ids.append(int(cur.lastrowid or 0))
        evmod.registrar(
            anot,
            acao="tarefas_geradas",
            entidade="banco",
            ator_id=corpo.admin_id,
            admin=str(admin["nome"]),
            tipo=corpo.tipo,
            n_pedidas=corpo.n,
            n_criadas=len(ids),
            ids=ids,
            lang=corpo.lang,
            fonte=corpo.fonte,
            n_anotacoes_alvo=corpo.n_anotacoes_alvo,
            prioridade=corpo.prioridade,
            projeto_id=projeto_id,
            db_build_id=build,
            puladas_material=plano["puladas_material"],
            puladas_existentes=plano["puladas_existentes"],
            fora_filtro=plano["fora_filtro"],
        )
        anot.execute("COMMIT")
    except Exception:
        anot.execute("ROLLBACK")
        raise

    return {
        "tipo": corpo.tipo,
        "criadas": len(ids),
        "ids": ids,
        "projeto_id": projeto_id,
        "n_anotacoes_alvo": corpo.n_anotacoes_alvo,
        "prioridade": corpo.prioridade,
        "puladas_material": plano["puladas_material"],
        "puladas_existentes": plano["puladas_existentes"],
        "fora_filtro": plano["fora_filtro"],
        "na_fila_agora": int(
            anot.execute(
                "SELECT count(*) AS n FROM tarefas WHERE tipo = ? AND status = 'aberta'",
                (corpo.tipo,),
            ).fetchone()["n"]
        ),
    }


# ---------------------------------------------------------------------------
# a trilha de auditoria
# ---------------------------------------------------------------------------


@router.get("/api/admin/eventos", summary="A trilha de auditoria de UMA coisa")
def eventos(
    anot: ConAnotacao,
    admin_id: Annotated[int, Query(ge=1)],
    entidade: Annotated[str, Query(max_length=40)],
    entidade_id: Annotated[int, Query(ge=1)],
) -> dict[str, Any]:
    """``eventos.da_entidade`` servido — o chamador que o P3a prometeu.

    ``entidade`` é validado contra ``eventos.ENTIDADES`` e o 422 traz a lista.
    Sem isso, um nome errado devolveria ``{"items": []}`` — uma trilha vazia,
    que é indistinguível de "nada aconteceu com este item" e é a leitura mais
    perigosa que uma auditoria pode induzir.
    """
    _quem(anot, admin_id)
    if entidade not in evmod.ENTIDADES:
        raise HTTPException(
            status_code=422,
            detail=f"entidade fora de {list(evmod.ENTIDADES)}: {entidade!r}",
        )
    itens = evmod.da_entidade(anot, entidade, entidade_id)
    return {"items": itens, "total": len(itens)}


__all__ = ["AMOSTRA_PREVIA", "PAPEIS_ADMIN", "TIPOS_COM_MATERIAL", "router"]
