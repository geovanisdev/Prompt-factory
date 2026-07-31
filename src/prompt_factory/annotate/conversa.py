"""A conversa com o modelo local: os turnos no banco e o payload que sai deles.

Divisão de trabalho deste marco, em uma linha cada:

* ``modelos.py`` fala HTTP com o Ollama e **não conhece o banco**;
* este módulo guarda os turnos e **não abre socket nenhum**;
* ``routes_conversa.py`` costura os dois e é quem tem as duas conexões.

Separados porque as falhas são de naturezas diferentes: um modelo que não foi
baixado é um estado da MÁQUINA, e uma rodada sem vencedora é um estado do
TRABALHO. Misturá-los produziria um 500 no lugar de uma tela que diz o que fazer.

POR QUE OS TURNOS MORAM NO SERVIDOR
===================================
1. **O duelo só é cego se o mapa não sair daqui.** A tela recebe "A" e "B"; qual
   deles é ``gemma3n:e4b`` fica no banco até a avaliação ser enviada. Se o nome
   viajasse (mesmo escondido num campo que a tela não desenha), bastaria abrir o
   inspetor para a preferência passar a ser sobre a marca — e uma preferência
   sobre a marca não vale o tempo de quem a produziu.
2. **Quem respondeu fica gravado com nome E digest.** ``qwen3:4b`` é uma TAG, e
   tag é reescrita quando o autor republica o modelo; o digest é o conteúdo.
3. **Trinta turnos não se perdem.** Recarregar a página, fechar a aba ou
   derrubar o servidor no meio não custa a conversa — e perder trabalho é a
   maior fricção que existe nesta plataforma.

O TEXTO DO MODELO É DADO
========================
Nada aqui interpreta a saída do modelo. Ela é ``TEXT`` numa coluna, sai como
``str`` num JSON e é desenhada com ``textContent``. O prompt que a originou vem
do corpus, que contém ``<script>`` de verdade e contém texto mandando ignorar
instruções anteriores — e a resposta a esse prompt é justamente o que está sendo
avaliado, nunca algo a obedecer.
"""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

#: A rubrica que se aplica a uma CONVERSA, dentro do pacote (como as diretrizes
#: e o ``demo_pack``, e pelo mesmo motivo: ``data/`` é gitignorado e um
#: instrumento de medida precisa sobreviver a um clone).
ARQUIVO_RUBRICA = Path(__file__).resolve().parent / "fixtures" / "rubrica_multiturno.json"

#: A identidade do instrumento, gravada em ``payload_json.rubrica``. Sem ela, uma
#: nota "4 em coerência" de hoje não teria como ser comparada com a de amanhã se
#: a rubrica mudasse — é o mesmo papel do ``versao_diretriz`` para a instrução.
ID_RUBRICA = "multiturno@1"

#: A frase do papel de cada turno para o ``/api/chat``. Num lugar só: os dois
#: vocabulários (o do banco e o do Ollama) se encontram exatamente aqui.
PAPEL_OLLAMA: dict[str, str] = {"usuario": "user", "modelo": "assistant"}

#: Turno do humano: sem lado, e por isso sem rótulo. É também o valor que o
#: UNIQUE do DDL usa para deixar UM turno de humano por ``ordem``.
SEM_ROTULO = ""


class ConversaInvalida(RuntimeError):
    """A conversa não está num estado que permita a operação pedida.

    Exceção própria porque as rotas a traduzem em **409**, não em 500: "a rodada
    anterior ainda não tem vencedora" é um estado normal do trabalho, e a tela
    sabe o que fazer com ele.
    """


# ---------------------------------------------------------------------------
# a rubrica multi-turno
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _bruto() -> dict[str, Any]:
    return json.loads(ARQUIVO_RUBRICA.read_text(encoding="utf-8"))


def rubrica() -> dict[str, Any]:
    """A rubrica multi-turno **na forma do envelope** (a mesma de ``rubrica_ativa``).

    A mesma forma de propósito: a tela desenha os critérios com o código que já
    existe, e ``tarefas.erro_contra_a_rubrica`` — a função pura que os dois
    escritores chamam — a confere sem saber que ela veio de um arquivo em vez de
    uma linha de ``rubricas``.

    Ela **não** é semeada na tabela ``rubricas``: aquela tabela é indexada por
    ``prompt_uid``, e esta rubrica não é de prompt nenhum. Ela é da PLATAFORMA, e
    vale para toda conversa — semeá-la por prompt criaria uma cópia por item, e
    a primeira que divergisse seria impossível de encontrar.
    """
    dados = _bruto()
    return {
        "id": None,
        "rubrica_id": str(dados["id"]),
        "titulo": str(dados["titulo"]),
        "titulo_i18n": dados.get("titulo_i18n"),
        # Cópia rasa da lista: quem receber isto não pode mutar o cache.
        "criterios": [dict(c) for c in dados["criterios"]],
        "origem": "plataforma",
        "nota_chave": "ws.rubrica_multiturno",
    }


