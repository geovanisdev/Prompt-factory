"""Agreement entre anotadores — a medida que o parecer disse que estava errada.

O PLANO ORIGINAL PEDIA A COISA ERRADA, E O PARECER PEGOU
========================================================
O marco do painel do admin prometia "agreement **só em campos categóricos**",
por igualdade exata. É o instrumento certo para a campanha de rotulagem do M5,
onde o que se compara é ``task_type`` contra ``task_type``. Só que o tipo de
tarefa DOMINANTE desta plataforma é ``avaliar_rubrica``, e a resposta dele é uma
**escala ancorada**: igualdade exata trataria "3 contra 4" e "1 contra 5" como o
mesmo erro, num instrumento em que a segunda discordância é quatro vezes a
primeira. Um número assim não é impreciso — ele é enganoso na direção pessimista,
e uma equipe que quase concorda apareceria como uma equipe que não concorda.

Daí o peso **quadrático** sobre a escala declarada: a penalidade cresce com o
quadrado da distância, que é a convenção para escala ordenada e a que os
avaliadores de anotação reconhecem.

Medido, com 12 pares numa escala 1..5 espalhados pela escala, oito exatos e
quatro errando por um degrau: **ponderado 0,894 · igualdade exata 0,571**. É a
mesma equipe e o mesmo trabalho. O primeiro número descreve o que aconteceu
(erram por um degrau, e um degrau em cinco posições é pouco); o segundo
descreveria uma equipe com problema de calibração. O plano original teria
publicado o segundo.

O PARADOXO DO KAPPA, MEDIDO — E POR QUE AS DUAS COLUNAS ANDAM JUNTAS
====================================================================
Com 12 pares em que as duas pessoas SEMPRE ficam a um degrau de distância
(3 contra 4, sempre), a concordância observada ponderada é **0,9375** e o kappa
é **-1,0**. Os dois números estão certos e respondem perguntas diferentes: elas
concordam quase sempre em valor, e **nunca** concordam mais do que o acaso já
daria — porque, usando só duas posições da escala, o acaso sozinho produziria
0,9688. Isso é discordância SISTEMÁTICA, e o kappa existe para vê-la.

Consequência de desenho, e ela não é negociável: **``observada`` e ``kappa``
saem sempre juntos, e a tela nunca mostra um sem o outro**. Um painel com
"-1,0" sozinho faria alguém refazer um lote que está bom; um com "94%" sozinho
esconderia um viés real entre dois anotadores.

QUAL É O INSTRUMENTO, COM NOME
==============================
Kappa de Cohen pressupõe **dois avaliadores fixos**, cada um com as próprias
marginais. Aqui os avaliadores VARIAM: cada par sai de uma tarefa, e a tarefa
seguinte pode ter outras duas pessoas. Para esse caso a família correta é a de
marginais **agrupadas** — o pi de Scott no caso nominal, e o alpha de
Krippendorff sem a correção de amostra pequena no caso ponderado.

É exatamente o que ``_pi`` calcula: cada valor observado entra na marginal uma
vez, venha do lado que vier, e a esperança por acaso sai dessa distribuição
única. Quando as marginais dos dois lados coincidem, o número é idêntico ao
kappa de Cohen — então nada se perde e a suposição falsa não é feita. O campo
``instrumento`` de cada linha diz qual foi usado, porque um número de agreement
sem o nome do instrumento não é conferível.

TRÊS DECISÕES QUE MUDAM O NÚMERO
================================
1. **O payload é o COMO SUBMETIDO, nunca o corrigido.** ``avaliacoes`` guarda a
   versão que o Rate and Review consertou; medir por ela mediria o quanto o
   revisor convergiu as duas pessoas, que é outra pergunta — e uma cuja resposta
   é sempre lisonjeira. Agreement é sobre o que os anotadores produziram
   sozinhos.
2. **Sintética fica de fora.** O mesmo sinal único de sempre
   (``gabarito_avaliacao_json IS NOT NULL``): uma anotação sintética foi gerada
   para carregar um defeito plantado. Concordar com ela não é virtude e
   discordar não é falha — incluí-la faria a medida responder a quantas
   sintéticas a campanha gerou.
3. **N/A não é um ponto da escala.** Um critério que alguém marcou "não se
   aplica" sai do kappa numérico e vira uma linha binária própria ("as duas
   pessoas concordam sobre o critério se aplicar?"). Empurrá-lo para dentro da
   escala exigiria escolher um número para ele, que é a sentinela numérica que
   ``NotaCriterio`` existe para não ter.

E A CATEGORIA FECHA O LAÇO
==========================
As perguntas de categoria do P9 (``task_type``/``domain``, vocabulário da
taxonomia do corpus) entram aqui como linhas **nominais** — e essa é a mesma
medida que a campanha de rotulagem do corpus vai precisar. Duas pessoas
classificando o mesmo prompt é, literalmente, o instrumento do M6, agora
produzido pelo trabalho da plataforma.
"""

