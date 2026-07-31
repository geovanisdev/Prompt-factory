"""RATE AND REVIEW (passagem 2) e a ESCALAÇÃO — as rotas.

A triagem (``routes_revisao``) decide se o trabalho entra. Aqui se decide quanto
ele vale, e o revisor **conserta no lugar** o que dá para consertar.

QUATRO REGRAS QUE SÃO A MECÂNICA
================================
1. **``devolvida`` não existe nesta passagem.** ``db.TRANSICOES`` não a lista a
   partir de ``pendente_avaliacao``, e a rota consulta a máquina em vez de
   repetir a regra num ``if``. O motivo é de produto: um item que já foi
   aprovado na triagem e volta ao anotador dias depois mede o revisor da
   triagem, não quem anotou — e a métrica de QC que nasce disso ficaria
   embaralhada. Quem conserta na passagem 2 é quem avalia.
2. **``incorrigivel`` é terminal.** Vira ``descartada`` e a atribuição **não é
   reaberta**. É o único desfecho que joga trabalho fora, e é por isso que a
   justificativa é obrigatória.
3. **O diff é do SERVIDOR.** O cliente manda o payload corrigido e os motivos;
   quem decide o que mudou é ``avaliacoes.diferencas``, comparando o payload
   original com o corrigido. Mudança sem motivo é 422; motivo sobre campo que
   não mudou também. Ver o cabeçalho de ``avaliacoes.py``.
4. **O alvo escondido nunca sai daqui.** ``anotacoes.gabarito_avaliacao_json``
   não entra em nenhum SELECT desta rota, exatamente como o ``gabarito_json``
   das tarefas-ouro: o envelope é montado campo a campo por
   ``tarefas.hidratar_anotacao``. Um alvo visível mediria a capacidade de ler
   JSON, não a calibração de quem avalia.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError

from . import avaliacoes as avmod
from . import catalogo as cat
from . import db as adb
from . import eventos as evmod
from . import payloads
from . import solo as solomod
from . import tarefas as tmod
from .deps import ConAnotacao, ConCorpus, exigir_anotador, exigir_papel
from .models import AvaliarIn, DecisaoAdminIn

router = APIRouter(tags=["avaliação"])

#: Quem avalia. O admin também, pela mesma razão da triagem: numa demonstração
#: com seis personas, um revisor que anotou tudo travaria a fila.
PAPEIS_AVALIACAO: tuple[str, ...] = ("revisor", "admin")

#: Quem decide uma escalação. **Só o admin** — se o revisor pudesse decidir a
#: própria escalação, "escalar" não significaria nada.
PAPEIS_ESCALACAO: tuple[str, ...] = ("admin",)


def _quem(conn: sqlite3.Connection, revisor_id: int) -> sqlite3.Row:
    return exigir_papel(exigir_anotador(conn, revisor_id), *PAPEIS_AVALIACAO)


def _recusar_propria(anotador_id: int, revisor_id: int) -> None:
    """403 na própria anotação — a não ser no modo solo, que é declarado."""
    if anotador_id == revisor_id and not solomod.ligado():
        raise HTTPException(
            status_code=403,
            detail=(
                "esta anotação é sua — quem avalia nunca é quem anotou. "
                "Para montar um portfólio sozinho, ligue `[annotate] "
                "permitir_autorrevisao` no settings.toml: a plataforma passa a "
                "permitir, avisa na tela e marca cada avaliação como autorrevisão."
            ),
        )


# ---------------------------------------------------------------------------
# a fila da passagem 2
# ---------------------------------------------------------------------------


@router.get("/api/avaliacao/fila", summary="A fila do Rate and Review")
def fila(
    anot: ConAnotacao,
    corpus: ConCorpus,
    revisor_id: Annotated[int, Query(ge=1)],
    projeto_id: Annotated[int | None, Query(ge=1)] = None,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
) -> dict[str, Any]:
    """O que a triagem aprovou, em ordem cronológica, com quem a aprovou.

    ``triador`` sai em cada item de propósito: a métrica mais interessante deste
    marco — item aprovado na triagem e julgado ``inutilizavel`` aqui — mede o
    revisor da triagem, e ela só existe porque os dois nomes viajam juntos desde
    a fila.
    """
    _quem(anot, revisor_id)
    tam = cat.page_size()
    params: dict[str, Any] = {"eu": revisor_id}
    if projeto_id is not None:
        params["projeto"] = projeto_id
    linhas = anot.execute(
        avmod.sql_fila(com_projeto=projeto_id is not None), params
    ).fetchall()
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
                "minha": int(linha["anotador_id"]) == revisor_id,
                # QUEM APROVOU NA TRIAGEM. É o segundo nome da métrica de QC.
                "triador": linha["triador"],
                "triada_em": linha["triada_em"],
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
        "minhas_excluidas": 0
        if solomod.ligado()
        else avmod.minhas_esperando(anot, revisor_id),
        "minhas_na_fila": sum(1 for i in itens if i["minha"]),
    }


@router.get("/api/avaliacao/{anotacao_id}", summary="Uma submissão aprovada, para avaliar")
def detalhe(
    anotacao_id: int,
    anot: ConAnotacao,
    corpus: ConCorpus,
    revisor_id: Annotated[int, Query(ge=1)],
) -> dict[str, Any]:
    """O MESMO envelope da triagem e do anotador, mais o comentário de quem triou.

    Reconhecimento em vez de memória: o revisor abre a submissão no layout em
    que ela foi produzida, com a mesma rubrica e a mesma ordem — só que agora os
    campos são editáveis.
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
    triagem = anot.execute(
        "SELECT r.veredito, r.comentario, r.criada_em, r.autorrevisao, au.nome AS revisor "
        "FROM revisoes r JOIN anotadores au ON au.id = r.revisor_id "
        "WHERE r.anotacao_id = ?",
        (anotacao_id,),
    ).fetchone()
    return {
        "tarefa": env,
        "minha": minha,
        "modo_solo": solomod.ligado(),
        # A passagem 1 à vista, e não só o resultado dela: "quem aprovou isto, e
        # com que comentário" é a informação que separa "a triagem errou" de "o
        # anotador errou".
        "triagem": None
        if triagem is None
        else {
            "veredito": str(triagem["veredito"]),
            "comentario": triagem["comentario"],
            "revisor": str(triagem["revisor"]),
            "autorrevisao": bool(triagem["autorrevisao"]),
            "criada_em": triagem["criada_em"],
        },
    }


