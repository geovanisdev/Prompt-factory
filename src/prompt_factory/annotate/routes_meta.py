"""Rotas de meta da Bancada: saúde dos dois bancos e as personas.

São as únicas rotas do P1, e as duas primeiras que a tela chama. O ``/api/health``
daqui é maior que o da curadoria de propósito: ele é o **painel de estado do
admin**, e o que ele diz precisa bastar para responder três perguntas sem abrir
um terminal:

1. os dois bancos abriram, e quais arquivos são;
2. **de onde vem o pool agora** (coleção curada ou fallback) e quantos itens tem;
3. quanto trabalho existe no banco da plataforma.

Tudo lido AO VIVO. Nada do corpus é congelado no lifespan: ele é trocado por
swap de arquivo embaixo desta app, e um número guardado na subida seria uma
mentira com data de validade.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, HTTPException

from .. import __version__, schema
from ..config import get as _cfg
from . import briefs as briefmod
from . import db as adb
from . import diretrizes as dirmod
from . import modelos as modmod
from . import pool as poolmod
from . import projetos as projmod
from . import solo as solomod
from .deps import ConAnotacao, ConCorpus, Estado
from .models import PerfilIn, perfil

router = APIRouter(tags=["bancada"])

#: As colunas de ``anotadores`` que ``models.perfil`` lê. Uma constante porque
#: são três consultas em dois módulos e um ``SELECT`` que esquece
#: ``qualificacoes_json`` só falha na hora de serializar.
COLUNAS_PERFIL = "id, nome, papel, ativo, qualificacoes_json, criado_em"

#: Os limites de ``[annotate]`` que a INTERFACE precisa conhecer para dizer o
#: que falta **antes** do clique. Lidos por request (o ``config`` é cacheado por
#: arquivo, então isto é um acesso a dict), e não congelados no lifespan: quem
#: mexe no ``settings.toml`` espera ver o efeito ao recarregar a página.
LIMITES: tuple[tuple[str, int], ...] = (
    ("min_chars_justificativa", 30),
    ("min_chars_sft", 40),
    ("min_criterios_rubrica", 3),
    ("max_criterios_rubrica", 8),
    ("min_chars_criacao", 15),
    ("min_chars_devolucao", 20),
    # P3b: os dois pisos do Rate and Review. Sem eles no health, o botão que diz
    # "faltam 12 caracteres" divergiria do 422 na primeira vez que o dono
    # ajustasse o `settings.toml`.
    ("min_chars_avaliacao", 30),
    ("min_chars_motivo_edicao", 10),
    ("claim_ttl_min", 120),
    ("page_size", 20),
    # P4d: o teto de turnos, para a tela dizer "24 de 24" ANTES de o botão de
    # enviar turno virar um 409.
    ("max_turnos_conversa", 24),
)


def limites() -> dict[str, int]:
    """``{chave: valor}`` de ``[annotate]``, para a tela validar igual ao servidor."""
    return {chave: int(_cfg("annotate", chave, default=padrao)) for chave, padrao in LIMITES}


@router.get("/api/health", summary="Os dois bancos abriram, e de onde vem o pool")
def health(anot: ConAnotacao, corpus: ConCorpus, meta: Estado) -> dict[str, Any]:
    """O primeiro request da interface, e o painel de estado do admin.

    **É aqui que o pool é materializado.** No caminho comum são duas leituras
    indexadas (a assinatura bate e não há nada a fazer); quando o corpus muda
    por baixo, esta chamada reconstrói a tabela ``pool`` e as rotas seguintes já
    a encontram pronta. Colocar isso no primeiro request da tela, e não no
    lifespan, é o mesmo princípio de sempre: o corpus é trocado por swap embaixo
    da app, e o que se lê na subida tem data de validade.

    Note que **as duas conexões são pedidas na assinatura**: se o corpus tiver
    sumido entre a subida e agora, o erro aparece aqui, na primeira chamada, em
    vez de numa rota qualquer no meio de uma anotação.
    """
    p = poolmod.materializar(anot, corpus)
    linha = corpus.execute("SELECT count(*) AS n FROM prompts").fetchone()
    corpus_meta = {
        r["key"]: r["value"] for r in corpus.execute("SELECT key, value FROM app_meta")
    }
    return {
        "status": "ok",
        "app": "Bancada",
        "pf_version": __version__,
        "sqlite_version": sqlite3.sqlite_version,
        "anotacao": {
            "arquivo": meta["db_anotacao"],
            "schema_version": int(
                adb.get_meta(anot, adb.CHAVE_VERSAO, str(adb.SCHEMA_VERSION_ANOTACAO))
                or adb.SCHEMA_VERSION_ANOTACAO
            ),
            "contagens": adb.contagens(anot),
        },
        "corpus": {
            "arquivo": meta["db_corpus"],
            # Dito explicitamente, e não deduzido pela interface: esta app NUNCA
            # escreve no corpus, e a tela mostra isso porque é uma promessa do
            # produto, não um detalhe de implementação.
            "somente_leitura": True,
            "n_prompts": int(linha["n"]),
            "db_build_id": corpus_meta.get("db_build_id"),
            "built_at": corpus_meta.get("built_at"),
            "schema_version": corpus_meta.get("schema_version"),
        },
        "pool": {
            "origem": p.origem,
            "n_pool": p.n_pool,
            # Cada frase deste bloco sai em DOIS formatos (P3i): a versão em
            # português, para a API se explicar sozinha e para o `pf annotate
            # status` imprimir no terminal; e a CHAVE + os dados, para a tela,
            # que fala duas línguas. Este painel é onde a proveniência fica
            # visível — deixá-lo em português numa tela em inglês tiraria do
            # avaliador estrangeiro exatamente o que ele veio ler.
            "motivo": p.motivo,
            "motivo_chave": p.motivo_chave,
            "motivo_dados": p.motivo_dados,
            "colecao": p.colecao,
            "filtro_fallback": poolmod.descrever_fallback(),
            "filtro": poolmod.filtro_para_tela(),
            # A POLÍTICA DE LICENÇA À VISTA, com o número que ela exclui. Sem o
            # número, "política ativa" é uma promessa; com ele, é uma medida —
            # e foi justamente a ausência dela que deixou 9.148 linhas
            # `cc-by-nc` entrarem no trabalho de anotação sem ninguém ver.
            "politica_licenca": poolmod.politica_licenca(),
            "excluidas_por_licenca": p.excluidas_licenca,
            # A verdade sobre a garantia de NSFW, no mesmo lugar em que a tela
            # a mostra. Ver `pool.NOTA_NSFW`.
            "nota_nsfw": poolmod.NOTA_NSFW,
            "nota_nsfw_chave": poolmod.T_NOTA_NSFW,
            "materializado_em": adb.get_meta(anot, poolmod.CHAVE_ATUALIZADO),
            "build_do_pool": adb.get_meta(anot, poolmod.CHAVE_BUILD),
        },
        # OS LIMITES SAEM DAQUI, e a tela os LÊ. É o que faz "o botão diz o que
        # falta" concordar com o 422 do servidor sem que os números existam em
        # dois lugares — eles moram em `config/settings.toml`, e uma cópia no
        # JavaScript divergiria na primeira vez que alguém ajustasse o arquivo.
        "limites": limites(),
        # O RUNTIME DE INFERÊNCIA (P4d), sondado AO VIVO — pela mesma razão de
        # tudo neste health: o Ollama sobe e desce por fora, e um retrato lido no
        # lifespan seria mentira com data de validade. A sonda tem timeout de 3 s
        # e `estado()` nunca levanta: servidor fora do ar e modelo não baixado
        # são estados de primeira classe, e o health é onde eles aparecem.
        #
        # `escala_turno` sai daqui e não do JS pela razão dos `limites`: uma
        # cópia no cliente divergiria do 422 na primeira vez que ela mudasse.
        "modelos": {
            **modmod.estado(),
            "escala_turno": {"min": adb.ESCALA_TURNO[0], "max": adb.ESCALA_TURNO[1]},
        },
        # MODO SOLO à vista, ligado ou desligado. É dele que a tela monta a
        # faixa permanente na fila de revisão. Um modo que muda a regra de QC e
        # não aparece na tela seria exatamente a mentira que este projeto não
        # conta — por isso ele sai do health e não de uma variável do JS.
        "modo_solo": solomod.ligado(),
        "projetos": {
            "padrao": projmod.nome_padrao(),
            "demonstracao": projmod.nome_demonstracao(),
        },
    }


@router.get("/api/projetos", summary="Os projetos, com quanto trabalho cada um tem")
def listar_projetos(anot: ConAnotacao) -> dict[str, Any]:
    """O que o seletor da barra mostra, e o que o painel do admin separa.

    Dois nascem com o banco: ``Portfólio`` (o trabalho real sobre o corpus, e o
    default de toda tarefa nova) e ``Demonstração`` (o pacote de fixtures).
    Separá-los é o que permite dizer qual trabalho é de quem, em vez de somar
    fixture com autoria na mesma coluna — e é o que faz o portfólio significar
    alguma coisa.
    """
    projmod.garantir_padroes(anot)
    return {
        "items": projmod.listar(anot),
        "padrao": projmod.nome_padrao(),
        "demonstracao": projmod.nome_demonstracao(),
    }


@router.get("/api/diretrizes", summary="A regra vigente de cada estilo de tarefa")
def listar_diretrizes(anot: ConAnotacao) -> dict[str, Any]:
    """As diretrizes **do banco**, com a versão — não literais no HTML.

    A tela desenha o que vier daqui, e a submissão carimba
    ``anotacoes.versao_diretriz`` com a mesma versão lida do banco (pelo
    servidor, nunca pelo que o cliente afirmar ter lido). Assim "sob qual regra
    isto foi anotado?" vira uma consulta em vez de uma arqueologia de git.
    """
    return {"items": dirmod.vigentes(anot)}


@router.get("/api/briefs", summary="O brief vigente de cada projeto")
def listar_briefs(anot: ConAnotacao) -> dict[str, Any]:
    """O ENQUADRAMENTO de cada projeto, com a versão — o outro eixo da instrução.

    ``/api/diretrizes`` responde COMO se faz um tipo de tarefa e vale em qualquer
    projeto; isto responde o que é ESTE projeto: para que serve o dado, que
    prompts aparecem, como se escolhe um, e o que este cliente aceita como rating
    e como justificativa.

    Todos os projetos numa chamada, e as duas línguas juntas: a tela troca de
    projeto pelo seletor da barra e de idioma pelo botão, e nenhuma das duas
    trocas pode piscar.

    ``escopo`` sai como ids da taxonomia do corpus, não como prosa — quem
    desenha os nomes legíveis é a tela, com a taxonomia que ela já recebe do
    ``/api/stats`` do corpus. Traduzir id aqui faria a plataforma e o banco de
    prompts falarem línguas diferentes sobre a mesma categoria.
    """
    return {"items": {str(k): v for k, v in briefmod.vigentes(anot).items()}}


#: A taxonomia do corpus, servida a partir de ``labeling/taxonomy.json`` — a
#: fonte ÚNICA das 32 classes. Uma segunda cópia dentro do ``index.html``
#: divergiria, e a que diverge é sempre a da tela (é a mesma disciplina do
#: ``/api/stats`` da curadoria).
#:
#: ``lru_cache`` porque o arquivo não muda em tempo de execução e a tela pede
#: isto uma vez por sessão. Taxonomia ilegível **não derruba a rota**: ela cai
#: para um dicionário vazio, e a tela desenha o id — que é a identidade de
#: qualquer jeito, e é o que o escopo do brief grava.
@lru_cache(maxsize=1)
def _taxonomia() -> dict[str, Any]:
    try:
        tax = schema.load_taxonomy()
    except (OSError, ValueError):  # pragma: no cover - arquivo do repo
        return {"versao": None, "task_type": {}, "domain": {}}
    return {
        "versao": tax.get("version"),
        **{
            secao: {
                chave: {
                    # O canônico é pt-BR porque a taxonomia nasceu com o corpus,
                    # que é pt-BR; o `nome_en` é o sidecar de exibição, exatamente
                    # como o `*_i18n` das rubricas. O ID nunca é traduzido: é ele
                    # que o escopo do brief guarda e que a anotação vai gravar.
                    "nome": str(entrada.get("nome", chave)),
                    "nome_en": str(entrada.get("nome_en") or entrada.get("nome", chave)),
                }
                for chave, entrada in tax.get(secao, {}).items()
            }
            for secao in ("task_type", "domain")
        },
    }


@router.get("/api/taxonomia", summary="As 32 classes do corpus, nas duas línguas")
def listar_taxonomia() -> dict[str, Any]:
    """O vocabulário que o ``escopo`` do brief referencia por ID.

    É o que faz a plataforma e o banco de prompts falarem a mesma língua sobre a
    mesma categoria — e é o vocabulário que o trabalho de anotação vai PRODUZIR,
    já que ``task_type`` e ``domain`` estão nulos em 100% do corpus.
    """
    return _taxonomia()


@router.get("/api/modelos-rubrica", summary="Os modelos de partida da aba escrever-rubrica")
def listar_modelos_rubrica() -> dict[str, Any]:
    """O banco de perguntas — DADO, e não literais dentro do ``index.html``.

    Até o P8 estes modelos eram um array de JavaScript, e um quarto modelo exigia
    editar a tela. O construtor do admin (P5a) existe justamente para que quem
    monta a tarefa escolha blocos sem tocar em código; ele vai trocar a fixture
    por uma tabela, e a tela não vai notar, porque ela nunca soube de onde eles
    vinham. Ver ``rubricas_modelo.py``.

    Sem dependência de banco de propósito: é fixture do pacote, e um clone limpo
    tem de conseguir abrir a aba antes de qualquer seed.
    """
    from . import rubricas_modelo

    return {"items": rubricas_modelo.carregar()}


@router.get("/api/perfis", summary="As personas cadastradas")
def listar_perfis(anot: ConAnotacao) -> dict[str, Any]:
    """Ordenadas por papel e nome — é a ordem em que o seletor da barra agrupa."""
    linhas = anot.execute(
        f"SELECT {COLUNAS_PERFIL} FROM anotadores ORDER BY papel, nome"
    ).fetchall()
    return {"items": [perfil(linha) for linha in linhas]}


@router.post("/api/perfis", status_code=201, summary="Cria uma persona")
def criar_perfil(corpo: PerfilIn, anot: ConAnotacao) -> dict[str, Any]:
    """409 em nome repetido — quem decide é o ``UNIQUE`` do DDL.

    Conferir com um SELECT antes seria uma corrida (e, com duas abas abertas na
    demonstração, uma corrida que acontece). Deixar o banco recusar e traduzir o
    erro é o mesmo caminho de ``app/routes_collections.criar``.
    """
    try:
        cur = anot.execute(
            "INSERT INTO anotadores (nome, papel) VALUES (?, ?)",
            (corpo.nome, corpo.papel),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"já existe alguém chamado {corpo.nome!r} — escolha outro nome",
        ) from exc
    linha = anot.execute(
        f"SELECT {COLUNAS_PERFIL} FROM anotadores WHERE id = ?",
        (int(cur.lastrowid or 0),),
    ).fetchone()
    return perfil(linha)


__all__ = ["router"]
