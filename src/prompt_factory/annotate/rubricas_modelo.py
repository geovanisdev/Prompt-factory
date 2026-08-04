"""O banco de modelos de rubrica — o primeiro pedaço do arsenal do admin.

POR QUE ISTO SAIU DO JAVASCRIPT
===============================
Até o P8, os três modelos de partida da aba ``escrever_rubrica`` eram literais
dentro de ``static/index.html``: um array ``MODELOS_RUBRICA`` com nome, título e
critérios escritos à mão em JS. Funcionava, e escondia o problema — **um quarto
modelo exigia editar a tela**. O construtor do P5a existe justamente para que
quem monta a tarefa escolha blocos e escreva perguntas sem tocar em código, e um
banco de perguntas que vive dentro do renderizador é o oposto disso.

Aqui eles são DADO: um arquivo de fixture, servido por ``/api/modelos-rubrica``,
lido pela tela. Quando o P5a chegar, ele ganha uma tabela e passa a aceitar
modelos escritos pelo admin — e o acréscimo é barato justamente porque a
fronteira já está aqui: a tela nunca soube de onde eles vinham.

**Uma tabela nova NÃO custa migração**: ``CREATE TABLE IF NOT EXISTS`` a cria num
banco existente. O que custou a v4 foi mudar um CHECK de tabela que já existia.
Por isso não se paga por ela agora.

A CONVENÇÃO DE LÍNGUA, QUE É O MOTIVO DE `nome_chave` EXISTIR
=============================================================
O CONTEÚDO do modelo (título, critério, âncora) é **inglês nas duas línguas da
interface**: um critério de rubrica é metadado, e metadado sai em inglês porque
é o que o cliente lê. Oferecer um modelo em português produziria, a cada clique,
uma rubrica que nasce violando a convenção que a tela declara ao lado do campo.

O que É cromo — o rótulo do botão que aplica o modelo — continua no dicionário
``TEXTOS``, e o modelo o referencia por ``nome_chave``. É a mesma fronteira que
vale para toda a plataforma, e ela precisa estar dita aqui porque é aqui que ela
será testada quando o admin puder escrever o próprio modelo: **rótulo escrito
por admin é dado, e vai com sidecar ``*_i18n``; rótulo nosso é cromo, e vai para
o dicionário.**
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Onde a fixture mora. Dentro do PACOTE, como o ``demo_pack.json`` e o
#: ``diretrizes.json``: ``data/`` é gitignorado, e um banco de perguntas que não
#: viaja com o código não existe num clone limpo.
ARQUIVO = Path(__file__).with_name("fixtures") / "modelos_rubrica.json"

SCHEMA = "modelos_rubrica@1"

#: Escala padrão de um modelo que não declara a própria. 1..5 é o que os três
#: modelos originais usavam, e mudá-los aqui mudaria rubricas que já existem.
ESCALA_PADRAO = (1, 5)


class ModeloInvalido(RuntimeError):
    """A fixture está torta. Falha ALTO: um modelo pela metade vira uma rubrica
    pela metade, e ninguém descobre até alguém tentar aplicar uma nota nela."""


#: Os sidecares de exibição que atravessam da fixture para a rubrica. Copiar por
#: LISTA e não por "tudo que termina em ``_i18n``" é de propósito: o que não está
#: aqui é campo que ninguém desenha, e carregá-lo só faria o blob crescer.
SIDECARES_CRITERIO = ("nome_i18n", "descricao_i18n", "grupo_i18n")


def _ancoras(fonte: dict[str, Any], piso: int, teto: int) -> list[dict[str, Any]]:
    """A régua do critério, nas duas formas que a fixture pode declarar.

    **Forma completa** (``ancoras: [{valor, rotulo, rotulo_i18n, exemplo, …}]``)
    é a canônica de ``rubrica@3`` e sai daqui verbatim: é ela que carrega a
    tradução e o exemplo que o ⓘ abre.

    **Forma curta** (``rotulo_min``/``rotulo_meio``/``rotulo_max``) existe para
    os modelos de partida escritos à mão, onde três chaves dizem tudo.
    ``rotulo_meio`` só é lido numa escala de três pontos, e é o campo que faz o
    instrumento de severidade valer: numa escala de 3, **todo o desacordo entre
    anotadores mora no ponto do meio**, e uma rubrica que ancora só as pontas
    deixa mudo justamente o ponto que precisava falar.
    """
    explicitas = fonte.get("ancoras")
    if isinstance(explicitas, list) and explicitas:
        return [{**a, "valor": int(a["valor"])} for a in explicitas]
    saida = []
    if fonte.get("rotulo_min"):
        saida.append({"valor": piso, "rotulo": str(fonte["rotulo_min"])})
    if fonte.get("rotulo_meio") and teto - piso == 2:
        saida.append({"valor": piso + 1, "rotulo": str(fonte["rotulo_meio"])})
    if fonte.get("rotulo_max"):
        saida.append({"valor": teto, "rotulo": str(fonte["rotulo_max"])})
    return saida


def _tipos_issue(bruto: Any) -> list[dict[str, Any]]:
    """O catálogo, aceitando a forma curta (uma string por tipo).

    O ``id`` é a IDENTIDADE — é ele que ``payload_json`` grava em
    ``notas[].tipos_issue`` — e por isso nunca é traduzido. Na forma curta ele
    nasce igual ao rótulo, e só diverge quando alguém quiser traduzir.
    """
    return [
        {"id": str(t), "rotulo": str(t)}
        if not isinstance(t, dict)
        else {**t, "id": str(t.get("id") or t.get("rotulo") or "")}
        for t in bruto
    ]


def _criterio(bruto: Any, piso: int, teto: int, padrao: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(bruto, dict) or not bruto.get("nome"):
        raise ModeloInvalido(f"critério sem nome: {bruto!r}")
    c: dict[str, Any] = {
        "nome": str(bruto["nome"]),
        "descricao": str(bruto.get("descricao") or ""),
        "escala": {
            "min": int(bruto.get("escala_min") or piso),
            "max": int(bruto.get("escala_max") or teto),
            "ancoras": [],
        },
    }
    # As âncoras HERDAM do modelo, e a herança é do CONJUNTO inteiro, não chave a
    # chave: uma régua é uma régua, não se herda metade dela. Num instrumento de
    # severidade elas são declaradas UMA vez porque significam a mesma coisa em
    # todo critério — e é exatamente isso que autoriza a matriz a escrever um
    # cabeçalho de coluna por grupo em vez de repeti-las em cada linha. O
    # critério ainda pode sobrescrevê-las (o overall tem a régua dele), e aí a
    # matriz percebe que a escala deixou de ser uniforme e não compartilha nada.
    propria = any(
        bruto.get(chave) for chave in ("ancoras", "rotulo_min", "rotulo_meio", "rotulo_max")
    )
    c["escala"]["ancoras"] = _ancoras(
        bruto if propria else padrao, c["escala"]["min"], c["escala"]["max"]
    )
    if bruto.get("grupo"):
        c["grupo"] = str(bruto["grupo"])
    if bruto.get("resumo"):
        c["resumo"] = True
    if bruto.get("tipos_issue"):
        c["tipos_issue"] = _tipos_issue(bruto["tipos_issue"])
    for chave in SIDECARES_CRITERIO:
        if bruto.get(chave):
            c[chave] = bruto[chave]
    return c


def carregar(caminho: Path | None = None) -> list[dict[str, Any]]:
    """Os modelos, já na forma canônica de ``rubrica@3``.

    A normalização acontece AQUI e não na tela, para que o JS receba exatamente
    a mesma forma que ``tarefas.rubrica_ativa`` devolve. Duas formas para a mesma
    coisa era o defeito que o ``routes_revisao:243`` custou meses para revelar.
    """
    # Import tardio: `tarefas` puxa `conversa` e `pool`, e quem só quer listar
    # os modelos não deve pagar por isso.
    from .tarefas import normalizar_criterio

    dados = json.loads((caminho or ARQUIVO).read_text(encoding="utf-8"))
    if dados.get("schema") != SCHEMA:
        raise ModeloInvalido(f"esperava {SCHEMA}, veio {dados.get('schema')!r}")
    saida = []
    for m in dados.get("modelos") or []:
        piso = int(m.get("escala_min") or ESCALA_PADRAO[0])
        teto = int(m.get("escala_max") or ESCALA_PADRAO[1])
        if teto - piso < 2:
            raise ModeloInvalido(
                f"{m.get('id')!r}: escala de {piso} a {teto} não tem três posições — "
                "uma escala de duas é um checkbox com nome de escala"
            )
        criterios = [_criterio(c, piso, teto, m) for c in (m.get("criterios") or [])]
        if not criterios:
            raise ModeloInvalido(f"{m.get('id')!r}: modelo sem critério nenhum")
        # O FORMULÁRIO DO ANOTADOR não sabe expressar grupo, catálogo de tipos
        # de issue nem overall: `escrever_rubrica@1` tem nome, descrição, escala
        # e as duas pontas, e mais nada. Um modelo que carrega esses campos e é
        # oferecido ali seria aplicado PERDENDO-OS, em silêncio — a mesma classe
        # de defeito que o `routes_revisao:243` custou meses para revelar.
        #
        # Então o modelo declara o que ele é: o instrumento completo fica para o
        # construtor do admin (P5a), que edita a rubrica montada diretamente. A
        # tela do anotador filtra por esta flag em vez de oferecer e mutilar.
        completo = not any(
            c.get("grupo") or c.get("resumo") or c.get("tipos_issue") for c in criterios
        )
        modelo: dict[str, Any] = {
            "id": str(m.get("id") or ""),
            "nome_chave": str(m.get("nome_chave") or ""),
            "titulo": str(m.get("titulo") or ""),
            "cabe_no_formulario": completo,
            "criterios": [normalizar_criterio(c) for c in criterios],
        }
        # O sidecar do título só existe onde ele será MATERIALIZADO. Ver o
        # comentário de `completo` acima e o `_nota` da fixture: o formulário
        # descarta sidecar por construção.
        if m.get("titulo_i18n"):
            modelo["titulo_i18n"] = m["titulo_i18n"]
        saida.append(modelo)
    if not saida:
        raise ModeloInvalido("nenhum modelo na fixture")
    return saida


__all__ = ["ARQUIVO", "ESCALA_PADRAO", "SCHEMA", "ModeloInvalido", "carregar"]