# ---------------------------------------------------------------------------
# registrar a avaliação
# ---------------------------------------------------------------------------


def _validar_corrigido(tipo: str, bruto: dict[str, Any]) -> Any:
    """O payload corrigido passa pelo MESMO modelo que validou a submissão.

    O revisor corrige dentro do contrato: uma correção que viola a escala da
    rubrica ou apaga a justificativa obrigatória do A/B não é correção, é um
    payload que o export não saberia ler.
    """
    modelo = payloads.escolher(tipo)
    try:
        return modelo.model_validate(bruto)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["body", "payload_corrigido", *[str(p) for p in erro["loc"]]],
                    "msg": erro["msg"],
                    "type": erro["type"],
                }
                for erro in exc.errors()
            ],
        ) from exc


def _conferir_diff(
    mudou: list[dict[str, Any]], declarados: list[Any]
) -> dict[str, str]:
    """Os caminhos que MUDARAM têm de ser exatamente os que têm motivo.

    Os dois lados falham, e falham diferente:

    * mudou e não veio motivo → alguém alterou trabalho alheio sem dizer por quê;
    * veio motivo e não mudou → a auditoria descreveria uma correção que não
      houve, o que é pior que não ter auditoria.
    """
    esperados = {d["campo"] for d in mudou}
    dados = {e.campo: e.motivo for e in declarados}
    sem_motivo = sorted(esperados - set(dados))
    if sem_motivo:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{len(sem_motivo)} mudança(s) sem motivo: {', '.join(sem_motivo)} — "
                "cada alteração precisa da própria justificativa curta"
            ),
        )
    inventados = sorted(set(dados) - esperados)
    if inventados:
        raise HTTPException(
            status_code=422,
            detail=(
                f"motivo para campo que não mudou: {', '.join(inventados)} — o diff é "
                "calculado pelo servidor a partir do payload corrigido"
            ),
        )
    return dados


