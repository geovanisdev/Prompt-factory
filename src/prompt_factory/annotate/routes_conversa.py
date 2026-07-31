"""As rotas da conversa com o modelo local (P4d).

Todas ``def``, nenhuma ``async def`` — a mesma regra de ``routes_trabalho``, e
aqui ela é MAIS importante, não menos: a geração leva de 1,4 s a 17 s (medido
nesta máquina, ver ``modelos.py``), e uma rota ``async def`` bloqueando o event
loop por dezessete segundos derrubaria a interface inteira, não só a aba. Em
``def``, o Starlette a joga no threadpool e o resto continua respondendo.

Com ``workers=1`` e um usuário, essa é a escolha certa. Streaming token a token
mudaria o desenho (uma rota que não toca o banco para o fluxo, outra síncrona
para gravar) e não foi feito: o que a tela precisa é saber que a máquina está
trabalhando e há quanto tempo, e isso é um indicador, não um fluxo.

A ORDEM DAS ESCRITAS É O CONTRATO DESTE MÓDULO
==============================================
**O turno do humano é gravado ANTES de chamar o Ollama.** Invertido, um timeout
de 180 s custaria o que a pessoa acabou de escrever — e o pior desfecho possível
nesta aba é perder trabalho. Gravando antes, a falha deixa a conversa num estado
nomeado (``aguardando``) e a tela oferece "tentar de novo" sobre o mesmo turno.

O QUE ESTAS ROTAS NUNCA FAZEM
=============================
Interpretar a saída do modelo. Ela é gravada como texto, devolvida como texto e
desenhada por ``textContent``. O prompt que a originou vem do corpus — que tem
``<script>`` de verdade e tem texto mandando ignorar instruções anteriores — e a
resposta a ele é o objeto da avaliação, nunca uma instrução a seguir.
"""

from __future__ import annotations

import secrets
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from . import conversa as convmod
from . import db as adb
from . import eventos as evmod
from . import modelos as modmod
from .deps import ConAnotacao, exigir_anotador, exigir_papel
from .models import EscolherRodadaIn, TurnoIn

router = APIRouter(tags=["conversa"])


def _quem(conn: sqlite3.Connection, anotador_id: int) -> sqlite3.Row:
    return exigir_papel(exigir_anotador(conn, anotador_id), "anotador")


def _minha_conversa(
    conn: sqlite3.Connection, atribuicao_id: int, anotador_id: int
) -> sqlite3.Row:
    """A atribuição, conferida: existe, é minha, é de conversa e ainda está aberta."""
    linha = conn.execute(
        "SELECT a.id, a.status, a.anotador_id, t.id AS tarefa_id, t.tipo, t.prompt_uid "
        "FROM atribuicoes a JOIN tarefas t ON t.id = a.tarefa_id WHERE a.id = ?",
        (atribuicao_id,),
    ).fetchone()
    if linha is None:
        raise HTTPException(status_code=404, detail=f"atribuição {atribuicao_id} não existe")
    if int(linha["anotador_id"]) != anotador_id:
        raise HTTPException(status_code=403, detail="esta atribuição é de outra pessoa")
    if str(linha["tipo"]) not in adb.TIPOS_CONVERSA:
        raise HTTPException(
            status_code=409,
            detail=(
                f"a tarefa {atribuicao_id} é do tipo {linha['tipo']} e não conversa com "
                "modelo nenhum"
            ),
        )
    if str(linha["status"]) in ("submetida", "aprovada"):
        # Continuar a conversa depois do envio reescreveria a conversa que a
        # anotação descreve — a avaliação passaria a falar de outro objeto.
        raise HTTPException(
            status_code=409,
            detail="esta conversa já foi enviada para avaliação e não continua",
        )
    return linha