def nomes_dos_criterios() -> list[str]:
    """Os ``nome`` canônicos, na ordem. É a IDENTIDADE gravada em ``notas[].criterio``."""
    return [str(c["nome"]) for c in _bruto()["criterios"]]


# ---------------------------------------------------------------------------
# leitura dos turnos
# ---------------------------------------------------------------------------

_COLUNAS = (
    "id, ordem, papel, rotulo, texto, raciocinio, truncado, modelo, digest, "
    "escolhida, justificativa, duracao_ms, criado_em"
)


def _linhas(conn: sqlite3.Connection, atribuicao_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT {_COLUNAS} FROM turnos_conversa WHERE atribuicao_id = ? "
        "ORDER BY ordem, rotulo, id",
        (atribuicao_id,),
    ).fetchall()


def _dict(linha: sqlite3.Row, *, cego: bool) -> dict[str, Any]:
    """Uma linha como a tela a recebe. ``cego`` apaga QUEM respondeu.

    O apagamento é por OMISSÃO da chave, e não por ``""``: uma chave ``modelo``
    presente e vazia convidaria a tela a desenhar um espaço reservado onde o
    nome vai aparecer, e o espaço reservado é metade do vazamento.
    """
    turno: dict[str, Any] = {
        "ordem": int(linha["ordem"]),
        "papel": str(linha["papel"]),
        "rotulo": str(linha["rotulo"]),
        "texto": str(linha["texto"]),
        # Sai SEMPRE, inclusive no duelo: o raciocínio é conteúdo a avaliar, e
        # ele não identifica o modelo (os dois podem ou não emitir). O que o
        # duelo esconde é QUEM respondeu, não O QUE ele produziu.
        "raciocinio": str(linha["raciocinio"]),
        "truncado": bool(linha["truncado"]),
        "escolhida": bool(linha["escolhida"]),
        "justificativa": linha["justificativa"],
        "duracao_ms": int(linha["duracao_ms"]),
        "criado_em": linha["criado_em"],
    }
    if not cego:
        turno["modelo"] = str(linha["modelo"])
        turno["digest"] = str(linha["digest"])
    return turno


def e_cego(tipo: str) -> bool:
    """No duelo o nome do modelo não sai do servidor; na conversa ele é mostrado.

    Não é inconsistência: na conversa de um modelo só **não há preferência a
    enviesar** — e saber com quem se está falando é o que permite ao anotador
    dizer "este modelo faz isto", que é a informação que a conversa produz.
    """
    return tipo == "duelo_modelos"


def turnos(
    conn: sqlite3.Connection, atribuicao_id: int, tipo: str
) -> list[dict[str, Any]]:
    """Os turnos desta atribuição, prontos para o envelope."""
    cego = e_cego(tipo)
    return [_dict(linha, cego=cego) for linha in _linhas(conn, atribuicao_id)]


def _por_ordem(linhas: list[sqlite3.Row]) -> dict[int, list[sqlite3.Row]]:
    grupos: dict[int, list[sqlite3.Row]] = {}
    for linha in linhas:
        grupos.setdefault(int(linha["ordem"]), []).append(linha)
    return grupos


def aguardando_modelo(conn: sqlite3.Connection, atribuicao_id: int) -> int | None:
    """A ``ordem`` do turno do humano que ficou **sem resposta**, ou ``None``.

    É o estado que a geração fracassada deixa para trás, e ele é de propósito: o
    turno do humano é gravado ANTES de chamar o Ollama, então um timeout de 180 s
    não custa o que a pessoa escreveu. A tela lê isto e oferece "tentar de novo"
    em cima do mesmo turno, em vez de pedir que ela redigite.
    """
    grupos = _por_ordem(_linhas(conn, atribuicao_id))
    if not grupos:
        return None
    ultima = max(grupos)
    return ultima if grupos[ultima][0]["papel"] == "usuario" else None