@router.post("/api/avaliacao/{anotacao_id}", summary="Registrar a avaliação (passagem 2)")
def avaliar(
    anotacao_id: int, corpo: AvaliarIn, anot: ConAnotacao, corpus: ConCorpus
) -> dict[str, Any]:
    """Duas escalas, o diff campo a campo e um desfecho. Tudo numa transação.

    Metade disto commitado seria um item medido e não encerrado, ou encerrado
    sem o diff que explica por quê — e a fila continuaria servindo o mesmo item.
    """
    revisor = _quem(anot, corpo.revisor_id)
    env = tmod.hidratar_anotacao(anot, corpus, anotacao_id)
    if env is None:
        raise HTTPException(status_code=404, detail=f"anotação {anotacao_id} não existe")
    anotacao = env["anotacao"]
    autorrevisao = int(anotacao["anotador_id"]) == corpo.revisor_id
    _recusar_propria(int(anotacao["anotador_id"]), corpo.revisor_id)

    atual = str(anotacao["status"])
    destino = avmod.desfecho(corpo.avaliacao_depois)
    if destino not in adb.TRANSICOES.get(atual, ()):
        # A MÁQUINA É DADO. É ela que recusaria um `devolvida` vindo daqui — a
        # tupla de `pendente_avaliacao` não o lista, e nenhuma rota o inventa.
        raise HTTPException(
            status_code=409,
            detail=(
                f"esta anotação está em {atual!r} e o Rate and Review só age sobre "
                f"'pendente_avaliacao' (alguém já avaliou esta versão)"
            ),
        )

    tipo = str(env["tarefa"]["tipo"])
    original = anotacao["payload"]
    corrigido_json: str | None = None
    mudou: list[dict[str, Any]] = []
    if corpo.payload_corrigido is not None:
        dados = _validar_corrigido(tipo, corpo.payload_corrigido)
        # O diff é sobre o payload NORMALIZADO pelo modelo (o mesmo tratamento
        # que a submissão recebeu), e não sobre o dicionário cru: um `"  "` que
        # o validador transforma em `None` não é uma mudança do revisor.
        corrigido = dados.model_dump()
        mudou = avmod.diferencas(original, corrigido)
        if mudou:
            corrigido_json = json.dumps(corrigido, ensure_ascii=False)
    motivos = _conferir_diff(mudou, corpo.edicoes)

    anot.execute("BEGIN IMMEDIATE")
    try:
        cur = anot.execute(
            "INSERT INTO avaliacoes (anotacao_id, revisor_id, avaliacao_antes, "
            "                        avaliacao_depois, justificativa, payload_corrigido_json, "
            "                        autorrevisao, tempo_ativo_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                anotacao_id,
                corpo.revisor_id,
                corpo.avaliacao_antes,
                corpo.avaliacao_depois,
                corpo.justificativa,
                # NULL quando nada mudou — e nesse caso `edicoes_avaliacao`
                # também fica vazia para esta linha. Guardar uma cópia idêntica
                # do payload diria que houve correção.
                corrigido_json,
                int(autorrevisao),
                int(corpo.tempo_ativo_ms),
            ),
        )
        avaliacao_id = int(cur.lastrowid or 0)
        anot.executemany(
            "INSERT INTO edicoes_avaliacao (avaliacao_id, campo, valor_antes, valor_depois, "
            "                               motivo) VALUES (?, ?, ?, ?, ?)",
            [
                (
                    avaliacao_id,
                    d["campo"],
                    # O valor gravado é o do SERVIDOR, não o que o cliente
                    # afirmou ter mostrado: a trilha descreve o payload, não a
                    # tela.
                    avmod.como_texto(d["valor_antes"]),
                    avmod.como_texto(d["valor_depois"]),
                    motivos[d["campo"]],
                )
                for d in mudou
            ],
        )
        anot.execute("UPDATE anotacoes SET status = ? WHERE id = ?", (destino, anotacao_id))
        # A ATRIBUIÇÃO NÃO É TOCADA, e isso é a regra 2 do cabeçalho. Nem para
        # `incorrigivel`: reabri-la devolveria trabalho ao anotador por um
        # caminho que a máquina de status não conhece.
        evmod.registrar(
            anot,
            acao="avaliacao_registrada",
            entidade="anotacao",
            entidade_id=anotacao_id,
            ator_id=corpo.revisor_id,
            avaliacao_id=avaliacao_id,
            revisor=str(revisor["nome"]),
            versao=int(anotacao["versao"]),
            tipo=tipo,
            avaliacao_antes=corpo.avaliacao_antes,
            avaliacao_depois=corpo.avaliacao_depois,
            de=atual,
            para=destino,
            n_edicoes=len(mudou),
            campos=[d["campo"] for d in mudou],
            autorrevisao=autorrevisao,
            tempo_ativo_ms=int(corpo.tempo_ativo_ms),
        )
        anot.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        anot.execute("ROLLBACK")
        # UNIQUE(anotacao_id): duas abas do revisor avaliando a mesma linha.
        raise HTTPException(
            status_code=409, detail=f"esta anotação já foi avaliada por alguém ({exc})"
        ) from exc
    except Exception:
        anot.execute("ROLLBACK")
        raise

    return {
        "anotacao_id": anotacao_id,
        "avaliacao_id": avaliacao_id,
        "avaliacao_antes": corpo.avaliacao_antes,
        "avaliacao_depois": corpo.avaliacao_depois,
        "status": destino,
        "escalada": destino == "escalada",
        "n_edicoes": len(mudou),
        "autorrevisao": autorrevisao,
        "na_fila": _quantas_esperam(anot, corpo.revisor_id),
    }


