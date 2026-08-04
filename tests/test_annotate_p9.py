"""P9: o BRIEF do projeto — o enquadramento como dado versionado.

O defeito que este marco fecha não é um bug: é uma **ausência**. As telas de
tarefa diziam o COMO (a mecânica de cada tipo, via ``diretrizes``) e não diziam o
QUÊ — que projeto é este, para que serve o dado, que prompts aparecem, como se
escolhe um, e o que este cliente aceita como rating e como justificativa. Num
trabalho real essas respostas estão no topo de toda tarefa, e a inconsistência
entre elas é uma das causas comuns de lote devolvido, porque cada anotador lê uma
versão diferente da mesma regra.

As famílias aqui:

* **dois eixos, dois ponteiros.** ``versao_diretriz`` versiona a mecânica do
  tipo; ``versao_brief`` versiona o enquadramento do projeto. Um só obrigaria a
  subir os dois juntos, e a coluna que separa "antes" de "depois" passaria a
  significar outra coisa;
* **o escopo é ID, não prosa.** Ele sai da taxonomia do corpus e não é traduzido
  — é o que faz a plataforma e o banco de prompts falarem a mesma língua sobre a
  mesma categoria;
* **NULL é a verdade no trabalho antigo.** Uma anotação feita antes de existir
  brief não seguiu brief nenhum, e carimbar 1 ali diria que ela seguiu um
  enquadramento que ainda não existia.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from prompt_factory import db as dbmod
from prompt_factory.annotate import briefs as briefmod
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import migracao
from prompt_factory.annotate import projetos as projmod
from prompt_factory.annotate import seed as seedmod
from prompt_factory.annotate.main import criar_app

from .fixtures_annotate_v1 import contagens_do_disco, rebaixar
from .test_annotate_p2 import notas_da_rubrica
from .test_api import montar_banco

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    caminho = tmp_path / "prompts.sqlite"
    montar_banco(caminho)
    return caminho


@pytest.fixture
def semeado(tmp_path: Path, corpus: Path) -> Path:
    banco = tmp_path / "annotate.sqlite"
    conn = dbmod.connect(banco)
    corpo = dbmod.connect(corpus, readonly=True)
    try:
        adb.init_db(conn)
        seedmod.semear(conn, corpo)
    finally:
        conn.close()
        corpo.close()
    return banco


@pytest.fixture
def cliente(semeado: Path, corpus: Path):
    with TestClient(criar_app(semeado, corpus)) as c:
        yield c


def _papeis(cliente: TestClient) -> dict[str, list[int]]:
    saida: dict[str, list[int]] = {}
    for p in cliente.get("/api/perfis").json()["items"]:
        saida.setdefault(p["papel"], []).append(p["id"])
    return saida


# ---------------------------------------------------------------------------
# 1. o arquivo
# ---------------------------------------------------------------------------


def test_o_brief_cobre_as_seis_superficies_que_faltavam() -> None:
    """Uma fonte, seis projeções — e não seis textos que podem divergir."""
    pacote = briefmod.carregar()
    for papel in briefmod.PAPEIS_PROJETO:
        corpo = pacote["projetos"][papel]
        for lang in briefmod.IDIOMAS:
            assert set(corpo["textos"][lang]) == set(briefmod.SECOES), (papel, lang)


def test_o_escopo_fala_a_LINGUA_DA_TAXONOMIA_do_corpus() -> None:
    """A decisão que fecha o laço com o banco de prompts.

    ``task_type`` e ``domain`` estão nulos em 100% do corpus. Um escopo escrito
    em ids da taxonomia é o que faz o trabalho de anotação PRODUZIR o rótulo que
    falta, em vez de produzir um JSONL que fica num diretório. Um vocabulário
    próprio aqui seria um segundo dicionário que nada casa.
    """
    taxonomia = json.loads(
        (Path(__file__).resolve().parents[1] / "labeling" / "taxonomy.json").read_text(
            encoding="utf-8"
        )
    )
    pacote = briefmod.carregar()
    for papel, corpo in pacote["projetos"].items():
        assert set(corpo["escopo"]["task_type_fora"]) <= set(taxonomia["task_type"]), papel
        assert set(corpo["escopo"]["domain_sem_sft"]) <= set(taxonomia["domain"]), papel
    # E o projeto real EXCLUI alguma coisa: um escopo que inclui tudo não é um
    # escopo, é uma descrição.
    assert pacote["projetos"]["padrao"]["escopo"]["task_type_fora"]


def test_o_brief_do_projeto_real_carrega_regra_QUE_MORDE() -> None:
    """O teste que separa brief de decoração.

    Um brief escrito pela mesma pessoa que fez o trabalho tende a nascer já
    satisfeito — e aí ele descreve em vez de especificar. Estas são as regras que
    a plataforma **verifica** e que um item pode violar: a convenção de língua
    dos metadados, o piso de duas frases, e a regra de obrigação do P8.
    """
    texto = " ".join(
        linha
        for secao in briefmod.SECOES
        for linha in briefmod.carregar()["projetos"]["padrao"]["textos"]["en"][secao]
    ).lower()
    assert "english" in texto, "a convenção de língua dos metadados não está dita"
    assert "two sentences" in texto, "o piso da justificativa não está dito"
    assert "defect type" in texto, "a regra de obrigação do P8 não está dita"


@pytest.mark.parametrize(
    ("estrago", "trecho_do_erro"),
    [
        ({"schema": "briefs@9"}, "esperava briefs@1"),
        ({"versao": 0}, "precisa ser >= 1"),
        ({"projetos": {}}, "sem brief para o projeto"),
    ],
)
def test_um_brief_torto_falha_alto(
    tmp_path: Path, estrago: dict[str, Any], trecho_do_erro: str
) -> None:
    """Brief quebrado é instrução ausente, e tem de aparecer no terminal — não
    como um painel vazio que ninguém liga ao arquivo."""
    dados = {**briefmod.carregar(), **estrago}
    alvo = tmp_path / "b.json"
    alvo.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=trecho_do_erro):
        briefmod.carregar(alvo)


def test_uma_lingua_a_menos_e_recusada(tmp_path: Path) -> None:
    """Paridade, e não "pelo menos o inglês": uma seção faltando numa língua vira
    um painel mudo justamente no idioma que a pessoa escolheu."""
    dados = briefmod.carregar()
    dados["projetos"]["padrao"]["textos"].pop("pt")
    alvo = tmp_path / "b.json"
    alvo.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="precisa existir em"):
        briefmod.carregar(alvo)


# ---------------------------------------------------------------------------
# 2. o banco
# ---------------------------------------------------------------------------


def test_o_seed_publica_um_brief_por_projeto_e_e_idempotente(semeado: Path) -> None:
    conn = dbmod.connect(semeado)
    try:
        vigentes = briefmod.vigentes(conn)
        assert len(vigentes) == len(briefmod.PAPEIS_PROJETO)
        for brief in vigentes.values():
            assert brief["versao"] == 1
            assert set(brief["textos"]) == set(briefmod.IDIOMAS)
        assert briefmod.semear(conn) == 0
    finally:
        conn.close()


def test_editar_uma_versao_ja_publicada_e_recusado(semeado: Path) -> None:
    """A mesma regra das diretrizes, pela mesma razão: um brief reescrito por
    baixo transformaria em mentira todo ``versao_brief`` que aponta para ele."""
    dados = briefmod.carregar()
    dados["projetos"]["padrao"]["textos"]["en"]["visao_geral"] = ["outra coisa"]
    conn = dbmod.connect(semeado)
    try:
        with pytest.raises(RuntimeError, match="não se edita"):
            briefmod.semear(conn, dados)
    finally:
        conn.close()


def test_subir_a_versao_publica_ao_lado_sem_apagar_a_antiga(semeado: Path) -> None:
    dados = briefmod.carregar()
    dados["versao"] = 2
    dados["projetos"]["padrao"]["textos"]["en"]["visao_geral"] = ["a regra apertou"]
    conn = dbmod.connect(semeado)
    try:
        assert briefmod.semear(conn, dados) == len(briefmod.PAPEIS_PROJETO)
        projeto_id = projmod.garantir(conn)
        assert briefmod.versao_vigente(conn, projeto_id) == 2
        assert briefmod.vigente(conn, projeto_id)["textos"]["en"]["visao_geral"] == [
            "a regra apertou"
        ]
        # A v1 continua no banco, apontada pelo trabalho que a seguiu.
        assert int(
            conn.execute(
                "SELECT count(*) AS n FROM briefs WHERE projeto_id = ?", (projeto_id,)
            ).fetchone()["n"]
        ) == 2
    finally:
        conn.close()


def test_projeto_sem_brief_devolve_None_e_nao_inventa_versao(semeado: Path) -> None:
    """``None`` e ``1`` são afirmações diferentes: "não havia brief" contra
    "seguiu o primeiro brief". A mesma lição do agreement do M5."""
    conn = dbmod.connect(semeado)
    try:
        novo = projmod.garantir(conn, "Cliente sem brief")
        assert briefmod.versao_vigente(conn, novo) is None
        assert briefmod.versao_vigente(conn, None) is None
        assert briefmod.vigente(conn, novo) is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. a submissão carimba os DOIS eixos
# ---------------------------------------------------------------------------


def test_a_submissao_grava_a_versao_do_brief_lida_do_BANCO(
    cliente: TestClient, semeado: Path
) -> None:
    """Do banco, nunca do cliente: uma tela com o JS em cache diria a versão
    velha, e a única coluna que separa "antes" de "depois" passaria a mentir."""
    quem = _papeis(cliente)["anotador"][0]
    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": "avaliar_rubrica"}
    ).json()["tarefa"]
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": notas_da_rubrica(env)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["versao_brief"] == 1

    conn = dbmod.connect(semeado)
    try:
        linha = conn.execute(
            "SELECT versao_diretriz, versao_brief FROM anotacoes WHERE id = ?",
            (r.json()["anotacao_id"],),
        ).fetchone()
    finally:
        conn.close()
    # DOIS eixos, dois ponteiros, e eles não são o mesmo número por acaso: um
    # versiona a mecânica do tipo, o outro o enquadramento do projeto.
    assert int(linha["versao_brief"]) == 1
    assert linha["versao_diretriz"] is not None


def test_a_versao_do_brief_sai_no_envelope_da_triagem(cliente: TestClient) -> None:
    """Quem tria precisa saber sob qual enquadramento aquilo foi feito — é a
    diferença entre "está errado" e "está certo para o brief de então"."""
    papeis = _papeis(cliente)
    quem, revisor = papeis["anotador"][0], papeis["revisor"][0]
    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": "avaliar_rubrica"}
    ).json()["tarefa"]
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": notas_da_rubrica(env)},
    )
    det = cliente.get(
        f"/api/revisao/{r.json()['anotacao_id']}", params={"revisor_id": revisor}
    ).json()
    assert det["tarefa"]["anotacao"]["versao_brief"] == 1


# ---------------------------------------------------------------------------
# 4. a rota
# ---------------------------------------------------------------------------


def test_a_rota_devolve_todos_os_projetos_nas_duas_linguas(cliente: TestClient) -> None:
    """Uma chamada só: a tela troca de projeto pelo seletor e de idioma pelo
    botão, e nenhuma das duas trocas pode piscar."""
    corpo = cliente.get("/api/briefs").json()
    assert len(corpo["items"]) >= len(briefmod.PAPEIS_PROJETO)
    for brief in corpo["items"].values():
        assert set(brief["textos"]) == set(briefmod.IDIOMAS)
        assert set(brief["textos"]["pt"]) == set(briefmod.SECOES)
        assert set(brief["escopo"]) == set(briefmod.CHAVES_ESCOPO)


def test_o_escopo_sai_como_ID_e_nunca_traduzido(cliente: TestClient) -> None:
    """Traduzir id aqui faria a plataforma e o corpus falarem línguas diferentes
    sobre a mesma categoria. Quem desenha o nome legível é a tela."""
    items = cliente.get("/api/briefs").json()["items"]
    fora = {
        chave
        for brief in items.values()
        for chave in brief["escopo"]["task_type_fora"]
    }
    assert "conversa-social" in fora
    assert not any(" " in chave for chave in fora), "id de taxonomia não tem espaço"


# ---------------------------------------------------------------------------
# 5. a migração
# ---------------------------------------------------------------------------


def test_migrar_da_v5_preserva_o_trabalho_e_deixa_versao_brief_NULL(
    cliente: TestClient, semeado: Path
) -> None:
    """NULL é a verdade: aquelas anotações não seguiram brief nenhum.

    É o oposto do ``versao_diretriz`` da migração da v1, onde carimbar 1 **era**
    a verdade — o texto literal do HTML era, palavra por palavra, a v1 do
    arquivo. Aqui não existia brief, e inventar 1 diria que existia.
    """
    quem = _papeis(cliente)["anotador"][0]
    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": "avaliar_rubrica"}
    ).json()["tarefa"]
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": notas_da_rubrica(env)},
    )
    assert r.status_code == 200, r.text
    anotacao_id = r.json()["anotacao_id"]

    conn = dbmod.connect(semeado)
    try:
        rebaixar(conn, 5)
        adb.set_meta(conn, adb.CHAVE_VERSAO, 5)
        antes = contagens_do_disco(conn)
    finally:
        conn.close()

    relatorio = migracao.migrar(semeado)
    assert relatorio["de"] == 5 and relatorio["para"] == adb.SCHEMA_VERSION_ANOTACAO
    assert Path(relatorio["backup"]).is_file()
    assert any("brief" in aviso for aviso in relatorio["avisos"])

    conn = dbmod.connect(semeado)
    try:
        depois = contagens_do_disco(conn)
        for tabela, n in antes.items():
            if tabela in migracao.TABELAS_QUE_CRESCEM:
                assert depois[tabela] >= n, tabela
                continue
            assert depois[tabela] == n, tabela
        assert depois["briefs"] == 0, "briefs nasce vazia; quem publica é o seed"
        linha = conn.execute(
            "SELECT versao_brief FROM anotacoes WHERE id = ?", (anotacao_id,)
        ).fetchone()
        assert linha["versao_brief"] is None
        # E o seed conserta na primeira execução seguinte.
        assert briefmod.semear(conn) == len(briefmod.PAPEIS_PROJETO)
    finally:
        conn.close()


def test_a_conversa_sobrevive_a_migracao_da_v5(semeado: Path, corpus: Path) -> None:
    """``turnos_conversa`` entra pela primeira vez numa lista de cópia.

    Ela existe desde a v5: uma migração v5→v6 que a esquecesse apagaria conversas
    inteiras — o único trabalho humano deste schema produzido ANTES de existir
    uma linha em ``anotacoes``.
    """
    conn = dbmod.connect(semeado)
    try:
        atribuicao = conn.execute("SELECT id FROM atribuicoes LIMIT 1").fetchone()
        if atribuicao is None:
            tarefa = conn.execute("SELECT id FROM tarefas LIMIT 1").fetchone()
            anotador = conn.execute("SELECT id FROM anotadores LIMIT 1").fetchone()
            cur = conn.execute(
                "INSERT INTO atribuicoes (tarefa_id, anotador_id, status) "
                "VALUES (?, ?, 'em_andamento')",
                (int(tarefa["id"]), int(anotador["id"])),
            )
            atribuicao_id = int(cur.lastrowid or 0)
        else:
            atribuicao_id = int(atribuicao["id"])
        conn.execute(
            "INSERT INTO turnos_conversa (atribuicao_id, ordem, papel, texto) "
            "VALUES (?, 0, 'usuario', 'oi, tudo bem?')",
            (atribuicao_id,),
        )
        rebaixar(conn, 5)
        adb.set_meta(conn, adb.CHAVE_VERSAO, 5)
    finally:
        conn.close()

    migracao.migrar(semeado)

    conn = dbmod.connect(semeado)
    try:
        turno = conn.execute("SELECT texto FROM turnos_conversa").fetchone()
        assert turno is not None and str(turno["texto"]) == "oi, tudo bem?"
    finally:
        conn.close()


def test_o_schema_migrado_da_v5_e_identico_ao_de_um_banco_novo(
    tmp_path: Path, semeado: Path
) -> None:
    """A propriedade que o ``ALTER TABLE`` não daria — e que este módulo mantém
    desde a v1."""
    conn = dbmod.connect(semeado)
    try:
        rebaixar(conn, 5)
        adb.set_meta(conn, adb.CHAVE_VERSAO, 5)
    finally:
        conn.close()
    migracao.migrar(semeado)

    novo = tmp_path / "novo.sqlite"
    conn = dbmod.connect(novo)
    try:
        adb.init_db(conn)
        esperado = sorted(
            str(r["sql"])
            for r in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
        )
    finally:
        conn.close()

    conn = dbmod.connect(semeado)
    try:
        obtido = sorted(
            str(r["sql"])
            for r in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
        )
    finally:
        conn.close()
    assert obtido == esperado


# ---------------------------------------------------------------------------
# 6. a entrega
# ---------------------------------------------------------------------------


def test_o_artefato_entregue_declara_o_enquadramento_do_projeto() -> None:
    """Sem isto o cliente lê o nome do projeto e não sabe QUAL versão do brief
    valia — que é justamente a pergunta que aparece quando o lote chega
    inconsistente."""
    from prompt_factory.annotate import entrega

    item = {
        "anotacao_id": 1,
        "tipo": "avaliar_rubrica",
        "payload_schema": "avaliar_rubrica@2",
        "versao_diretriz": 1,
        "versao_brief": 3,
        "projeto": "Portfólio",
        "prompt": {"text": "x"},
        "payload_final": {},
        "corrigido": False,
        "anotador": "Alguém",
        "submetida_em": "2026-08-03T00:00:00.000Z",
        "tempo_ativo_ms": 1,
        "is_demo": False,
        "sintetica": False,
        "licenca": None,
        "fonte": None,
        "lang": "pt",
        "qc": {},
        "avaliacao": None,
        "triagem": None,
        "decisao_admin": None,
        "payload_submetido": {},
        "prompt_uid": "u",
        "origem_tarefa": "semente",
        "versao": 1,
        "status": "avaliada",
        "anotador_id": 1,
    }
    (registro,) = list(entrega.registros_annotations([item]))
    assert registro["project_brief_version"] == 3
    assert registro["guideline_version"] == 1


# ---------------------------------------------------------------------------
# 7. o painel na tela
# ---------------------------------------------------------------------------


@pytest.fixture
def js() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "src/prompt_factory/annotate/static/index.html"
    ).read_text(encoding="utf-8")


def test_a_tabela_de_secoes_do_JS_cobre_o_contrato_do_PYTHON(js: str) -> None:
    """Duas listas da mesma coisa em duas linguagens — e a que fica para trás é
    sempre a da tela.

    Uma seção acrescentada ao ``briefs.SECOES`` sem entrada em ``SECOES_BRIEF``
    não quebra nada: ela simplesmente **não aparece**, e a instrução some sem
    aviso. É o modo de falha mais caro de um painel de instruções.
    """
    bloco = js.split("const SECOES_BRIEF = [", 1)[1].split("];", 1)[0]
    campos = re.findall(r'\["([a-z_]+)", "brief\.[a-z_]+"\]', bloco)
    assert campos == list(briefmod.SECOES)


def test_toda_secao_tem_rotulo_nas_duas_linguas(js: str) -> None:
    """O rótulo é CROMO e mora no dicionário; o texto é DADO e vem do banco."""
    dicionarios = json.loads(js.split("const TEXTOS = ", 1)[1].split("\n};", 1)[0] + "\n}")
    for secao in briefmod.SECOES:
        for lang in briefmod.IDIOMAS:
            assert f"brief.{secao}" in dicionarios[lang], (secao, lang)


def test_a_chave_da_secao_e_LITERAL_e_nunca_concatenada(js: str) -> None:
    """``t("brief." + secao)`` passaria despercebido pelo teste de chave órfã e
    deixaria entrada morta no dicionário — a mesma disciplina do P3i."""
    assert 't("brief." +' not in js
    assert '"brief.visao_geral"' in js


def test_so_UM_painel_de_instrucao_abre_sozinho(js: str) -> None:
    """Medido: os dois abertos davam 1.608px até a área de trabalho numa
    viewport de 720px — o doom scrolling que o P8 existiu para matar,
    reintroduzido no topo de toda tarefa. O brief ganha a primeira vez porque é
    o enquadramento; ler a mecânica antes dele produz trabalho tecnicamente
    correto e fora de propósito."""
    assert 'det.open = primeiraVez && !$("#brief").open;' in js


def test_o_chip_de_categoria_mostra_o_ID_junto_do_nome(js: str) -> None:
    """O id é o que atravessa a plataforma e o corpus, e é o que a anotação vai
    gravar. Um chip só com o nome traduzido esconderia justamente isso."""
    assert 'el("span", "cat-id", id)' in js


def test_a_taxonomia_tem_nome_em_ingles_nas_32_classes() -> None:
    """O portfólio é lido por avaliadores estrangeiros. Sem ``nome_en``, o
    escopo do brief abriria em português no meio de uma tela em inglês.

    Aditivo e sem bump de versão: a versão da taxonomia descreve as CLASSES, e
    acrescentar a tradução de um nome não muda a regra de classificação — a
    mesma lógica do sidecar ``*_i18n`` de uma rubrica.
    """
    from prompt_factory import schema

    tax = schema.load_taxonomy()
    for secao in ("task_type", "domain"):
        for chave, entrada in tax[secao].items():
            assert entrada.get("nome_en"), (secao, chave)
            assert entrada["nome_en"] != entrada["nome"], (secao, chave)


def test_a_rota_da_taxonomia_devolve_as_duas_linguas(cliente: TestClient) -> None:
    corpo = cliente.get("/api/taxonomia").json()
    assert corpo["versao"]
    assert len(corpo["task_type"]) == 16 and len(corpo["domain"]) == 16
    assert corpo["task_type"]["conversa-social"]["nome_en"] == "Social chat"