def estado(conn: sqlite3.Connection, atribuicao_id: int, tipo: str) -> dict[str, Any]:
    """O que a TELA precisa saber para decidir o que oferecer agora.

    ``rodada_pendente`` é a ordem da rodada de duelo que ainda espera uma escolha
    — enquanto ela existir, não há próximo turno a escrever, porque não se sabe
    com qual resposta a conversa continua. ``aguardando`` é o outro estado
    incompleto: o humano escreveu e a geração não voltou.
    """
    linhas = _linhas(conn, atribuicao_id)
    grupos = _por_ordem(linhas)
    pendente = None
    for ordem in sorted(grupos):
        do_grupo = grupos[ordem]
        if do_grupo[0]["papel"] != "modelo":
            continue
        if len(do_grupo) > 1 and not any(int(x["escolhida"]) for x in do_grupo):
            pendente = ordem
            break
    ultima = max(grupos) if grupos else None
    return {
        "turnos": [_dict(linha, cego=e_cego(tipo)) for linha in linhas],
        "n_turnos": len(grupos),
        "max_turnos": _max_turnos(),
        "rodada_pendente": pendente,
        "aguardando": (
            ultima if ultima is not None and grupos[ultima][0]["papel"] == "usuario" else None
        ),
        # Só faz sentido avaliar o que já existe: uma conversa de zero turnos não
        # tem coerência entre turnos para medir.
        "avaliavel": any(linha["papel"] == "modelo" for linha in linhas),
    }


def _max_turnos() -> int:
    # Import local: `modelos` fala com a rede e este módulo não deveria arrastá-lo
    # para dentro de quem só quer ler o banco (a migração, por exemplo).
    from . import modelos as modmod

    return modmod.max_turnos()


def historico(conn: sqlite3.Connection, atribuicao_id: int) -> list[dict[str, str]]:
    """O histórico no formato do ``/api/chat`` — com a VENCEDORA de cada rodada.

    É aqui que "a conversa continua com a vencedora" acontece de verdade. Mandar
    as duas respostas faria o modelo ler a resposta do concorrente como se fosse
    dele; mandar sempre a "A" faria a escolha do anotador não mudar nada, e o
    duelo deixaria de ser multi-turno para virar dois A/B independentes.

    Uma rodada sem vencedora **para o histórico** ali: continuar sem ela seria
    inventar uma escolha que ninguém fez.
    """
    grupos = _por_ordem(_linhas(conn, atribuicao_id))
    saida: list[dict[str, str]] = []
    for ordem in sorted(grupos):
        do_grupo = grupos[ordem]
        if do_grupo[0]["papel"] == "usuario":
            saida.append({"role": "user", "content": str(do_grupo[0]["texto"])})
            continue
        escolhida = next((x for x in do_grupo if int(x["escolhida"])), None)
        if escolhida is None:
            if len(do_grupo) > 1:
                break
            escolhida = do_grupo[0]
        saida.append({"role": "assistant", "content": str(escolhida["texto"])})
    return saida


# ---------------------------------------------------------------------------
# escrita
# ---------------------------------------------------------------------------


def proxima_ordem(conn: sqlite3.Connection, atribuicao_id: int) -> int:
    linha = conn.execute(
        "SELECT COALESCE(max(ordem), -1) AS o FROM turnos_conversa WHERE atribuicao_id = ?",
        (atribuicao_id,),
    ).fetchone()
    return int(linha["o"]) + 1


def gravar_usuario(conn: sqlite3.Connection, atribuicao_id: int, texto: str) -> int:
    """Grava o turno do humano e devolve a ``ordem`` dele."""
    ordem = proxima_ordem(conn, atribuicao_id)
    conn.execute(
        "INSERT INTO turnos_conversa (atribuicao_id, ordem, papel, rotulo, texto) "
        "VALUES (?, ?, 'usuario', ?, ?)",
        (atribuicao_id, ordem, SEM_ROTULO, texto),
    )
    return ordem