def _quantas_esperam(conn: sqlite3.Connection, revisor_id: int) -> int:
    """Quantas sobraram na fila DESTA pessoa (a mesma regra da listagem)."""
    if solomod.ligado():
        return int(
            conn.execute(
                "SELECT count(*) AS n FROM anotacoes WHERE status = 'pendente_avaliacao'"
            ).fetchone()["n"]
        )
    return int(
        conn.execute(
            "SELECT count(*) AS n FROM anotacoes an "
            "JOIN atribuicoes a ON a.id = an.atribuicao_id "
            "WHERE an.status = 'pendente_avaliacao' AND a.anotador_id <> ?",
            (revisor_id,),
        ).fetchone()["n"]
    )


# ---------------------------------------------------------------------------
# a escalação: a fila do admin e a decisão que encerra
# ---------------------------------------------------------------------------


@router.get("/api/escalacao/fila", summary="Os itens escalados esperando o admin")
def fila_escalacao(
    anot: ConAnotacao,
    corpus: ConCorpus,
    admin_id: Annotated[int, Query(ge=1)],
) -> dict[str, Any]:
    """O que o revisor marcou ``borderline_admin`` e ninguém decidiu ainda.

    Cada item vem com o **parecer inteiro** (as duas escalas, a justificativa e
    o diff com o motivo de cada mudança): o admin decide sobre a avaliação, não
    sobre o item cru, e mandá-lo abrir outra tela para ler o parecer faria a
    escalação custar mais que o problema que ela resolve.
    """
    exigir_papel(exigir_anotador(anot, admin_id), *PAPEIS_ESCALACAO)
    linhas = anot.execute(avmod.SQL_ESCALADOS).fetchall()
    corte = cat.texto_da_lista()
    itens: list[dict[str, Any]] = []
    for linha in linhas:
        uid = str(linha["prompt_uid"])
        prompt = tmod.resolver_prompt(anot, corpus, uid)
        itens.append(
            {
                "avaliacao_id": int(linha["avaliacao_id"]),
                "anotacao_id": int(linha["anotacao_id"]),
                "versao": int(linha["versao"]),
                "tipo": str(linha["tipo"]),
                "tarefa_id": int(linha["tarefa_id"]),
                "atribuicao_id": int(linha["atribuicao_id"]),
                "anotador_id": int(linha["anotador_id"]),
                "anotador": str(linha["anotador"]),
                "revisor_id": int(linha["revisor_id"]),
                "revisor": str(linha["revisor"]),
                "avaliacao_antes": str(linha["avaliacao_antes"]),
                "avaliacao_depois": str(linha["avaliacao_depois"]),
                "justificativa": str(linha["justificativa"]),
                "autorrevisao": bool(linha["autorrevisao"]),
                "criada_em": linha["criada_em"],
                "projeto_id": None if linha["projeto_id"] is None else int(linha["projeto_id"]),
                "projeto": linha["projeto"],
                "prompt_uid": uid,
                "disponivel": prompt is not None,
                "prompt": None if prompt is None else prompt["text"][:corte],
                "edicoes": avmod.edicoes(anot, int(linha["avaliacao_id"])),
            }
        )
    return {"items": itens, "total": len(itens), "modo_solo": solomod.ligado()}