from __future__ import annotations

import json
import sqlite3
from itertools import combinations
from typing import Any

from ..config import get as _cfg
from . import payloads as pmod
from . import tarefas as tmod

#: Anotação que foi **descartada** não entra: aquele trabalho foi jogado fora, e
#: medir concordância contra ele mediria uma discordância que a plataforma já
#: resolveu. Todo o resto entra — inclusive ``devolvida``, que é trabalho
#: submetido de verdade: agreement é sobre o que as pessoas produziram, e filtrar
#: pelo que a revisão aprovou mediria a revisão.
STATUS_FORA: tuple[str, ...] = ("descartada",)

#: Os pesos disponíveis, por nome. ``quadratico`` para escala ancorada (a
#: distância importa e cresce rápido); ``nominal`` para vocabulário sem ordem
#: (preferência A/B, classe da taxonomia), onde "perto" não significa nada.
PESOS: tuple[str, ...] = ("nominal", "linear", "quadratico")

#: Por que um kappa não pôde ser publicado. Chaves, e não frases: a tela fala
#: duas línguas (mesma disciplina de ``tarefas.CHAVE_MOTIVO_*``).
MOTIVO_POUCOS = "poucos_pares"
MOTIVO_SEM_VARIANCIA = "sem_variancia"


def piso_de_pares() -> int:
    return int(_cfg("annotate", "agreement_min_pares", default=10))


# ---------------------------------------------------------------------------
# a matemática, pura
# ---------------------------------------------------------------------------


def _matriz_de_pesos(k: int, pesos: str) -> list[list[float]]:
    """``w[i][j]`` = **quanto conta como concordância** ficar em i e j.

    1 na diagonal, e cai com a distância. Normalizar por ``k - 1`` (a amplitude
    da escala DECLARADA, não a observada) é o que faz um degrau numa escala de 3
    valer mais que um degrau numa de 5 — que é a verdade: numa escala de três
    posições, um degrau é metade do instrumento.
    """
    if pesos == "nominal" or k < 2:
        return [[1.0 if i == j else 0.0 for j in range(k)] for i in range(k)]
    span = float(k - 1)
    expoente = 2.0 if pesos == "quadratico" else 1.0
    return [
        [1.0 - (abs(i - j) / span) ** expoente for j in range(k)] for i in range(k)
    ]