def _erro_de_inferencia(exc: modmod.FalhaDeInferencia) -> HTTPException:
    """Falha do runtime vira **503 com a chave da tela**, nunca 500.

    503 e não 500 porque a causa é um serviço de fora: o Ollama não está de pé,
    ou o modelo não foi baixado. Um 500 diria "a plataforma quebrou" sobre um
    arquivo que faltou baixar — a leitura errada, e a mais cara.

    O ``detail`` sai em dois formatos pela regra do P3i: a frase em português
    (para ``/docs`` e para quem não é este ``index.html``) e a CHAVE + dados,
    porque a tela fala duas línguas e a rota não.
    """
    return HTTPException(
        status_code=503,
        detail={
            "mensagem": str(exc),
            "chave": exc.chave,
            "dados": exc.dados,
            "url": modmod.base_url(),
        },
    )


# ---------------------------------------------------------------------------
# o estado do runtime
# ---------------------------------------------------------------------------


@router.get("/api/modelos", summary="O Ollama está de pé, e com quais modelos")
def listar_modelos() -> dict[str, Any]:
    """O retrato do runtime local. **200 mesmo com tudo fora do ar.**

    Modelo ausente e servidor fora do ar são estados de PRIMEIRA CLASSE: a
    máquina que abre esta plataforma pode não ter baixado 7,5 GB ainda, e isso é
    normal. A resposta traz o que falta e o comando que conserta
    (``ollama pull gemma3n:e4b``), e a tela desenha os dois. Um 503 aqui faria a
    interface tratar "falta baixar um arquivo" como "a plataforma quebrou".

    Não toca banco nenhum — por isso não pede conexão. Continua ``def`` porque
    abre um socket, e socket em ``async def`` bloqueia o event loop igual.
    """
    return {
        **modmod.estado(),
        "max_turnos": modmod.max_turnos(),
        "escala_turno": {"min": adb.ESCALA_TURNO[0], "max": adb.ESCALA_TURNO[1]},
        "num_predict": modmod.num_predict(),
        "temperature": modmod.temperatura(),
        "num_ctx": modmod.num_ctx(),
    }


# ---------------------------------------------------------------------------
# a conversa
# ---------------------------------------------------------------------------


@router.get("/api/conversa/{atribuicao_id}", summary="A conversa desta tarefa")
def ler_conversa(
    atribuicao_id: int,
    anot: ConAnotacao,
    anotador_id: Annotated[int, Query(ge=1)],
) -> dict[str, Any]:
    """Os turnos gravados. **Cegos no duelo** — sem ``modelo`` nem ``digest``."""
    _quem(anot, anotador_id)
    linha = _minha_conversa(anot, atribuicao_id, anotador_id)
    return {
        "atribuicao_id": atribuicao_id,
        "tipo": str(linha["tipo"]),
        "conversa": convmod.estado(anot, atribuicao_id, str(linha["tipo"])),
    }


def _preparar_modelos(tipo: str) -> tuple[list[str], dict[str, str]]:
    """Quais modelos respondem este turno, e o digest de cada um.

    Uma consulta a ``/api/tags`` por TURNO, não por resposta: no duelo são duas
    gerações contra o mesmo inventário, e ele não muda entre elas.
    """
    configurados = modmod.modelos_configurados()
    quantos = 2 if tipo == "duelo_modelos" else 1
    if len(configurados) < quantos:
        raise HTTPException(
            status_code=503,
            detail={
                "mensagem": (
                    f"o duelo precisa de {quantos} modelos e [annotate] ollama_modelos "
                    f"declara {len(configurados)}"
                ),
                "chave": modmod.T_NENHUM,
                "dados": {},
                "url": modmod.base_url(),
            },
        )
    instalados = modmod.listar()
    mapa = modmod.digests(instalados)
    escolhidos = list(configurados[:quantos])
    faltando = [m for m in escolhidos if m not in mapa]
    if faltando:
        raise _erro_de_inferencia(
            modmod.ModeloAusente(
                f"{', '.join(faltando)} não está baixado nesta máquina. "
                + "  ".join(modmod.COMANDO_PULL.format(modelo=m) for m in faltando),
                modelos=faltando,
                comandos=[modmod.COMANDO_PULL.format(modelo=m) for m in faltando],
            )
        )
    return escolhidos, mapa