@router.post("/api/escalacao/{avaliacao_id}", summary="O admin decide e encerra")
def decidir(
    avaliacao_id: int, corpo: DecisaoAdminIn, anot: ConAnotacao
) -> dict[str, Any]:
    """``aprovada`` | ``devolvida`` | ``descartada``. Uma decisão, e é final.

    ``UNIQUE(avaliacao_id)`` no DDL: uma segunda decisão sobre a mesma avaliação
    seria a reabertura silenciosa do que já foi encerrado.

    ``devolvida`` **reabre a atribuição** (``em_andamento``), como a triagem faz
    — e é o único caminho pelo qual um item que passou da triagem volta ao
    anotador. Ele existe porque é o admin quem o percorre, com o parecer do
    revisor em mãos, e não a passagem 2 se contradizendo.
    """
    admin = exigir_papel(exigir_anotador(anot, corpo.admin_id), *PAPEIS_ESCALACAO)
    linha = anot.execute(
        "SELECT av.id, av.anotacao_id, av.revisor_id, an.status, an.versao, "
        "       an.atribuicao_id, t.tipo "
        "FROM avaliacoes av JOIN anotacoes an ON an.id = av.anotacao_id "
        "JOIN atribuicoes a ON a.id = an.atribuicao_id "
        "JOIN tarefas t ON t.id = a.tarefa_id "
        "WHERE av.id = ?",
        (avaliacao_id,),
    ).fetchone()
    if linha is None:
        raise HTTPException(status_code=404, detail=f"avaliação {avaliacao_id} não existe")

    atual = str(linha["status"])
    destino = adb.DESFECHO_DECISAO[corpo.decisao]
    if destino not in adb.TRANSICOES.get(atual, ()):
        raise HTTPException(
            status_code=409,
            detail=(
                f"esta anotação está em {atual!r}; o admin só decide sobre 'escalada' "
                "(alguém já decidiu esta escalação)"
            ),
        )

    anot.execute("BEGIN IMMEDIATE")
    try:
        cur = anot.execute(
            "INSERT INTO decisoes_admin (avaliacao_id, admin_id, decisao, comentario) "
            "VALUES (?, ?, ?, ?)",
            (avaliacao_id, corpo.admin_id, corpo.decisao, corpo.comentario),
        )
        decisao_id = int(cur.lastrowid or 0)
        anot.execute(
            "UPDATE anotacoes SET status = ? WHERE id = ?",
            (destino, int(linha["anotacao_id"])),
        )
        if corpo.decisao == "devolvida":
            # REABRE A MESMA LINHA, como a triagem: o UNIQUE(tarefa_id,
            # anotador_id) é a trava anti-repetição, e uma segunda atribuição
            # faria o mesmo trabalho contar duas vezes em toda métrica. O prazo
            # é renovado — uma escalação decidida dias depois devolveria uma
            # tarefa já vencida (ver `tarefas.SQL_REABRIR`).
            tmod.reabrir(anot, int(linha["atribuicao_id"]))
        evmod.registrar(
            anot,
            acao="decisao_admin",
            entidade="avaliacao",
            entidade_id=avaliacao_id,
            ator_id=corpo.admin_id,
            admin=str(admin["nome"]),
            anotacao_id=int(linha["anotacao_id"]),
            versao=int(linha["versao"]),
            tipo=str(linha["tipo"]),
            decisao=corpo.decisao,
            de=atual,
            para=destino,
            comentario=corpo.comentario,
        )
        anot.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        anot.execute("ROLLBACK")
        raise HTTPException(
            status_code=409, detail=f"esta escalação já foi decidida ({exc})"
        ) from exc
    except Exception:
        anot.execute("ROLLBACK")
        raise

    return {
        "avaliacao_id": avaliacao_id,
        "decisao_id": decisao_id,
        "anotacao_id": int(linha["anotacao_id"]),
        "decisao": corpo.decisao,
        "status": destino,
        "reabriu": corpo.decisao == "devolvida",
        "na_fila": int(
            anot.execute(
                "SELECT count(*) AS n FROM avaliacoes av "
                "LEFT JOIN decisoes_admin d ON d.avaliacao_id = av.id "
                "WHERE av.avaliacao_depois = 'borderline_admin' AND d.id IS NULL"
            ).fetchone()["n"]
        ),
    }


__all__ = ["PAPEIS_AVALIACAO", "PAPEIS_ESCALACAO", "router"]