def gravar_modelo(
    conn: sqlite3.Connection,
    atribuicao_id: int,
    ordem: int,
    rotulo: str,
    resultado: dict[str, Any],
    *,
    escolhida: bool = False,
) -> None:
    """Grava a resposta de UM modelo. ``rotulo`` é ``""`` fora do duelo.

    ``escolhida`` já nasce verdadeira na conversa de um modelo só: não há rodada
    a decidir, e é essa resposta que o histórico do próximo turno leva.
    """
    conn.execute(
        "INSERT INTO turnos_conversa (atribuicao_id, ordem, papel, rotulo, texto, "
        "                             raciocinio, truncado, modelo, digest, escolhida, "
        "                             duracao_ms) "
        "VALUES (?, ?, 'modelo', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            atribuicao_id,
            ordem,
            rotulo,
            str(resultado["texto"]),
            str(resultado.get("raciocinio") or ""),
            int(bool(resultado.get("truncado"))),
            str(resultado.get("modelo") or ""),
            str(resultado.get("digest") or ""),
            int(bool(escolhida)),
            int(resultado.get("duracao_ms") or 0),
        ),
    )


def escolher(
    conn: sqlite3.Connection,
    atribuicao_id: int,
    ordem: int,
    rotulo: str,
    justificativa: str,
) -> None:
    """Marca a vencedora de uma rodada de duelo. Levanta ``ConversaInvalida`` se não dá.

    A justificativa vai na linha DA VENCEDORA e não numa tabela à parte: ela é
    sobre aquela escolha naquela rodada, e separá-la criaria uma segunda chave
    para manter em dia sem nada em troca.
    """
    do_grupo = conn.execute(
        "SELECT id, rotulo, escolhida FROM turnos_conversa "
        "WHERE atribuicao_id = ? AND ordem = ? AND papel = 'modelo' ORDER BY rotulo",
        (atribuicao_id, ordem),
    ).fetchall()
    if len(do_grupo) < 2:
        raise ConversaInvalida(
            f"a rodada {ordem} não é um duelo (tem {len(do_grupo)} resposta(s))"
        )
    if rotulo not in {str(x["rotulo"]) for x in do_grupo}:
        raise ConversaInvalida(f"rótulo {rotulo!r} não existe na rodada {ordem}")
    if any(int(x["escolhida"]) for x in do_grupo):
        # Reabrir uma escolha reescreveria o histórico de um turno que o modelo
        # seguinte JÁ leu: a conversa que aconteceu deixaria de ser a conversa
        # gravada. Quem se arrependeu devolve a tarefa e começa outra.
        raise ConversaInvalida(f"a rodada {ordem} já foi decidida")
    conn.execute(
        "UPDATE turnos_conversa SET escolhida = 1, justificativa = ? "
        "WHERE atribuicao_id = ? AND ordem = ? AND rotulo = ?",
        (justificativa, atribuicao_id, ordem, rotulo),
    )


def limpar_ordem(conn: sqlite3.Connection, atribuicao_id: int, ordem: int) -> int:
    """Apaga as respostas de MODELO de uma ``ordem``. Devolve quantas saíram.

    Existe por causa da meia rodada: no duelo, uma falha entre a primeira e a
    segunda geração deixaria um lado gravado (o ``sqlite3`` deste projeto é
    autocommit). Meia rodada não é dado — é um duelo com um lado só, e o
    ``estado`` a leria como completa. O turno do HUMANO não é tocado: ele é o
    que se está tentando responder de novo.
    """
    cur = conn.execute(
        "DELETE FROM turnos_conversa "
        "WHERE atribuicao_id = ? AND ordem = ? AND papel = 'modelo'",
        (atribuicao_id, ordem),
    )
    return int(cur.rowcount or 0)


def apagar(conn: sqlite3.Connection, atribuicao_id: int) -> int:
    """Apaga a conversa desta atribuição. Devolve quantas linhas saíram.

    Chamado quando o anotador ABANDONA a tarefa: a próxima pessoa (ou ele mesmo,
    pelo catálogo) precisa começar de uma conversa vazia. Sem isto, retomar uma
    tarefa devolvida herdaria os turnos de quem desistiu — e a rubrica seria
    aplicada a uma conversa que a pessoa não teve.
    """
    cur = conn.execute("DELETE FROM turnos_conversa WHERE atribuicao_id = ?", (atribuicao_id,))
    return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# o payload: montado pelo SERVIDOR, a partir do que ele mesmo gravou
# ---------------------------------------------------------------------------


