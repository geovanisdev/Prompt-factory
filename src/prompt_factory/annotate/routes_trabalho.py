"""As rotas do fluxo do anotador (P2): fila, catálogo, submissão.

Todas ``def``, nenhuma ``async def`` — regra herdada de ``app/deps.py`` e válida
aqui igual: o ``sqlite3`` é síncrono, e uma consulta dentro do event loop trava
o servidor inteiro.

O DESENHO EM UMA FRASE
======================
O cliente diz **quem é** e **o que quer**; o servidor decide **o que ele
recebe** e **como o trabalho dele é validado**. Nenhuma decisão de autorização,
de tipo de payload ou de recorte de pool sai do que o cliente afirmou.

TRÊS ESCOLHAS QUE PARECEM ARBITRÁRIAS E NÃO SÃO
================================================
1. **Fila vazia é 200, não 404.** Fila vazia é o estado mais comum de uma
   plataforma bem servida. Um 404 faria o cliente tratar o caminho normal como
   falha e mostrar uma tela de erro onde deveria haver uma bifurcação.
2. **O modo livre ignora as vagas.** ``n_anotacoes_alvo`` governa a FILA (quem
   recebe o que sem escolher). Quem foi ao catálogo e escolheu um prompt já
   decidiu; recusar com "essa já tem duas pessoas" transformaria o botão
   "Anotar este" num botão que às vezes falha, sem que a tela pudesse prever.
3. **Prazo vencido não descarta trabalho submetido.** O TTL existe para devolver
   a vaga a QUEM ESTÁ ESPERANDO, não para punir quem demorou. Se a anotação
   chegou, ela é gravada e a resposta avisa que o prazo tinha estourado. Jogar
   fora trabalho humano por causa de um relógio é o pior desfecho possível aqui.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError

from . import catalogo as cat
from . import db as adb
from . import payloads
from . import tarefas as tmod
from .deps import ConAnotacao, ConCorpus, exigir_anotador, exigir_papel
from .models import AbandonarIn, LivreIn, ProximaIn, SubmeterIn

router = APIRouter(tags=["trabalho"])


def _quem(conn: sqlite3.Connection, anotador_id: int) -> sqlite3.Row:
    """404 se o perfil não existe, 403 se não é anotador.

    Só ``anotador``: revisor e admin têm as próprias telas, e deixar os três
    anotarem embaralharia as métricas por papel do painel (P5) — que existem
    justamente para provar que a plataforma sabe quem fez o quê.
    """
    return exigir_papel(exigir_anotador(conn, anotador_id), "anotador")


def _envelope(
    conn: sqlite3.Connection, corpus: sqlite3.Connection, atribuicao_id: int
) -> dict[str, Any]:
    env = tmod.hidratar(conn, corpus, atribuicao_id)
    if env is None:
        # Só chega aqui se o uid sumiu ENTRE o claim e a hidratação (o
        # `pf load-db` roda em segundos). 409, e não 500: não houve defeito,
        # houve corrida com uma recarga do corpus.
        raise HTTPException(
            status_code=409,
            detail=(
                "o prompt desta tarefa não está mais no corpus — ele foi recarregado "
                "agora. Peça a próxima."
            ),
        )
    return env


# ---------------------------------------------------------------------------
# fila (modo locked)
# ---------------------------------------------------------------------------


@router.post("/api/tarefas/proxima", summary="A próxima tarefa do tipo, para mim")
def proxima(corpo: ProximaIn, anot: ConAnotacao, corpus: ConCorpus) -> dict[str, Any]:
    """A trava do modo locked. **200 com ``tarefa: null`` quando a fila está vazia.**

    ``motivo`` sempre vem preenchido — inclusive no caso de sucesso. É ele que a
    tela usa para montar a bifurcação do estado vazio, e uma frase honesta ali
    ("você já anotou todas") vale mais que um contador.
    """
    _quem(anot, corpo.anotador_id)
    atribuicao_id, motivo = tmod.reivindicar(anot, corpus, corpo.anotador_id, corpo.tipo)
    if atribuicao_id is None:
        return {"tarefa": None, "motivo": motivo, "disponiveis": 0}
    env = _envelope(anot, corpus, atribuicao_id)
    return {
        "tarefa": env,
        "motivo": motivo,
        "disponiveis": tmod.contagens_por_tipo(anot, corpo.anotador_id)[corpo.tipo],
    }


@router.get("/api/tarefas/contagens", summary="Quantas tarefas de cada tipo esperam por mim")
def contagens(anot: ConAnotacao, anotador_id: Annotated[int, Query(ge=1)]) -> dict[str, Any]:
    """Os números dos contadores das abas — **pessoais**, não globais.

    Mostrar "12 na fila" para quem já anotou as 12 é a forma mais rápida de
    fazer a tela parecer quebrada. Roda a expiração preguiçosa de passagem: uma
    listagem é exatamente um dos dois momentos em que ela importa.
    """
    _quem(anot, anotador_id)
    return {"contagens": tmod.contagens_por_tipo(anot, anotador_id)}


# ---------------------------------------------------------------------------
# modo livre + catálogo
# ---------------------------------------------------------------------------


def _minha_atribuicao_na_tarefa(
    conn: sqlite3.Connection, tarefa_id: int, anotador_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, status FROM atribuicoes WHERE tarefa_id = ? AND anotador_id = ?",
        (tarefa_id, anotador_id),
    ).fetchone()


@router.post("/api/tarefas/livre", summary="Quero anotar este prompt (modo livre)")
def livre(corpo: LivreIn, anot: ConAnotacao, corpus: ConCorpus) -> dict[str, Any]:
    """Find-or-create de ``(tipo, prompt_uid)`` + atribuição minha.

    **Find-or-create pela chave ``(tipo, prompt_uid)``, não por origem.** Se já
    existe uma tarefa da semente para este par, ela é reusada: criar uma segunda
    tarefa "livre" sobre o mesmo trabalho faria a mesma anotação contar duas
    vezes em toda métrica e quebraria o ``ja_anotei`` do catálogo.

    Atribuição do modo livre nasce **sem prazo** (``expira_em`` NULL): ninguém
    está esperando na fila por uma tarefa que a pessoa escolheu para si.
    """
    _quem(anot, corpo.anotador_id)

    if not tmod.no_pool(anot, corpus, corpo.prompt_uid):
        raise HTTPException(
            status_code=404,
            detail=(
                f"{corpo.prompt_uid} não está no pool desta plataforma. "
                "O catálogo mostra tudo o que pode ser anotado."
            ),
        )

    # O PROMPT PRECISA SUSTENTAR O TIPO. Comparar A/B exige duas respostas de
    # modelo; avaliar exige rubrica ativa e resposta. Um prompt cru do corpus
    # não tem nada disso, e criar a tarefa mesmo assim abriria um workspace de
    # duas colunas que nunca vão existir — o beco que esta plataforma não pode
    # ter. O catálogo já desabilita o botão pela MESMA regra; isto é quem recusa.
    if (faltando := cat.material_faltando(anot, corpo.prompt_uid, corpo.tipo)) is not None:
        raise HTTPException(status_code=409, detail=faltando)

    tmod.expirar_vencidas(anot)
    linha = anot.execute(
        "SELECT id FROM tarefas WHERE tipo = ? AND prompt_uid = ? ORDER BY id LIMIT 1",
        (corpo.tipo, corpo.prompt_uid),
    ).fetchone()

    if linha is None:
        cur = anot.execute(
            "INSERT INTO tarefas (tipo, prompt_uid, origem, status) VALUES (?, ?, ?, 'aberta')",
            (corpo.tipo, corpo.prompt_uid, corpo.origem),
        )
        tarefa_id = int(cur.lastrowid or 0)
        minha = None
    else:
        tarefa_id = int(linha["id"])
        # Tarefa pausada por uid sumido que voltou (uma recarga pode trazê-lo de
        # volta): quem a escolheu de novo no catálogo é prova viva de que ela
        # serve outra vez.
        anot.execute(
            "UPDATE tarefas SET status = 'aberta' WHERE id = ? AND status = 'pausada'",
            (tarefa_id,),
        )
        minha = _minha_atribuicao_na_tarefa(anot, tarefa_id, corpo.anotador_id)

    if minha is not None:
        estado = str(minha["status"])
        if estado in ("submetida", "aprovada"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "você já anotou este prompt neste estilo de tarefa — "
                    "veja em Minhas tarefas"
                ),
            )
        if estado != "em_andamento":
            # `abandonada`/`expirada` REVIVEM a mesma linha, e não criam outra:
            # o UNIQUE(tarefa_id, anotador_id) é a trava anti-repetição, e
            # contornar isso duplicaria a pessoa nas métricas.
            anot.execute(
                "UPDATE atribuicoes SET status = 'em_andamento', expira_em = NULL, "
                "terminada_em = NULL WHERE id = ?",
                (int(minha["id"]),),
            )
        atribuicao_id = int(minha["id"])
        novo = False
    else:
        cur = anot.execute(
            "INSERT INTO atribuicoes (tarefa_id, anotador_id, status, expira_em) "
            "VALUES (?, ?, 'em_andamento', NULL)",
            (tarefa_id, corpo.anotador_id),
        )
        atribuicao_id = int(cur.lastrowid or 0)
        novo = True

    return {"tarefa": _envelope(anot, corpus, atribuicao_id), "novo": novo}


@router.get("/api/catalogo", summary="Buscar no pool para escolher uma tarefa")
def listar_catalogo(
    anot: ConAnotacao,
    corpus: ConCorpus,
    anotador_id: Annotated[int, Query(ge=1)],
    tipo: Annotated[str, Query()],
    q: Annotated[str | None, Query(max_length=500)] = None,
    lang: Annotated[str | None, Query(pattern="^(pt|en)$")] = None,
    fonte: Annotated[str | None, Query(max_length=80)] = None,
    faixa: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
) -> dict[str, Any]:
    """Busca + três filtros, dentro do pool, com ``ja_anotei`` por (tipo, anotador).

    Três filtros e não sete: quem abre o catálogo quer escolher uma tarefa em
    dez segundos, não montar um recorte — recorte é o trabalho da OUTRA
    ferramenta, a de curadoria, e ela já faz isso melhor.
    """
    _quem(anot, anotador_id)
    if tipo not in adb.TIPOS_TAREFA:
        raise HTTPException(status_code=422, detail=f"tipo fora de {list(adb.TIPOS_TAREFA)}")
    if faixa is not None and faixa not in cat.FAIXAS:
        raise HTTPException(status_code=422, detail=f"faixa fora de {list(cat.FAIXAS)}")
    return cat.uma_pagina(
        anot,
        corpus,
        anotador_id=anotador_id,
        tipo=tipo,
        busca=q,
        lang=lang,
        fonte=fonte,
        faixa=faixa,
        pagina=page,
    )


@router.get("/api/prompts/{uid}", summary="Um prompt inteiro (corpus ou pacote)")
def um_prompt(uid: str, anot: ConAnotacao, corpus: ConCorpus) -> dict[str, Any]:
    """404 fora do pool — e o 404 é a regra, não uma checagem de existência.

    Um uid que existe no corpus mas está fora do pool responde 404 de propósito:
    esta app oferece o pool, e vazar o resto por adivinhação de uid daria acesso
    justamente ao que o filtro exclui (NSFW, robô, 1 MB de texto).
    """
    if not tmod.no_pool(anot, corpus, uid):
        raise HTTPException(status_code=404, detail=f"{uid} não está no pool desta plataforma")
    prompt = tmod.resolver_prompt(anot, corpus, uid)
    if prompt is None:  # pragma: no cover - corrida com uma recarga do corpus
        raise HTTPException(status_code=404, detail=f"{uid} não está mais no corpus")
    return prompt


# ---------------------------------------------------------------------------
# submeter / abandonar / minhas
# ---------------------------------------------------------------------------


def _atribuicao_minha(
    conn: sqlite3.Connection, atribuicao_id: int, anotador_id: int
) -> sqlite3.Row:
    linha = conn.execute(
        "SELECT a.id, a.status, a.expira_em, a.anotador_id, a.iniciada_em, "
        "       t.id AS tarefa_id, t.tipo, t.prompt_uid "
        "FROM atribuicoes a JOIN tarefas t ON t.id = a.tarefa_id WHERE a.id = ?",
        (atribuicao_id,),
    ).fetchone()
    if linha is None:
        raise HTTPException(status_code=404, detail=f"atribuição {atribuicao_id} não existe")
    if int(linha["anotador_id"]) != anotador_id:
        # 403 e não 404: o recurso existe, e mentir sobre isso confundiria o
        # diagnóstico da própria demonstração ("sumiu a minha tarefa?").
        raise HTTPException(status_code=403, detail="esta atribuição é de outra pessoa")
    return linha


def _conferir_contra_a_rubrica(
    conn: sqlite3.Connection, uid: str, dados: payloads.AvaliarRubrica
) -> None:
    """A nota tem de caber na escala DAQUELE critério, e todos precisam de nota.

    O Pydantic não pode fazer isto: ele não conhece a rubrica. A checagem mora
    aqui, onde a rubrica está em mãos — e é o mesmo cálculo que o botão da tela
    faz ANTES do clique ("2 de 4 critérios sem nota"). Os dois concordarem é o
    que faz a validação parecer instantânea sem deixar de ser do servidor.
    """
    rubrica = tmod.rubrica_ativa(conn, uid)
    if rubrica is None:
        return
    esperados = {str(c.get("nome")): c for c in rubrica["criterios"] if isinstance(c, dict)}
    if not esperados:
        return
    dadas = {nota.criterio: nota for nota in dados.notas}

    faltam = sorted(set(esperados) - set(dadas))
    if faltam:
        raise HTTPException(
            status_code=422,
            detail=f"{len(faltam)} critério(s) sem nota: {', '.join(faltam)}",
        )
    sobram = sorted(set(dadas) - set(esperados))
    if sobram:
        raise HTTPException(
            status_code=422,
            detail=f"critério que não está na rubrica: {', '.join(sobram)}",
        )
    for nome, criterio in esperados.items():
        escala = criterio.get("escala") or {}
        piso, teto = int(escala.get("min", 1)), int(escala.get("max", 9))
        valor = dadas[nome].nota
        if not piso <= valor <= teto:
            raise HTTPException(
                status_code=422,
                detail=f"'{nome}' aceita de {piso} a {teto}; veio {valor}",
            )


@router.post("/api/atribuicoes/{atribuicao_id}/submeter", summary="Enviar a anotação")
def submeter(
    atribuicao_id: int, corpo: SubmeterIn, anot: ConAnotacao, corpus: ConCorpus
) -> dict[str, Any]:
    """Valida o payload **contra o tipo lido do banco** e grava ``versao = max+1``.

    O tipo NUNCA vem do corpo. Um cliente que declarasse o próprio tipo poderia
    gravar um ``comparar_ab@1`` perfeitamente válido numa tarefa de SFT, e o
    defeito só apareceria no export — quando a pessoa que anotou já foi embora.
    """
    _quem(anot, corpo.anotador_id)
    tmod.expirar_vencidas(anot)
    linha = _atribuicao_minha(anot, atribuicao_id, corpo.anotador_id)

    estado = str(linha["status"])
    if estado in ("submetida", "aprovada"):
        raise HTTPException(
            status_code=409,
            detail="esta anotação já foi enviada — o re-trabalho abre pela devolução do revisor",
        )
    if estado == "abandonada":
        raise HTTPException(status_code=409, detail="você abandonou esta tarefa; peça outra")
    # `expirada` NÃO bloqueia: o trabalho já existe, e descartá-lo por causa do
    # relógio é o pior desfecho possível. Ver o cabeçalho do módulo.
    expirou = estado == "expirada"

    tipo = str(linha["tipo"])
    modelo = payloads.escolher(tipo)
    try:
        dados = modelo.model_validate(corpo.payload)
    except ValidationError as exc:
        # 422 com o CAMINHO do campo, não "payload inválido": a tela precisa
        # apontar para o campo que está errado.
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["body", "payload", *[str(p) for p in erro["loc"]]],
                    "msg": erro["msg"],
                    "type": erro["type"],
                }
                for erro in exc.errors()
            ],
        ) from exc

    if isinstance(dados, payloads.AvaliarRubrica):
        _conferir_contra_a_rubrica(anot, str(linha["prompt_uid"]), dados)

    versao = 1 + int(
        anot.execute(
            "SELECT COALESCE(max(versao), 0) AS v FROM anotacoes WHERE atribuicao_id = ?",
            (atribuicao_id,),
        ).fetchone()["v"]
    )
    cur = anot.execute(
        "INSERT INTO anotacoes (atribuicao_id, versao, payload_schema, payload_json, "
        "                       tempo_ativo_ms, iniciada_em) VALUES (?, ?, ?, ?, ?, ?)",
        (
            atribuicao_id,
            versao,
            payloads.nome_schema(tipo),
            # `ensure_ascii=False`: acentuação vai como caractere, não como ç.
            # O payload é lido por gente no painel do admin e no export.
            json.dumps(dados.model_dump(), ensure_ascii=False),
            int(corpo.tempo_ativo_ms),
            linha["iniciada_em"],
        ),
    )
    anot.execute(
        f"UPDATE atribuicoes SET status = 'submetida', terminada_em = {tmod.SQL_AGORA} "
        "WHERE id = ?",
        (atribuicao_id,),
    )

    resposta: dict[str, Any] = {
        "anotacao_id": int(cur.lastrowid or 0),
        "versao": versao,
        "expirou": expirou,
        "disponiveis": tmod.contagens_por_tipo(anot, corpo.anotador_id)[tipo],
    }
    # A OFERTA DE CONTINUAÇÃO. É o que encadeia as duas abas: quem acabou de
    # escrever a rubrica de um prompt é quem melhor sabe qual resposta ela pede.
    if tipo == "escrever_rubrica":
        resposta["continuacao"] = {
            "tipo": "sft_resposta",
            "prompt_uid": str(linha["prompt_uid"]),
            "origem": "continuacao",
            "convite": "Escrever a resposta de referência deste mesmo prompt",
            "porque": (
                "A rubrica que você acabou de escrever aparece ao lado, como checklist."
            ),
        }
    return resposta


@router.post("/api/atribuicoes/{atribuicao_id}/abandonar", summary="Devolver a tarefa")
def abandonar(atribuicao_id: int, corpo: AbandonarIn, anot: ConAnotacao) -> dict[str, Any]:
    """Devolve a vaga imediatamente, sem esperar o TTL.

    A linha NÃO é apagada: o UNIQUE(tarefa_id, anotador_id) é a trava
    anti-repetição, e apagar deixaria a mesma pessoa pegar a mesma tarefa de
    novo pela fila. Quem abandonou pode voltar a ela pelo catálogo — aí é
    escolha explícita, e a rota do modo livre revive esta mesma linha.
    """
    _quem(anot, corpo.anotador_id)
    linha = _atribuicao_minha(anot, atribuicao_id, corpo.anotador_id)
    if str(linha["status"]) in ("submetida", "aprovada"):
        raise HTTPException(status_code=409, detail="esta anotação já foi enviada")
    anot.execute(
        f"UPDATE atribuicoes SET status = 'abandonada', terminada_em = {tmod.SQL_AGORA} "
        "WHERE id = ?",
        (atribuicao_id,),
    )
    return {"atribuicao_id": atribuicao_id, "status": "abandonada"}


@router.get("/api/atribuicoes", summary="Minhas tarefas")
def minhas(
    anot: ConAnotacao,
    corpus: ConCorpus,
    anotador_id: Annotated[int, Query(ge=1)],
    status: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """O histórico desta pessoa, com as DEVOLVIDAS e o comentário do revisor.

    A devolução é o único item desta lista que exige ação, então ela vem com o
    comentário embutido: obrigar a abrir a tarefa para descobrir o que o revisor
    escreveu é a fricção que faz uma devolução ser ignorada.

    O prompt aparece por um trecho curto (``list_text_chars``), lido do corpus —
    um uid sozinho não diz a ninguém qual tarefa é qual. Uid sumido vira
    ``prompt: null`` com ``disponivel: false``, nunca um 500.
    """
    _quem(anot, anotador_id)
    tmod.expirar_vencidas(anot)
    if status is not None and status not in adb.STATUS_ATRIBUICAO:
        raise HTTPException(status_code=422, detail=f"status fora de {list(adb.STATUS_ATRIBUICAO)}")

    sql = (
        "SELECT a.id, a.status, a.expira_em, a.iniciada_em, a.terminada_em, "
        "       t.id AS tarefa_id, t.tipo, t.origem, t.prompt_uid, "
        "       an.id AS anotacao_id, an.versao, an.status AS anotacao_status, "
        "       r.veredito, r.comentario "
        "FROM atribuicoes a "
        "JOIN tarefas t ON t.id = a.tarefa_id "
        "LEFT JOIN anotacoes an ON an.atribuicao_id = a.id "
        "  AND an.versao = (SELECT max(versao) FROM anotacoes WHERE atribuicao_id = a.id) "
        "LEFT JOIN revisoes r ON r.anotacao_id = an.id "
        "WHERE a.anotador_id = :eu"
    )
    params: dict[str, Any] = {"eu": anotador_id}
    if status:
        sql += " AND a.status = :status"
        params["status"] = status
    sql += " ORDER BY a.id DESC LIMIT :limite"
    params["limite"] = 200

    corte = cat.texto_da_lista()
    itens: list[dict[str, Any]] = []
    for linha in anot.execute(sql, params):
        uid = str(linha["prompt_uid"])
        prompt = tmod.resolver_prompt(anot, corpus, uid)
        itens.append(
            {
                "atribuicao_id": int(linha["id"]),
                "status": str(linha["status"]),
                "expira_em": linha["expira_em"],
                "iniciada_em": linha["iniciada_em"],
                "terminada_em": linha["terminada_em"],
                "tarefa_id": int(linha["tarefa_id"]),
                "tipo": str(linha["tipo"]),
                "origem": str(linha["origem"]),
                "prompt_uid": uid,
                "disponivel": prompt is not None,
                "prompt": None if prompt is None else prompt["text"][:corte],
                "fonte": None if prompt is None else prompt["fonte"],
                "anotacao_id": None if linha["anotacao_id"] is None else int(linha["anotacao_id"]),
                "versao": None if linha["versao"] is None else int(linha["versao"]),
                "anotacao_status": linha["anotacao_status"],
                "veredito": linha["veredito"],
                "comentario_revisor": linha["comentario"],
            }
        )
    return {"items": itens, "total": len(itens)}


@router.get("/api/atribuicoes/{atribuicao_id}", summary="Reabrir uma tarefa minha")
def uma_atribuicao(
    atribuicao_id: int,
    anot: ConAnotacao,
    corpus: ConCorpus,
    anotador_id: Annotated[int, Query(ge=1)],
) -> dict[str, Any]:
    """O mesmo envelope da fila — é o que faz "continuar de onde parei" funcionar.

    E é também o caminho do re-trabalho (P3): o envelope já traz
    ``versao_anterior`` com o payload e o comentário do revisor.
    """
    _quem(anot, anotador_id)
    _atribuicao_minha(anot, atribuicao_id, anotador_id)
    return {"tarefa": _envelope(anot, corpus, atribuicao_id)}


__all__ = ["router"]
