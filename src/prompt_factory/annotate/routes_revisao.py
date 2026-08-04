"""A TRIAGEM (passagem 1 da revisão) — a aba "Anotações" do revisor.

Aprova ou devolve. Sem escore, sem correção no lugar: essas são a passagem 2
("Rate and Review", P3b). **Esta é a única passagem que devolve trabalho ao
anotador**, e é isso que a torna o fecho do ciclo: anotar → devolver → corrigir
→ aprovar, com ``versao = 2`` gravada.

TRÊS REGRAS QUE PARECEM DETALHE E SÃO A MECÂNICA
=================================================
1. **O revisor não vê as próprias anotações** — por padrão. Não é etiqueta: com
   uma pessoa só revisando o que ela mesma escreveu, o número de aprovação do
   painel do admin deixa de medir qualquer coisa. O ``WHERE`` exclui, e há teste.

   **Exceção declarada: o MODO SOLO** (``[annotate] permitir_autorrevisao``).
   Uma pessoa montando o próprio portfólio atua nos três papéis, e a regra de
   equipe travaria o caso de uso principal. Ligado, o revisor vê as próprias —
   e a plataforma **diz isso na tela** (faixa permanente na fila) e **grava**
   ``revisoes.autorrevisao = 1``. A regra não é afrouxada em silêncio: ela é
   substituída por uma que se declara, porque um QC que se esconde não é QC.
2. **Devolver exige comentário** — no Pydantic, na rota e no CHECK do DDL. O
   comentário volta para o anotador junto com o formulário preenchido; devolver
   sem ele devolve trabalho sem devolver informação, e o re-trabalho sai igual
   ao primeiro.
3. **Devolver REABRE a atribuição** (``em_andamento``) em vez de criar outra. O
   ``UNIQUE(tarefa_id, anotador_id)`` é a trava anti-repetição: uma segunda
   atribuição faria o mesmo trabalho contar duas vezes em toda métrica. Quem
   versiona é ``anotacoes.versao``, e o ``submeter`` já grava ``max + 1``
   sozinho.

APROVAR MATERIALIZA A RUBRICA
=============================
Uma rubrica proposta na aba escrever-rubrica vive dentro do ``payload_json`` da
anotação até alguém aprová-la. Na aprovação ela vira linha em ``rubricas`` com
``origem='anotacao'`` — e passa a ser o instrumento com que OUTRAS pessoas
avaliam aquele prompt. É o que fecha o encadeamento entre as quatro abas, e é o
motivo de a triagem existir antes do Rate and Review.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from . import catalogo as cat
from . import db as adb
from . import eventos as evmod
from . import seed as seedmod
from . import solo as solomod
from . import tarefas as tmod
from .deps import ConAnotacao, ConCorpus, exigir_anotador, exigir_papel
from .models import TriarIn

router = APIRouter(tags=["revisão"])

#: Quem pode triar. O admin também, de propósito: numa demonstração com seis
#: personas, um revisor que anotou tudo travaria a fila, e a saída do estado
#: vazio da tela do anotador é literalmente "entrar como administrador".
PAPEIS_TRIAGEM: tuple[str, ...] = ("revisor", "admin")


def _quem(conn: sqlite3.Connection, revisor_id: int) -> sqlite3.Row:
    return exigir_papel(exigir_anotador(conn, revisor_id), *PAPEIS_TRIAGEM)


def _recusar_propria(anotador_id: int, revisor_id: int) -> None:
    """403 na própria anotação — a não ser no modo solo, que é declarado."""
    if anotador_id == revisor_id and not solomod.ligado():
        raise HTTPException(
            status_code=403,
            detail=(
                "esta anotação é sua — quem revisa nunca é quem anotou. "
                "Para montar um portfólio sozinho, ligue `[annotate] "
                "permitir_autorrevisao` no settings.toml: a plataforma passa a "
                "permitir, avisa na tela e marca cada revisão como autorrevisão."
            ),
        )


#: A fila da triagem, sem o texto do prompt (que vem do outro banco). O
#: ``:eu`` sai do WHERE no modo solo — ver ``_sql_fila``.
_SQL_FILA_BASE = (
    "SELECT an.id, an.versao, an.submetida_em, an.payload_schema, an.status, "
    "       a.id AS atribuicao_id, a.anotador_id, au.nome AS anotador, "
    "       t.id AS tarefa_id, t.tipo, t.origem, t.prompt_uid, "
    "       t.projeto_id, pr.nome AS projeto "
    "FROM anotacoes an "
    "JOIN atribuicoes a ON a.id = an.atribuicao_id "
    "JOIN anotadores au ON au.id = a.anotador_id "
    "JOIN tarefas t ON t.id = a.tarefa_id "
    "LEFT JOIN projetos pr ON pr.id = t.projeto_id "
    "WHERE an.status = 'pendente_triagem'"
)


def _sql_fila(*, com_projeto: bool) -> str:
    sql = _SQL_FILA_BASE
    if not solomod.ligado():
        sql += " AND a.anotador_id <> :eu"
    if com_projeto:
        sql += " AND t.projeto_id = :projeto"
    # CRONOLÓGICA: quem esperou mais é atendido primeiro. Ordenar por prioridade
    # da tarefa deixaria uma submissão de terça esperando atrás de uma de hoje.
    return sql + " ORDER BY an.id"


@router.get("/api/revisao/fila", summary="A fila da triagem")
def fila(
    anot: ConAnotacao,
    corpus: ConCorpus,
    revisor_id: Annotated[int, Query(ge=1)],
    projeto_id: Annotated[int | None, Query(ge=1)] = None,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
) -> dict[str, Any]:
    """Ordem cronológica, com um trecho do prompt para dar a cada item um nome.

    O trecho vem do corpus (``list_text_chars``); um uid sozinho não diz a
    ninguém qual item é qual, e a fila do revisor é justamente onde se escolhe
    pelo conteúdo. Uid sumido vira ``disponivel: false`` — a triagem continua
    possível, porque o que se julga é o payload, não o prompt.

    ``modo_solo`` sai na resposta **sempre**, ligado ou desligado: é dele que a
    tela monta a faixa permanente. Um modo que muda a regra de QC e não aparece
    seria exatamente a mentira que este projeto não conta.
    """
    _quem(anot, revisor_id)
    tam = cat.page_size()
    params: dict[str, Any] = {"eu": revisor_id}
    if projeto_id is not None:
        params["projeto"] = projeto_id
    linhas = anot.execute(_sql_fila(com_projeto=projeto_id is not None), params).fetchall()
    total = len(linhas)
    pagina = max(1, int(page))
    recorte = linhas[(pagina - 1) * tam : pagina * tam]

    corte = cat.texto_da_lista()
    itens: list[dict[str, Any]] = []
    for linha in recorte:
        uid = str(linha["prompt_uid"])
        prompt = tmod.resolver_prompt(anot, corpus, uid)
        itens.append(
            {
                "anotacao_id": int(linha["id"]),
                "versao": int(linha["versao"]),
                "submetida_em": linha["submetida_em"],
                "tipo": str(linha["tipo"]),
                "origem": str(linha["origem"]),
                "tarefa_id": int(linha["tarefa_id"]),
                "atribuicao_id": int(linha["atribuicao_id"]),
                "anotador_id": int(linha["anotador_id"]),
                "anotador": str(linha["anotador"]),
                # É MINHA? A tela marca cada item no modo solo, e não só a fila
                # inteira: numa fila mista (trabalho meu e de outra pessoa),
                # saber quais são as minhas é o que torna o aviso acionável.
                "minha": int(linha["anotador_id"]) == revisor_id,
                "projeto_id": None if linha["projeto_id"] is None else int(linha["projeto_id"]),
                "projeto": linha["projeto"],
                "prompt_uid": uid,
                "disponivel": prompt is not None,
                "prompt": None if prompt is None else prompt["text"][:corte],
                "fonte": None if prompt is None else prompt["fonte"],
            }
        )
    return {
        "items": itens,
        "total": total,
        "page": pagina,
        "page_size": tam,
        "pages": (total + tam - 1) // tam,
        "modo_solo": solomod.ligado(),
        # As MINHAS, contadas à parte: sem este número, um revisor que anotou e
        # vê a fila vazia acha que a plataforma perdeu o trabalho dele. No modo
        # solo elas não são excluídas — o número vira "quantas destas são suas".
        "minhas_excluidas": 0
        if solomod.ligado()
        else int(
            anot.execute(
                "SELECT count(*) AS n FROM anotacoes an "
                "JOIN atribuicoes a ON a.id = an.atribuicao_id "
                "WHERE an.status = 'pendente_triagem' AND a.anotador_id = ?",
                (revisor_id,),
            ).fetchone()["n"]
        ),
        "minhas_na_fila": sum(1 for i in itens if i["minha"]),
    }


@router.get("/api/revisao/{anotacao_id}", summary="Uma submissão, como o anotador a produziu")
def detalhe(
    anotacao_id: int,
    anot: ConAnotacao,
    corpus: ConCorpus,
    revisor_id: Annotated[int, Query(ge=1)],
) -> dict[str, Any]:
    """O mesmo envelope do anotador + o payload submetido.

    403 na própria anotação, e não 404: o recurso existe, e mentir sobre isso
    confundiria o diagnóstico da própria demonstração.
    """
    _quem(anot, revisor_id)
    env = tmod.hidratar_anotacao(anot, corpus, anotacao_id)
    if env is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"anotação {anotacao_id} não existe, ou o prompt dela não está mais "
                "no corpus (ele foi recarregado)"
            ),
        )
    minha = int(env["anotacao"]["anotador_id"]) == revisor_id
    _recusar_propria(int(env["anotacao"]["anotador_id"]), revisor_id)
    return {"tarefa": env, "minha": minha, "modo_solo": solomod.ligado()}


def _materializar_rubrica(conn: sqlite3.Connection, env: dict[str, Any]) -> int | None:
    """Aprovou uma ``escrever_rubrica``? A rubrica vira instrumento de verdade.

    Idempotente por ``(prompt_uid, anotacao_id)``: triar duas vezes a mesma
    anotação é impossível (``UNIQUE(anotacao_id)`` em ``revisoes``), mas uma
    versão 2 aprovada do MESMO prompt cria uma linha nova — e a mais recente é a
    que ``rubrica_ativa`` devolve, porque ela ordena por ``id DESC``.
    """
    anotacao = env["anotacao"]
    payload = anotacao["payload"]
    if not isinstance(payload, dict) or not payload.get("criterios"):
        return None
    uid = str(env["prompt"]["uid"])
    ja = conn.execute(
        "SELECT id FROM rubricas WHERE prompt_uid = ? AND anotacao_id = ?",
        (uid, int(anotacao["id"])),
    ).fetchone()
    if ja is not None:
        return int(ja["id"])
    cur = conn.execute(
        "INSERT INTO rubricas (prompt_uid, titulo, criterios_json, origem, anotacao_id, status) "
        "VALUES (?, ?, ?, 'anotacao', ?, 'ativa')",
        (
            uid,
            str(payload.get("titulo") or "Rubrica aprovada"),
            json.dumps(
                {
                    "schema": seedmod.SCHEMA_RUBRICA,
                    # CONVERTIDO, não copiado. O payload vem de
                    # `escrever_rubrica@1` (`escala_min`/`rotulo_min`) e o rótulo
                    # acima promete `rubrica@3` (`escala:{min,max,ancoras}`).
                    # Gravar um sob o nome do outro fazia a escala declarada
                    # sumir em silêncio — na tela E na validação do servidor.
                    # Mesma função da leitura, de propósito: duas conversões
                    # divergiriam e a divergência só apareceria numa rubrica.
                    "criterios": [
                        tmod.normalizar_criterio(c) for c in payload["criterios"]
                    ],
                },
                ensure_ascii=False,
            ),
            int(anotacao["id"]),
        ),
    )
    return int(cur.lastrowid or 0)


@router.post("/api/revisao/{anotacao_id}", summary="Aprovar ou devolver (triagem)")
def triar(
    anotacao_id: int, corpo: TriarIn, anot: ConAnotacao, corpus: ConCorpus
) -> dict[str, Any]:
    """``A`` aprova, ``R`` devolve. Uma transição, uma revisão, um evento.

    Tudo numa transação só: o status da anotação, o da atribuição, a linha de
    ``revisoes``, a rubrica materializada e o evento de auditoria. Metade disso
    commitado é um item que a tela mostra aprovado e a fila continua servindo.
    """
    revisor = _quem(anot, corpo.revisor_id)
    env = tmod.hidratar_anotacao(anot, corpus, anotacao_id)
    if env is None:
        raise HTTPException(status_code=404, detail=f"anotação {anotacao_id} não existe")
    anotacao = env["anotacao"]
    autorrevisao = int(anotacao["anotador_id"]) == corpo.revisor_id
    _recusar_propria(int(anotacao["anotador_id"]), corpo.revisor_id)

    atual = str(anotacao["status"])
    destino = "pendente_avaliacao" if corpo.veredito == "aprovada" else "devolvida"
    if destino not in adb.TRANSICOES.get(atual, ()):
        # A máquina de status é DADO (`db.TRANSICOES`), e é ela que recusa —
        # não uma sequência de `if` que diverge dela na primeira mudança.
        raise HTTPException(
            status_code=409,
            detail=(
                f"esta anotação está em {atual!r} e a triagem só age sobre "
                "'pendente_triagem' (alguém já triou esta versão)"
            ),
        )

    anot.execute("BEGIN IMMEDIATE")
    try:
        cur = anot.execute(
            "INSERT INTO revisoes (anotacao_id, revisor_id, veredito, comentario, "
            "                      autorrevisao) VALUES (?, ?, ?, ?, ?)",
            (
                anotacao_id,
                corpo.revisor_id,
                corpo.veredito,
                corpo.comentario,
                # GRAVADO, não deduzido depois: `atribuicoes.anotador_id` pode
                # ser corrigido por um admin, e a derivação passaria a mentir
                # sobre uma revisão que já aconteceu.
                int(autorrevisao),
            ),
        )
        revisao_id = int(cur.lastrowid or 0)
        anot.execute(
            "UPDATE anotacoes SET status = ? WHERE id = ?", (destino, anotacao_id)
        )

        rubrica_id: int | None = None
        if corpo.veredito == "aprovada":
            anot.execute(
                f"UPDATE atribuicoes SET status = 'aprovada', "
                f"terminada_em = COALESCE(terminada_em, {tmod.SQL_AGORA}) WHERE id = ?",
                (int(env["atribuicao_id"]),),
            )
            if str(env["tarefa"]["tipo"]) == "escrever_rubrica":
                rubrica_id = _materializar_rubrica(anot, env)
        else:
            # REABRE A MESMA LINHA, com PRAZO NOVO. Ver a regra 3 do cabeçalho do
            # módulo e o porquê do prazo em `tarefas.SQL_REABRIR`.
            tmod.reabrir(anot, int(env["atribuicao_id"]))
        evmod.registrar(
            anot,
            acao="triagem_aprovada" if corpo.veredito == "aprovada" else "triagem_devolvida",
            entidade="anotacao",
            entidade_id=anotacao_id,
            ator_id=corpo.revisor_id,
            revisor=str(revisor["nome"]),
            versao=int(anotacao["versao"]),
            tipo=str(env["tarefa"]["tipo"]),
            de=atual,
            para=destino,
            comentario=corpo.comentario,
            rubrica_id=rubrica_id,
            autorrevisao=autorrevisao,
        )
        anot.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        anot.execute("ROLLBACK")
        # UNIQUE(anotacao_id): duas abas do revisor triando a mesma linha. É
        # corrida, não defeito — e a resposta certa é dizer que já foi triada.
        raise HTTPException(
            status_code=409,
            detail=f"esta anotação já foi triada por alguém ({exc})",
        ) from exc
    except Exception:
        anot.execute("ROLLBACK")
        raise

    if solomod.ligado():
        restantes = int(
            anot.execute(
                "SELECT count(*) AS n FROM anotacoes WHERE status = 'pendente_triagem'"
            ).fetchone()["n"]
        )
    else:
        restantes = int(
            anot.execute(
                "SELECT count(*) AS n FROM anotacoes an "
                "JOIN atribuicoes a ON a.id = an.atribuicao_id "
                "WHERE an.status = 'pendente_triagem' AND a.anotador_id <> ?",
                (corpo.revisor_id,),
            ).fetchone()["n"]
        )
    return {
        "anotacao_id": anotacao_id,
        "revisao_id": revisao_id,
        "veredito": corpo.veredito,
        "status": destino,
        "rubrica_id": rubrica_id,
        "autorrevisao": autorrevisao,
        "restantes": restantes,
    }


__all__ = ["PAPEIS_TRIAGEM", "router"]