def _gerar(
    anot: sqlite3.Connection,
    atribuicao_id: int,
    tipo: str,
    ordem: int,
    ator_id: int,
) -> None:
    """Chama o(s) modelo(s) e grava o(s) turno(s) NA ``ordem`` recebida.

    ``ordem`` é a do turno do MODELO — a do humano mais um. Num duelo as duas
    respostas compartilham essa mesma ordem e se distinguem pelo rótulo; é
    exatamente o que o ``UNIQUE (atribuicao_id, ordem, rotulo)`` do DDL diz.

    NO DUELO, QUEM É "A" É SORTEADO A CADA RODADA. Fixar ``A = primeiro da
    configuração`` deixaria a preferência medir a posição da coluna: um anotador
    que perceba que "A é sempre o gemma" passa a votar na marca, e o dado
    inteiro perde o valor. O sorteio é do SERVIDOR e o mapa não sai daqui.
    """
    escolhidos, mapa = _preparar_modelos(tipo)
    # Uma tentativa anterior pode ter deixado meia rodada gravada (aconteceu no
    # navegador: um lado respondeu e o outro estourou o teto de tokens). Meia
    # rodada não é dado — é um duelo com um lado só, que o `estado` leria como
    # rodada completa e o histórico levaria adiante como vencedora que ninguém
    # escolheu. Ela sai antes de qualquer coisa.
    convmod.limpar_ordem(anot, atribuicao_id, ordem)
    historico = convmod.historico(anot, atribuicao_id)

    if tipo != "duelo_modelos":
        resultado = modmod.conversar(
            escolhidos[0], historico, digest=mapa.get(escolhidos[0], "")
        )
        convmod.gravar_modelo(
            anot, atribuicao_id, ordem, convmod.SEM_ROTULO, resultado, escolhida=True
        )
        evmod.registrar(
            anot,
            acao="turno_gerado",
            entidade="atribuicao",
            entidade_id=atribuicao_id,
            ator_id=ator_id,
            ordem=ordem,
            modelo=resultado["modelo"],
            digest=resultado["digest"][:12],
            duracao_ms=resultado["duracao_ms"],
            truncado=resultado["truncado"],
        )
        return

    lados = list(escolhidos[:2])
    if secrets.randbelow(2):
        lados.reverse()
    # GERA OS DOIS, DEPOIS GRAVA OS DOIS. Gravando dentro do laço, uma falha no
    # segundo modelo deixaria uma rodada com UM lado — e o `sqlite3` do projeto
    # é autocommit, então o primeiro já estaria no disco. O `estado` leria essa
    # meia rodada como completa (uma rodada só é "pendente" quando tem dois
    # lados) e o histórico levaria adiante uma vencedora que ninguém escolheu.
    gerados = [
        (rotulo, modmod.conversar(nome, historico, digest=mapa.get(nome, "")))
        for rotulo, nome in zip(adb.ROTULOS_DUELO, lados, strict=True)
    ]
    for rotulo, resultado in gerados:
        convmod.gravar_modelo(anot, atribuicao_id, ordem, rotulo, resultado)
    # O evento NÃO carrega o mapa de quem é A: `eventos` é uma trilha que o
    # painel do admin lê, e um mapa cego que vaza por uma porta que ninguém está
    # olhando vaza do mesmo jeito. Ele carrega o par, sem os lados.
    evmod.registrar(
        anot,
        acao="rodada_gerada",
        entidade="atribuicao",
        entidade_id=atribuicao_id,
        ator_id=ator_id,
        ordem=ordem,
        modelos=sorted(lados),
    )


