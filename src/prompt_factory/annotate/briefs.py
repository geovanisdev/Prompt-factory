"""O BRIEF DO PROJETO — o enquadramento sob o qual cada anotação foi feita.

DUAS INSTRUÇÕES, DOIS EIXOS, DUAS TABELAS
=========================================
``diretrizes`` descreve a **mecânica de um tipo de tarefa** ("no A/B você escolhe
um; empate só quando…") e vale em qualquer projeto. O brief descreve o
**projeto**: para que serve o dado, que prompts aparecem, como se escolhe um, e o
que este cliente espera de um rating e de uma justificativa.

Os dois mudam sozinhos — dá para apertar a regra da justificativa deste cliente
sem tocar na mecânica do A/B, e vice-versa. Uma tabela só obrigaria a subir as
duas versões junto, e aí ``versao_diretriz`` deixaria de significar o que
significa. Daí ``anotacoes`` carregar **dois** ponteiros.

POR QUE ISTO PRECISOU EXISTIR
=============================
As telas de tarefa da plataforma diziam o COMO e não diziam o QUÊ: nenhuma
superfície respondia "que projeto é este, que prompts ele cobre, e o que conta
como uma justificativa aceitável aqui". Num trabalho real essas respostas estão
no topo de toda tarefa, e a inconsistência entre elas é uma das causas comuns de
lote devolvido — porque cada anotador lê uma versão diferente da mesma regra.

A cura não é escrever o texto em seis lugares. É **um objeto, versionado**, do
qual as seis superfícies são projeções.

O ESCOPO NÃO É PROSA
====================
``escopo`` fica FORA de ``textos`` porque ele é feito de ids da taxonomia do
corpus (``labeling/taxonomy.json``), e id não se traduz. É exatamente a fronteira
que separa o campo canônico de um critério do sidecar ``*_i18n`` dele — e é o que
permite ao brief dizer "``conversa-social`` está fora" numa língua só e a tela
desenhar a lista nas duas.

E é o que fecha o laço com o corpus: ``task_type`` e ``domain`` estão **nulos**
em 100% das linhas do banco de prompts. Um brief que declara escopo em termos da
taxonomia é o que faz o trabalho de anotação PRODUZIR o rótulo que falta, em vez
de produzir um JSONL que fica num diretório.

A LÍNGUA É DIMENSÃO DO TEXTO, NÃO DA VERSÃO
============================================
Mesma decisão do ``diretrizes@2``, e pelas mesmas três razões: publicar a v2 só
em inglês fica estruturalmente impossível, ``versao_brief`` não ganha um segundo
ponteiro, e a tela troca de idioma sem uma segunda ida ao servidor.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import projetos as projmod

#: O arquivo versionado, dentro do pacote (``data/`` é gitignorado).
ARQUIVO = Path(__file__).resolve().parent / "fixtures" / "briefs.json"

#: Contrato do ``texto_json`` da tabela ``briefs``.
SCHEMA_BRIEF = "briefs@1"

#: As línguas, na ordem de precedência. O inglês é a FONTE.
IDIOMAS: tuple[str, ...] = ("en", "pt")

#: As seções do brief, na ordem em que a tela as desenha. As chaves são pt-BR
#: como todo o schema desta app; os RÓTULOS que a pessoa lê vêm do dicionário
#: ``TEXTOS`` da tela, porque rótulo é cromo.
#:
#: São exatamente as superfícies que faltavam: visão geral da tarefa, caso de
#: uso, que tipos de prompt aparecem, como se escolhe ou se cria um, e o resumo
#: de como avaliar e como justificar.
SECOES: tuple[str, ...] = (
    "visao_geral",
    "caso_de_uso",
    "tipos_de_prompt",
    "selecao",
    "avaliacao",
)

#: Os papéis de projeto que o arquivo pode descrever. Chaveado como
#: ``projetos.DESCRICOES``, e **não pelo nome do projeto**: o nome sai do
#: ``settings.toml`` e mudá-lo não pode desligar o brief em silêncio.
PAPEIS_PROJETO: tuple[str, ...] = ("padrao", "demonstracao")

#: As chaves de ``escopo``. Listas de ids da taxonomia — nunca prosa.
CHAVES_ESCOPO: tuple[str, ...] = ("task_type_fora", "domain_sem_sft")


def carregar(caminho: Path | None = None) -> dict[str, Any]:
    """Lê e confere o arquivo. Falha ALTO: brief quebrado é instrução ausente.

    A conferência é estrutural e cobra **paridade** de língua e de seção. Uma
    seção a menos numa das línguas vira um painel mudo justamente no idioma em
    que a pessoa escolheu trabalhar — e nada no runtime levantaria por causa
    disso.
    """
    alvo = Path(caminho) if caminho is not None else ARQUIVO
    dados = json.loads(alvo.read_text(encoding="utf-8"))
    if dados.get("schema") != SCHEMA_BRIEF:
        raise ValueError(f"{alvo}: esperava {SCHEMA_BRIEF}, veio {dados.get('schema')!r}")
    versao = int(dados.get("versao", 0))
    if versao < 1:
        raise ValueError(f"{alvo}: 'versao' precisa ser >= 1")

    projetos = dados.get("projetos") or {}
    faltando = sorted(set(PAPEIS_PROJETO) - set(projetos))
    if faltando:
        raise ValueError(f"{alvo}: sem brief para o projeto {', '.join(faltando)}")
    sobrando = sorted(set(projetos) - set(PAPEIS_PROJETO))
    if sobrando:
        raise ValueError(f"{alvo}: papel de projeto desconhecido {', '.join(sobrando)}")

    for papel, corpo in projetos.items():
        escopo = corpo.get("escopo")
        if not isinstance(escopo, dict) or set(escopo) != set(CHAVES_ESCOPO):
            raise ValueError(
                f"{alvo}: o 'escopo' de {papel!r} precisa ter exatamente "
                f"{sorted(CHAVES_ESCOPO)} — ele é lista de ids, não prosa"
            )
        for chave, ids in escopo.items():
            if not isinstance(ids, list) or not all(isinstance(x, str) and x for x in ids):
                raise ValueError(f"{alvo}: {papel}.escopo.{chave} precisa ser lista de ids")

        textos = corpo.get("textos")
        if not isinstance(textos, dict) or set(textos) != set(IDIOMAS):
            raise ValueError(
                f"{alvo}: {papel!r} tem as línguas {sorted(textos or {})} — "
                f"o brief precisa existir em {sorted(IDIOMAS)}, sem sobra nem falta"
            )
        for lang, secoes in textos.items():
            if not isinstance(secoes, dict) or set(secoes) != set(SECOES):
                raise ValueError(
                    f"{alvo}: {papel!r} em {lang!r} tem as seções {sorted(secoes or {})} — "
                    f"precisa ter exatamente {sorted(SECOES)}"
                )
            for secao, paragrafos in secoes.items():
                if not isinstance(paragrafos, list) or not paragrafos:
                    raise ValueError(
                        f"{alvo}: {papel}.{lang}.{secao} precisa ser uma lista não vazia"
                    )
                if not all(isinstance(x, str) and x.strip() for x in paragrafos):
                    raise ValueError(f"{alvo}: parágrafo vazio em {papel}.{lang}.{secao}")
    return dados


def _texto_json(corpo: dict[str, Any]) -> str:
    """O ``texto_json`` canônico de um projeto. Chaves ordenadas: o diff importa."""
    return json.dumps(
        {
            "schema": SCHEMA_BRIEF,
            "escopo": {chave: list(corpo["escopo"][chave]) for chave in CHAVES_ESCOPO},
            "textos": {
                lang: {secao: list(corpo["textos"][lang][secao]) for secao in SECOES}
                for lang in IDIOMAS
            },
        },
        ensure_ascii=False,
    )


def semear(conn: sqlite3.Connection, dados: dict[str, Any] | None = None) -> int:
    """Insere a versão do arquivo. Devolve quantas linhas ENTRARAM.

    Idempotente por ``(projeto_id, versao)``. **Não sobrescreve**: um brief já
    usado para gravar trabalho humano, reescrito por baixo, transformaria em
    mentira todo ``versao_brief`` que aponta para ele. Mudou o brief, sobe a
    ``versao`` — a antiga permanece, apontada pelas anotações que a seguiram.

    Não há promoção de contrato aqui (o ``@1`` é o primeiro), e é de propósito
    que não exista uma: a exceção do ``diretrizes._promovivel`` só se justificou
    porque havia banco no mundo com a v1 monolíngue.
    """
    pacote = dados if dados is not None else carregar()
    versao = int(pacote["versao"])
    ids = projmod.garantir_padroes(conn)
    entraram = 0
    for papel, corpo in pacote["projetos"].items():
        projeto_id = ids[papel]
        texto = _texto_json(corpo)
        atual = conn.execute(
            "SELECT texto_json FROM briefs WHERE projeto_id = ? AND versao = ?",
            (projeto_id, versao),
        ).fetchone()
        if atual is not None:
            if str(atual["texto_json"]) == texto:
                continue
            raise RuntimeError(
                f"o brief do projeto {papel!r} v{versao} já está no banco com outro texto. "
                "Brief usado para gravar trabalho não se edita — suba a 'versao' em "
                "fixtures/briefs.json e semeie de novo."
            )
        conn.execute(
            "INSERT INTO briefs (projeto_id, versao, texto_json) VALUES (?, ?, ?)",
            (projeto_id, versao, texto),
        )
        entraram += 1
    return entraram


def versao_vigente(conn: sqlite3.Connection, projeto_id: int | None) -> int | None:
    """A maior versão gravada para o projeto — a que vale para quem anota AGORA.

    ``None`` num projeto sem brief (ou numa tarefa sem projeto). A submissão
    grava ``versao_brief = NULL`` nesse caso, que é honesto: não havia brief
    publicado. Inventar 1 diria que a anotação seguiu um enquadramento que não
    existia.
    """
    if projeto_id is None:
        return None
    linha = conn.execute(
        "SELECT max(versao) AS v FROM briefs WHERE projeto_id = ?", (projeto_id,)
    ).fetchone()
    return None if linha is None or linha["v"] is None else int(linha["v"])


def _do_json(bruto: Any) -> dict[str, Any]:
    """O conteúdo de um ``texto_json``, tolerante a lixo.

    Tolerante na FORMA porque um painel vazio é pior que um painel incompleto:
    a tela desenha o que houver e o seed conserta na próxima execução.
    """
    try:
        conteudo = json.loads(str(bruto))
    except (TypeError, ValueError):  # pragma: no cover - texto corrompido à mão
        conteudo = {}
    if not isinstance(conteudo, dict):
        conteudo = {}
    escopo = conteudo.get("escopo")
    textos = conteudo.get("textos")
    return {
        "escopo": {
            chave: list((escopo or {}).get(chave) or []) for chave in CHAVES_ESCOPO
        },
        "textos": {
            lang: {
                secao: list(((textos or {}).get(lang) or {}).get(secao) or [])
                for secao in SECOES
            }
            for lang in IDIOMAS
        },
    }


def vigente(conn: sqlite3.Connection, projeto_id: int) -> dict[str, Any] | None:
    """O brief vigente de UM projeto, nas duas línguas de uma vez.

    As duas línguas juntas pela mesma razão do ``diretrizes.vigentes``: trocar de
    idioma não pode piscar, e um painel de instruções que recarrega ao trocar de
    língua faz a troca parecer um reload.
    """
    linha = conn.execute(
        "SELECT versao, texto_json FROM briefs WHERE projeto_id = ? "
        "ORDER BY versao DESC LIMIT 1",
        (projeto_id,),
    ).fetchone()
    if linha is None:
        return None
    return {"versao": int(linha["versao"]), **_do_json(linha["texto_json"])}


def vigentes(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """``{projeto_id: {versao, escopo, textos}}`` — todos os projetos numa consulta.

    A tela troca de projeto pelo seletor da barra; buscar o brief a cada troca
    daria uma ida ao banco por clique num painel que muda de vez em nunca.
    """
    linhas = conn.execute(
        "SELECT b.projeto_id, b.versao, b.texto_json FROM briefs b "
        "WHERE b.versao = (SELECT max(versao) FROM briefs WHERE projeto_id = b.projeto_id) "
        "ORDER BY b.projeto_id"
    ).fetchall()
    return {
        int(linha["projeto_id"]): {
            "versao": int(linha["versao"]),
            **_do_json(linha["texto_json"]),
        }
        for linha in linhas
    }


__all__ = [
    "ARQUIVO",
    "CHAVES_ESCOPO",
    "IDIOMAS",
    "PAPEIS_PROJETO",
    "SCHEMA_BRIEF",
    "SECOES",
    "carregar",
    "semear",
    "versao_vigente",
    "vigente",
    "vigentes",
]