def _pi(
    pares: list[tuple[Any, Any]], categorias: list[Any], pesos: str
) -> dict[str, Any]:
    """A conta. Devolve ``observada`` sempre e ``kappa`` quando ele existe.

    ``observada`` é DESCRITIVA — "com que frequência as duas disseram o mesmo",
    ponderada pela distância. Ela é um fato sobre os pares e vale com um par só.

    ``kappa`` é INFERENCIAL: desconta o que sairia por acaso, e o acaso é
    estimado das marginais. Com poucos pares essa estimativa é ruído, e é por
    isso que o piso existe — ver ``[annotate] agreement_min_pares``.

    O CASO DEGENERADO TEM NOME, E NÃO É ZERO
    ========================================
    Quando todos os valores caem numa categoria só, a esperança por acaso é 1 e
    a fórmula vira 0/0. É o paradoxo clássico do kappa, e as duas saídas
    preguiçosas mentem: ``1.0`` afirmaria concordância perfeita onde não houve
    escolha nenhuma a fazer, e ``0.0`` afirmaria concordância ao nível do acaso —
    a acusação mais forte que este número sabe fazer — justamente quando as duas
    pessoas concordaram em tudo. Sai ``None`` com ``sem_variancia``.
    """
    n = len(pares)
    indice = {c: i for i, c in enumerate(categorias)}
    k = len(categorias)
    w = _matriz_de_pesos(k, pesos)

    # MARGINAIS AGRUPADAS: cada valor entra uma vez, de qualquer lado. É o que
    # torna a medida simétrica sem inventar um "avaliador 1" — ver o cabeçalho.
    contagem = [0.0] * k
    for a, b in pares:
        contagem[indice[a]] += 1
        contagem[indice[b]] += 1
    p = [c / (2 * n) for c in contagem] if n else [0.0] * k

    observada = sum(w[indice[a]][indice[b]] for a, b in pares) / n if n else 0.0
    esperada = sum(w[i][j] * p[i] * p[j] for i in range(k) for j in range(k))

    saida: dict[str, Any] = {
        "n_pares": n,
        "observada": round(observada, 4),
        "esperada": round(esperada, 4),
        "kappa": None,
        "motivo": None,
        "instrumento": f"pi_{pesos}",
    }
    if n < piso_de_pares():
        saida["motivo"] = MOTIVO_POUCOS
        return saida
    if esperada >= 1.0:
        saida["motivo"] = MOTIVO_SEM_VARIANCIA
        return saida
    saida["kappa"] = round((observada - esperada) / (1.0 - esperada), 4)
    return saida


def medir(
    rotulo: str,
    pares: list[tuple[Any, Any]],
    categorias: list[Any],
    pesos: str,
    **extra: Any,
) -> dict[str, Any] | None:
    """Uma linha do relatório, ou ``None`` quando não houve par nenhum.

    Linha sem par não é linha com zero: ela é a ausência de qualquer par para
    comparar, e desenhá-la com ``0`` no lugar do kappa é exatamente a confusão
    que o ``sem_variancia`` acima evita na outra ponta.
    """
    if not pares or len(categorias) < 2:
        return None
    return {"campo": rotulo, "categorias": len(categorias), **_pi(pares, categorias, pesos), **extra}


# ---------------------------------------------------------------------------
# os pares: quem comparou o quê com quem
# ---------------------------------------------------------------------------


_SQL_SUBMETIDAS = """
SELECT an.id, an.versao, an.payload_json, an.status,
       a.anotador_id, au.nome AS anotador,
       t.id AS tarefa_id, t.tipo, t.prompt_uid,
       an.gabarito_avaliacao_json AS gabarito,
       pj.nome AS projeto
FROM anotacoes an
JOIN atribuicoes a ON a.id = an.atribuicao_id
JOIN anotadores au ON au.id = a.anotador_id
JOIN tarefas t     ON t.id = a.tarefa_id
LEFT JOIN projetos pj ON pj.id = t.projeto_id
WHERE an.status NOT IN ({vagas})
ORDER BY t.id, a.anotador_id, an.versao
"""


