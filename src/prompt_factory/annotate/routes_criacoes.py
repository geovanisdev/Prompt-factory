"""As rotas do MODO CRIAR — escrever, listar e revisar prompts novos.

Os outros dois modos de seleção consomem o corpus; este o alimenta. A mecânica
é a mais simples da plataforma de propósito — não há fila, nem trava, nem prazo:
escrever um prompt não é uma vaga que alguém pode roubar, e um TTL sobre um
texto que a pessoa está redigindo devolveria a "vaga" no meio de uma frase.

A REVISÃO AQUI É UMA PASSAGEM SÓ, E ISSO É DECISÃO
==================================================
Anotação tem duas (triagem + Rate and Review) porque o produto é um JULGAMENTO,
e julgamento se corrige no lugar com motivo por mudança. Uma criação é um TEXTO:
ou ele serve de prompt e entra no corpus, ou não serve. Corrigir o texto de
outra pessoa e ingerir o resultado como "prompt escrito por gente" destruiria
justamente a proveniência que o projeto inteiro existe para preservar — o texto
deixaria de ser de quem escreveu sem que nada no dado dissesse isso.

Por isso a recusa exige comentário e a aprovação não: quem escreveu precisa
saber o que fazer diferente, e "está bom" não precisa de parágrafo.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from . import criacoes as crimod
from . import pedidos as pedmod
from . import solo as solomod
from . import tarefas as tmod
from .deps import ConAnotacao, ConCorpus, exigir_anotador, exigir_papel
from .models import CriacaoIn, DevolverPedidoIn, ProximoPedidoIn, RevisarCriacaoIn

router = APIRouter(tags=["criações"])

#: Quem revisa criação. Os mesmos da triagem, e pela mesma razão registrada lá:
#: numa demonstração com seis personas, um revisor que escreveu tudo travaria a
#: fila, e a saída do estado vazio é entrar como administrador.
PAPEIS_REVISAO: tuple[str, ...] = ("revisor", "admin")


def _vocabulario() -> dict[str, set[str]]:
    """Os ids válidos de cada eixo, da taxonomia do corpus.

    A MESMA projeção que a pergunta de categoria do P9 usa
    (``tarefas._vocabulario_taxonomia``), e não uma segunda leitura do arquivo:
    duas listas da mesma taxonomia divergiriam, e a divergência apareceria como
    uma sugestão que o formulário aceita e o painel não sabe desenhar.
    """
    return {
        eixo: {str(o["id"]) for o in opcoes}
        for eixo, opcoes in tmod._vocabulario_taxonomia().items()
    }


def _conferir_sugestoes(corpo: CriacaoIn) -> None:
    """As sugestões pertencem à taxonomia — ou não vêm.

    Recusar exige CERTEZA: sem vocabulário em mãos (taxonomia ilegível) o
    pertencimento não é conferível e a sugestão passa, como na pergunta de
    categoria. O que não pode é um id inventado ser gravado como se fosse uma
    classe do corpus — ele viajaria até o painel do admin parecendo um rótulo.
    """
    vocab = _vocabulario()
    for campo, eixo in (("task_type_sugerido", "task_type"), ("domain_sugerido", "domain")):
        valor = getattr(corpo, campo)
        validos = vocab.get(eixo) or set()
        if valor and validos and valor not in validos:
            raise HTTPException(
                status_code=422,
                detail=f"{campo}: {valor!r} não é uma classe da taxonomia do corpus",
            )


@router.post("/api/criacoes", status_code=201, summary="Escrever um prompt novo")
def criar(corpo: CriacaoIn, anot: ConAnotacao, corpus: ConCorpus) -> dict[str, Any]:
    """Grava o prompt e devolve a linha **com o aviso de duplicata**.

    201 mesmo quando o texto colide com o corpus. O aviso viaja no corpo
    (``duplicata_corpus`` + ``uid_duplicata``) e a tela o mostra como
    RESULTADO, não como erro: a mesma cadeia de normalização que junta as
    duplicatas do WildChat acabou de reconhecer que duas pessoas escreveram a
    mesma coisa. Devolver 409 aqui trataria a prova de que o dedup funciona
    como uma falha de quem escreveu.
    """
    exigir_anotador(anot, corpo.autor_id)
    _conferir_sugestoes(corpo)
    if corpo.pedido_id is not None:
        # A MESMA função que a tela consulta para saber se ainda pode enviar.
        # Duas implementações da regra dariam um botão habilitado que devolve
        # erro — o defeito que `catalogo.material_faltando` já evitou uma vez.
        try:
            pedmod.conferir_para_criacao(anot, corpo.pedido_id, anotador_id=corpo.autor_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return crimod.criar(
        anot,
        corpus,
        autor_id=corpo.autor_id,
        texto=corpo.texto,
        lang=corpo.lang,
        task_type_sugerido=corpo.task_type_sugerido,
        domain_sugerido=corpo.domain_sugerido,
        brief=corpo.brief,
        pedido_id=corpo.pedido_id,
        material=None if corpo.material is None else corpo.material.model_dump(),
    )


# ---------------------------------------------------------------------------
# A FILA DE PEDIDOS (Central de Briefs) — o insumo da vista criar
# ---------------------------------------------------------------------------
#
# Rotas no mesmo router do modo criar, e não num arquivo próprio: o pedido não é
# um produto ao lado da criação, é o INSUMO dela. Separá-los sugeriria uma vista
# de pedidos que não existe — corrigir um pedido é arquivá-lo e redestilar.


@router.get("/api/pedidos", summary="A fila de pedidos: o meu e o que há disponível")
def pedidos_fila(
    anot: ConAnotacao, anotador_id: Annotated[int, Query(ge=1)]
) -> dict[str, Any]:
    """O pedido reservado por esta pessoa (ou ``null``) e as facetas do filtro.

    Uma rota e não duas porque a tela precisa das duas coisas no mesmo paint: o
    painel do pedido e o seletor de disciplina aparecem juntos, e duas
    requisições dariam um instante em que o filtro promete disciplinas que a
    fila já não tem.
    """
    return {
        "meu": pedmod.meu(anot, anotador_id),
        "facetas": pedmod.facetas(anot),
        "reserva_ttl_min": pedmod.reserva_ttl_min(),
    }


@router.post("/api/pedidos/proximo", summary="Reservar o próximo pedido da fila")
def pedidos_proximo(corpo: ProximoPedidoIn, anot: ConAnotacao) -> dict[str, Any]:
    """Reserva e devolve o pedido. Fila vazia é **200 com ``pedido: null``**.

    A mesma decisão do claim de tarefa: fila vazia é o estado mais comum de uma
    plataforma bem servida, e um 404 faria o cliente tratar o caminho normal
    como falha. O ``motivo_chave`` é o que a tela traduz; o ``motivo`` em
    português é para o ``/docs``.
    """
    exigir_anotador(anot, corpo.anotador_id)
    return pedmod.proximo(
        anot,
        anotador_id=corpo.anotador_id,
        disciplina=corpo.disciplina,
        papel=corpo.papel,
    )


@router.post("/api/pedidos/{pedido_id}/devolver", summary="Devolver o pedido à fila")
def pedidos_devolver(
    pedido_id: int, corpo: DevolverPedidoIn, anot: ConAnotacao
) -> dict[str, Any]:
    """Desfaz a reserva. Não penaliza e não arquiva — o pedido volta como estava."""
    exigir_anotador(anot, corpo.anotador_id)
    try:
        return pedmod.devolver(anot, pedido_id, anotador_id=corpo.anotador_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/criacoes", summary="As criações — as minhas ou a fila de revisão")
def listar(
    anot: ConAnotacao,
    corpus: ConCorpus,
    anotador_id: Annotated[int, Query(ge=1)],
    fila: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """``fila=false`` (default) as minhas; ``fila=true`` as pendentes de revisão.

    Duas leituras da mesma tabela, e não duas rotas, porque o que muda entre
    elas é um WHERE — mas o PAPEL é conferido só na fila: qualquer anotador vê
    as próprias criações, e ver a fila de revisão é a autorização.

    Na fila, as próprias saem (o revisor não decide sobre o que escreveu),
    **salvo no modo solo** — que é declarado na tela e no ``/api/health``, como
    em toda a plataforma.
    """
    quem = exigir_anotador(anot, anotador_id)
    if not fila:
        return {
            "items": crimod.listar(anot, corpus, autor_id=anotador_id),
            "fila": False,
            "funil": crimod.funil(anot),
        }

    exigir_papel(quem, *PAPEIS_REVISAO)
    itens = crimod.listar(anot, corpus, status="submetida")
    if not solomod.ligado():
        itens = [i for i in itens if int(i["autor_id"]) != anotador_id]
    return {
        "items": itens,
        "fila": True,
        "modo_solo": solomod.ligado(),
        "funil": crimod.funil(anot),
    }


@router.post("/api/criacoes/{criacao_id}/revisar", summary="Aprovar ou recusar uma criação")
def revisar(
    criacao_id: int, corpo: RevisarCriacaoIn, anot: ConAnotacao
) -> dict[str, Any]:
    """A decisão. Aprovar grava o ``uid_previsto``; recusar exige o comentário.

    O 409 de "já foi revisada" é do domínio (``criacoes.revisar`` levanta
    ``ValueError``) e não de um ``if`` aqui: a regra de qual status pode
    transitar mora junto do UPDATE que a aplica, senão a rota e o módulo
    passariam a ter opiniões separadas sobre a mesma máquina de estados.
    """
    quem = exigir_papel(exigir_anotador(anot, corpo.revisor_id), *PAPEIS_REVISAO)

    linha = anot.execute(
        "SELECT autor_id FROM criacoes WHERE id = ?", (criacao_id,)
    ).fetchone()
    if linha is None:
        raise HTTPException(status_code=404, detail=f"criação {criacao_id} não existe")
    if int(linha["autor_id"]) == int(quem["id"]) and not solomod.ligado():
        raise HTTPException(
            status_code=403,
            detail=(
                "este prompt é seu — quem revisa nunca é quem escreveu. "
                "Para montar um portfólio sozinho, ligue `[annotate] "
                "permitir_autorrevisao` no settings.toml: a plataforma passa a "
                "permitir e diz na tela que está permitindo."
            ),
        )

    try:
        return crimod.revisar(
            anot,
            criacao_id,
            revisor_id=corpo.revisor_id,
            aprovar=corpo.aprovar,
            comentario=corpo.comentario,
        )
    except LookupError as exc:  # pragma: no cover - o SELECT acima já pegou
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


__all__ = ["PAPEIS_REVISAO", "router"]