@router.post("/api/conversa/{atribuicao_id}/turno", summary="Mandar um turno ao modelo")
def turno(atribuicao_id: int, corpo: TurnoIn, anot: ConAnotacao) -> dict[str, Any]:
    """Grava o turno do humano, gera a(s) resposta(s) e devolve a conversa.

    ``texto`` ausente = **tentar de novo** sobre o último turno do humano, que
    ficou sem resposta porque a geração anterior falhou. É por isso que ele é
    opcional: obrigar a redigitar um parágrafo porque o Ollama demorou seria
    cobrar do anotador o preço de um timeout.
    """
    _quem(anot, corpo.anotador_id)
    linha = _minha_conversa(anot, atribuicao_id, corpo.anotador_id)
    tipo = str(linha["tipo"])

    estado = convmod.estado(anot, atribuicao_id, tipo)
    if estado["rodada_pendente"] is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"a rodada {estado['rodada_pendente']} ainda não tem vencedora — sem ela "
                "não dá para saber com qual resposta a conversa continua"
            ),
        )

    aguardando = estado["aguardando"]
    if corpo.texto is None:
        if aguardando is None:
            raise HTTPException(
                status_code=409,
                detail="não há turno esperando resposta — escreva o próximo",
            )
        # A resposta vai DEPOIS do turno do humano que ficou sem ela.
        ordem = int(aguardando) + 1
    else:
        if aguardando is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"o turno {aguardando} ficou sem resposta. Tente gerá-lo de novo "
                    "antes de escrever o próximo"
                ),
            )
        if estado["n_turnos"] >= modmod.max_turnos():
            raise HTTPException(
                status_code=409,
                detail=(
                    f"esta conversa chegou ao teto de {modmod.max_turnos()} turnos "
                    "([annotate] max_turnos_conversa). Encerre e avalie"
                ),
            )
        # O TURNO DO HUMANO É GRAVADO PRIMEIRO. Ver o cabeçalho: invertido, um
        # timeout de 180 s custaria o que a pessoa acabou de escrever.
        ordem = convmod.gravar_usuario(anot, atribuicao_id, corpo.texto) + 1

    try:
        _gerar(anot, atribuicao_id, tipo, ordem, corpo.anotador_id)
    except modmod.FalhaDeInferencia as exc:
        # O turno do humano CONTINUA GRAVADO — é o ponto da ordem das escritas.
        # A resposta traz a conversa como ela ficou, para a tela redesenhar sem
        # perder nada e oferecer "tentar de novo".
        erro = _erro_de_inferencia(exc)
        erro.detail["conversa"] = convmod.estado(anot, atribuicao_id, tipo)
        raise erro from exc

    return {
        "atribuicao_id": atribuicao_id,
        "tipo": tipo,
        "conversa": convmod.estado(anot, atribuicao_id, tipo),
    }


@router.post(
    "/api/conversa/{atribuicao_id}/escolher", summary="Escolher a vencedora da rodada"
)
def escolher(
    atribuicao_id: int, corpo: EscolherRodadaIn, anot: ConAnotacao
) -> dict[str, Any]:
    """Marca a vencedora de uma rodada de duelo — e é ela que continua a conversa.

    **A resposta não revela o modelo.** O rótulo escolhido é "A" ou "B" até o
    envio da avaliação; quem venceu de verdade só aparece em ``conversa.revelacao``,
    depois de a anotação estar gravada.
    """
    _quem(anot, corpo.anotador_id)
    linha = _minha_conversa(anot, atribuicao_id, corpo.anotador_id)
    tipo = str(linha["tipo"])
    if tipo != "duelo_modelos":
        raise HTTPException(
            status_code=409, detail="só o duelo tem rodada com duas respostas a escolher"
        )
    try:
        convmod.escolher(anot, atribuicao_id, corpo.ordem, corpo.rotulo, corpo.justificativa)
    except convmod.ConversaInvalida as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    evmod.registrar(
        anot,
        acao="rodada_decidida",
        entidade="atribuicao",
        entidade_id=atribuicao_id,
        ator_id=corpo.anotador_id,
        ordem=corpo.ordem,
        # O RÓTULO, nunca o modelo: a trilha de auditoria também é uma porta.
        rotulo=corpo.rotulo,
    )
    return {
        "atribuicao_id": atribuicao_id,
        "tipo": tipo,
        "conversa": convmod.estado(anot, atribuicao_id, tipo),
    }


__all__ = ["router"]