def para_payload(
    conn: sqlite3.Connection, atribuicao_id: int, tipo: str
) -> dict[str, Any]:
    """``{turnos: [...]}`` (e ``rodadas`` no duelo) — **o que o cliente NÃO manda**.

    A submissão SOBRESCREVE estas chaves com o que sai daqui. Não é desconfiança
    genérica do cliente: é que o cliente **não sabe** qual modelo respondeu no
    duelo (esse é o ponto do rótulo cego), e um dado de preferência em que o
    "quem" veio de quem não sabia seria fabricação.

    ``turnos`` é a CONVERSA como ela aconteceu — no duelo, os turnos do humano
    mais a resposta vencedora de cada rodada. ``rodadas`` é o dado de
    PREFERÊNCIA: as duas respostas, qual venceu e por quê. São coisas diferentes
    e por isso são duas chaves: quem for treinar um modelo lê ``turnos``; quem
    for treinar um juiz lê ``rodadas``.
    """
    grupos = _por_ordem(_linhas(conn, atribuicao_id))
    fluxo: list[dict[str, Any]] = []
    rodadas: list[dict[str, Any]] = []

    for ordem in sorted(grupos):
        do_grupo = grupos[ordem]
        if do_grupo[0]["papel"] == "usuario":
            fluxo.append(
                {
                    "ordem": ordem,
                    "papel": "usuario",
                    "texto": str(do_grupo[0]["texto"]),
                    "criado_em": do_grupo[0]["criado_em"],
                }
            )
            continue
        escolhida = next((x for x in do_grupo if int(x["escolhida"])), None) or do_grupo[0]
        fluxo.append(
            {
                "ordem": ordem,
                "papel": "modelo",
                "texto": str(escolhida["texto"]),
                "raciocinio": str(escolhida["raciocinio"]),
                "truncado": bool(escolhida["truncado"]),
                # QUEM RESPONDEU, com nome e digest. Sem isto o dado não vale
                # nada daqui a seis meses — é a razão de esta coluna existir.
                "modelo": str(escolhida["modelo"]),
                "digest": str(escolhida["digest"]),
                "duracao_ms": int(escolhida["duracao_ms"]),
                "criado_em": escolhida["criado_em"],
            }
        )
        if len(do_grupo) > 1:
            rodadas.append(
                {
                    "ordem": ordem,
                    "respostas": [
                        {
                            "rotulo": str(x["rotulo"]),
                            "texto": str(x["texto"]),
                            "raciocinio": str(x["raciocinio"]),
                            "truncado": bool(x["truncado"]),
                            "modelo": str(x["modelo"]),
                            "digest": str(x["digest"]),
                            "duracao_ms": int(x["duracao_ms"]),
                        }
                        for x in do_grupo
                    ],
                    "vencedora": str(escolhida["rotulo"]),
                    "justificativa": str(escolhida["justificativa"] or ""),
                }
            )

    saida: dict[str, Any] = {"turnos": fluxo}
    if tipo == "duelo_modelos":
        saida["rodadas"] = rodadas
    return saida


def revelacao(conn: sqlite3.Connection, atribuicao_id: int) -> list[dict[str, Any]]:
    """QUEM ERA A E QUEM ERA B, por rodada. **Só depois do envio.**

    É a única saída do mapa cego, e ela existe porque o anotador merece saber o
    que acabou de julgar — e porque um duelo que nunca revela é um teste, não uma
    ferramenta de trabalho. A rota que a chama é a de submissão, depois de a
    anotação estar gravada.
    """
    grupos = _por_ordem(_linhas(conn, atribuicao_id))
    saida: list[dict[str, Any]] = []
    for ordem in sorted(grupos):
        do_grupo = grupos[ordem]
        if do_grupo[0]["papel"] != "modelo" or len(do_grupo) < 2:
            continue
        escolhida = next((x for x in do_grupo if int(x["escolhida"])), None)
        saida.append(
            {
                "ordem": ordem,
                "lados": [
                    {
                        "rotulo": str(x["rotulo"]),
                        "modelo": str(x["modelo"]),
                        "digest": str(x["digest"])[:12],
                    }
                    for x in do_grupo
                ],
                "vencedora": None if escolhida is None else str(escolhida["rotulo"]),
                "modelo_vencedor": None if escolhida is None else str(escolhida["modelo"]),
            }
        )
    return saida


__all__ = [
    "ARQUIVO_RUBRICA",
    "ID_RUBRICA",
    "PAPEL_OLLAMA",
    "SEM_ROTULO",
    "ConversaInvalida",
    "aguardando_modelo",
    "apagar",
    "e_cego",
    "escolher",
    "estado",
    "gravar_modelo",
    "gravar_usuario",
    "historico",
    "limpar_ordem",
    "nomes_dos_criterios",
    "para_payload",
    "proxima_ordem",
    "revelacao",
    "rubrica",
    "turnos",
]