def coletar(
    conn: sqlite3.Connection, projeto: str | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """As tarefas com **duas ou mais pessoas**, cada uma na versão mais nova.

    ``ORDER BY … an.versao`` + sobrescrita no dicionário é o que deixa a última
    versão de cada pessoa: quem foi devolvido e refez conta com o re-trabalho, e
    não com a submissão que a triagem recusou. Comparar a v1 de um com a v2 de
    outro mediria a revisão de um contra o trabalho do outro.
    """
    sql = _SQL_SUBMETIDAS.format(vagas=",".join("?" * len(STATUS_FORA)))
    por_tarefa: dict[int, dict[int, dict[str, Any]]] = {}
    sinteticas = 0

    for linha in conn.execute(sql, STATUS_FORA).fetchall():
        if projeto is not None and str(linha["projeto"] or "") != projeto:
            continue
        if linha["gabarito"] is not None:
            sinteticas += 1
            continue
        try:
            payload = json.loads(str(linha["payload_json"] or "{}"))
        except ValueError:  # pragma: no cover - JSON nosso, sempre válido
            continue
        if not isinstance(payload, dict):  # pragma: no cover
            continue
        por_tarefa.setdefault(int(linha["tarefa_id"]), {})[int(linha["anotador_id"])] = {
            "anotacao_id": int(linha["id"]),
            "anotador_id": int(linha["anotador_id"]),
            "anotador": str(linha["anotador"]),
            "tipo": str(linha["tipo"]),
            "prompt_uid": str(linha["prompt_uid"]),
            "payload": payload,
        }

    comparaveis = [
        {"tarefa_id": tid, "tipo": next(iter(pessoas.values()))["tipo"], "pessoas": list(pessoas.values())}
        for tid, pessoas in sorted(por_tarefa.items())
        if len(pessoas) >= 2
    ]
    resumo = {
        "tarefas_com_2_ou_mais": len(comparaveis),
        "tarefas_com_1": sum(1 for p in por_tarefa.values() if len(p) == 1),
        "anotadores_distintos": len({q["anotador_id"] for p in por_tarefa.values() for q in p.values()}),
        "sinteticas_excluidas": sinteticas,
    }
    return comparaveis, resumo


def _notas_por_criterio(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    saida = {}
    for nota in payload.get("notas") or []:
        if isinstance(nota, dict) and nota.get("criterio"):
            saida[str(nota["criterio"])] = nota
    return saida


def _escala(rubrica: dict[str, Any] | None, criterio: str) -> tuple[int, int] | None:
    """A escala DAQUELE critério, da rubrica que as duas pessoas viram.

    ``None`` quando a rubrica não declarou escala (``escala_declarada = False``
    de ``tarefas.normalizar_criterio``): sem amplitude declarada não há como
    normalizar a distância, e inventar 1..9 aqui daria pesos que nenhuma das
    duas pessoas viu na tela. A linha simplesmente não é medida — inventar em
    silêncio é o defeito que o ``routes_revisao:243`` custou meses para revelar.
    """
    for c in (rubrica or {}).get("criterios") or []:
        if str(c.get("nome")) != criterio:
            continue
        if not c.get("escala_declarada"):
            return None
        escala = c.get("escala") or {}
        return int(escala.get("min", 1)), int(escala.get("max", 5))
    return None


def relatorio(
    conn: sqlite3.Connection, projeto: str | None = None
) -> dict[str, Any]:
    """As linhas de agreement, com o resumo da população que as produziu.

    As escalas NÃO são agrupadas numa linha só, e isso é decisão. Um par vindo
    de uma rubrica 1..5 e um par vindo do instrumento de severidade 1..3 não
    cabem na mesma matriz: ou se inventam categorias que ninguém podia escolher,
    ou se achata a distância entre as que existem. Sai **uma linha por escala**,
    com a escala no rótulo — e o painel mostra as duas em vez de uma média que
    não corresponde a instrumento nenhum.
    """
    comparaveis, resumo = coletar(conn, projeto)

    # {(min,max): [(a,b)]} para as notas; o resto acumula direto.
    notas_por_escala: dict[tuple[int, int], list[tuple[Any, Any]]] = {}
    na_pares: list[tuple[Any, Any]] = []
    issue_pares: list[tuple[Any, Any]] = []
    categoria_pares: dict[str, dict[str, Any]] = {}
    preferencia_pares: list[tuple[Any, Any]] = []

    for tarefa in comparaveis:
        rubrica = (
            tmod.rubrica_ativa(conn, tarefa["pessoas"][0]["prompt_uid"])
            if tarefa["tipo"] == "avaliar_rubrica"
            else None
        )
        perguntas = {p["id"]: p for p in tmod.perguntas_da_rubrica(rubrica)}

        for um, outro in combinations(tarefa["pessoas"], 2):
            if tarefa["tipo"] == "comparar_ab":
                a, b = um["payload"].get("preferencia"), outro["payload"].get("preferencia")
                if a in pmod.ESCOLHAS_AB and b in pmod.ESCOLHAS_AB:
                    preferencia_pares.append((a, b))
                continue
            if tarefa["tipo"] != "avaliar_rubrica":
                continue

            n1, n2 = _notas_por_criterio(um["payload"]), _notas_por_criterio(outro["payload"])
            for criterio in sorted(set(n1) & set(n2)):
                d1, d2 = n1[criterio], n2[criterio]
                na1, na2 = bool(d1.get("nao_aplicavel")), bool(d2.get("nao_aplicavel"))
                na_pares.append((na1, na2))
                # N/A sai do numérico: ele não é um ponto da escala, e é
                # justamente por isso que ele é campo próprio no contrato.
                if na1 or na2:
                    continue
                faixa = _escala(rubrica, criterio)
                v1, v2 = d1.get("nota"), d2.get("nota")
                if faixa is None or not isinstance(v1, int) or not isinstance(v2, int):
                    continue
                if faixa[0] <= v1 <= faixa[1] and faixa[0] <= v2 <= faixa[1]:
                    notas_por_escala.setdefault(faixa, []).append((v1, v2))

                t1, t2 = d1.get("tipos_issue") or {}, d2.get("tipos_issue") or {}
                for chave in sorted(set(t1) & set(t2)):
                    issue_pares.append((bool(t1[chave]), bool(t2[chave])))

            r1, r2 = um["payload"].get("respostas") or {}, outro["payload"].get("respostas") or {}
            for qid, pergunta in perguntas.items():
                if pergunta.get("tipo") != "categoria":
                    continue
                validas = [str(o.get("id")) for o in pergunta.get("opcoes") or []]
                a, b = str(r1.get(qid) or ""), str(r2.get(qid) or "")
                if a in validas and b in validas:
                    alvo = categoria_pares.setdefault(
                        qid, {"pergunta": pergunta, "pares": []}
                    )
                    alvo["pares"].append((a, b))

    linhas: list[dict[str, Any]] = []
    for (piso, teto), pares in sorted(notas_por_escala.items()):
        linha = medir(
            f"avaliar_rubrica.notas[{piso}..{teto}]",
            pares,
            list(range(piso, teto + 1)),
            "quadratico",
            escala={"min": piso, "max": teto},
            chave_i18n="agr.notas",
        )
        if linha:
            linhas.append(linha)
    for rotulo, pares, categorias, chave in (
        ("avaliar_rubrica.nao_aplicavel", na_pares, [False, True], "agr.na"),
        ("avaliar_rubrica.tipos_issue", issue_pares, [False, True], "agr.issue"),
        (
            "comparar_ab.preferencia",
            preferencia_pares,
            list(pmod.ESCOLHAS_AB),
            "agr.preferencia",
        ),
    ):
        linha = medir(rotulo, pares, categorias, "nominal", chave_i18n=chave)
        if linha:
            linhas.append(linha)
    for qid, bloco in sorted(categoria_pares.items()):
        pergunta = bloco["pergunta"]
        linha = medir(
            f"avaliar_rubrica.respostas.{qid}",
            bloco["pares"],
            [str(o.get("id")) for o in pergunta.get("opcoes") or []],
            "nominal",
            chave_i18n="agr.categoria",
            rotulo_pergunta=pergunta.get("rotulo"),
            rotulo_pergunta_i18n=pergunta.get("rotulo_i18n"),
            vocabulario=pergunta.get("vocabulario"),
        )
        if linha:
            linhas.append(linha)

    return {
        "linhas": linhas,
        "resumo": resumo,
        "piso_pares": piso_de_pares(),
        "projeto": projeto,
    }


__all__ = [
    "MOTIVO_POUCOS",
    "MOTIVO_SEM_VARIANCIA",
    "PESOS",
    "STATUS_FORA",
    "coletar",
    "medir",
    "piso_de_pares",
    "relatorio",
]
