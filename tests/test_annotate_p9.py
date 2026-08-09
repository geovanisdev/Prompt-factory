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
from prompt_factory.annotate import migracao, rubricas_modelo
from prompt_factory.annotate import projetos as projmod
from prompt_factory.annotate import seed as seedmod
from prompt_factory.annotate import tarefas as tarmod
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
    # As quatro regras do grill me (2026-08-06). Âncoras estáveis de propósito:
    # o texto pode ser reescrito à vontade desde que continue DIZENDO isto.
    assert "model-generated" in texto, "a proibição de IA no rationale não está dita"
    assert "verif" in texto, "a regra 'verificou ou declarou' não está dita"
    assert "triage returns" in texto, "a lista de devolução automática não está dita"
    assert "user goal" in texto, "a caixa de user goal não está explicada"


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
        # A versão publicada é a DO ARQUIVO, nunca um literal: o grill me do
        # dono já produziu a v2, e um `== 1` aqui congelaria o teste no passado.
        versao_do_arquivo = int(briefmod.carregar()["versao"])
        for brief in vigentes.values():
            assert brief["versao"] == versao_do_arquivo
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
    proxima = int(dados["versao"]) + 1
    dados["versao"] = proxima
    dados["projetos"]["padrao"]["textos"]["en"]["visao_geral"] = ["a regra apertou"]
    conn = dbmod.connect(semeado)
    try:
        assert briefmod.semear(conn, dados) == len(briefmod.PAPEIS_PROJETO)
        projeto_id = projmod.garantir(conn)
        assert briefmod.versao_vigente(conn, projeto_id) == proxima
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
    versao_do_arquivo = int(briefmod.carregar()["versao"])
    assert r.json()["versao_brief"] == versao_do_arquivo

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
    assert int(linha["versao_brief"]) == versao_do_arquivo
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
    assert det["tarefa"]["anotacao"]["versao_brief"] == int(briefmod.carregar()["versao"])


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


# ---------------------------------------------------------------------------
# 8. o skip categorizado — a promessa do brief v2 com UI de verdade
# ---------------------------------------------------------------------------


def _claim(cliente: TestClient, quem: int) -> dict[str, Any]:
    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": "avaliar_rubrica"}
    ).json()["tarefa"]
    assert env is not None
    return env


def test_o_skip_com_motivo_grava_o_motivo_no_EVENTO(
    cliente: TestClient, semeado: Path
) -> None:
    """O padrão de skips é dado de ALOCAÇÃO, e trilha de operação mora em
    ``eventos`` — não numa coluna que custaria a migração v7."""
    quem = _papeis(cliente)["anotador"][0]
    env = _claim(cliente, quem)
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/abandonar",
        json={"anotador_id": quem, "motivo": "cannot-verify"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["motivo"] == "cannot-verify"

    conn = dbmod.connect(semeado)
    try:
        linha = conn.execute(
            "SELECT detalhe_json FROM eventos WHERE acao = 'tarefa_abandonada' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(str(linha["detalhe_json"]))["motivo"] == "cannot-verify"


def test_motivo_fora_da_lista_e_422(cliente: TestClient) -> None:
    """A lista é fechada porque o brief a promete fechada: 'one click, no
    essay'. Um campo livre viraria o pedágio que a regra existe para não ter."""
    quem = _papeis(cliente)["anotador"][0]
    env = _claim(cliente, quem)
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/abandonar",
        json={"anotador_id": quem, "motivo": "preguica"},
    )
    assert r.status_code == 422
    # A tarefa NÃO foi abandonada: validação recusada não muda estado.
    r2 = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/abandonar",
        json={"anotador_id": quem, "motivo": "outside-my-domain"},
    )
    assert r2.status_code == 200, r2.text


def test_abandono_sem_motivo_continua_valendo_e_nao_finge_ser_skip(
    cliente: TestClient, semeado: Path
) -> None:
    """Largar uma tarefa do catálogo ou mudar de ideia não é 'não sei julgar
    isto'. O evento sai SEM a chave — ausente, nunca null."""
    quem = _papeis(cliente)["anotador"][0]
    env = _claim(cliente, quem)
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/abandonar",
        json={"anotador_id": quem},
    )
    assert r.status_code == 200, r.text
    assert r.json()["motivo"] is None

    conn = dbmod.connect(semeado)
    try:
        linha = conn.execute(
            "SELECT detalhe_json FROM eventos WHERE acao = 'tarefa_abandonada' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert "motivo" not in json.loads(str(linha["detalhe_json"]))


def test_a_tabela_de_motivos_do_JS_bate_com_o_enum_do_BACKEND(js: str) -> None:
    """Duas listas da mesma coisa em duas linguagens. Um motivo acrescentado no
    backend sem botão na tela seria uma categoria que ninguém consegue escolher;
    um botão sem enum seria um clique que devolve 422."""
    bloco = js.split("const MOTIVOS_SKIP = [", 1)[1].split("];", 1)[0]
    ids = re.findall(r'\["([a-z-]+)", "skip\.[a-z_]+"\]', bloco)
    assert tuple(ids) == adb.MOTIVOS_ABANDONO
    # E o brief v2 lista exatamente estes, na prosa: a promessa e a UI são a
    # mesma lista, com os hífens virando espaço no texto humano.
    texto = " ".join(
        linha
        for secao in briefmod.SECOES
        for linha in briefmod.carregar()["projetos"]["padrao"]["textos"]["en"][secao]
    ).lower()
    for motivo in adb.MOTIVOS_ABANDONO:
        assert motivo.replace("-", " ") in texto, motivo


# ---------------------------------------------------------------------------
# 9. as perguntas do instrumento — a caixa de user goal
# ---------------------------------------------------------------------------


def _env_severidade(cliente: TestClient, quem: int) -> dict[str, Any]:
    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": "avaliar_rubrica"}
    ).json()["tarefa"]
    assert env is not None and env["rubrica"]["titulo"] == "Severity review"
    return env


def _payload_severidade(env: dict[str, Any], **extra: Any) -> dict[str, Any]:
    corpo: dict[str, Any] = {
        "notas": [
            {"criterio": c["nome"], "nota": int(c["escala"]["max"])}
            for c in env["rubrica"]["criterios"]
        ],
        # Caixa livre leva prosa; CATEGORIA leva o id da primeira opção — como
        # o helper compartilhado, e pela mesma razão: qualquer outra string é
        # "fora do vocabulário".
        "respostas": {
            q["id"]: str(q["opcoes"][0]["id"])
            if q.get("tipo") == "categoria" and q.get("opcoes")
            else "The person wanted a wall-ready procedure inside the stated limits."
            for q in env["rubrica"]["perguntas"]
        },
    }
    corpo.update(extra)
    return corpo


def test_o_instrumento_declara_as_perguntas_e_o_envelope_as_carrega(
    cliente: TestClient,
) -> None:
    """As perguntas vêm NORMALIZADAS do servidor — a tela e a validação leem a
    mesma lista, senão haveria um 422 impossível de satisfazer. As três, na
    ordem do trabalho: dizer o que a pessoa queria, depois classificá-lo."""
    quem = _papeis(cliente)["anotador"][0]
    env = _env_severidade(cliente, quem)
    perguntas = {p["id"]: p for p in env["rubrica"]["perguntas"]}
    assert list(perguntas) == ["user-goal", "task-type", "domain"]
    goal = perguntas["user-goal"]
    assert goal["obrigatoria"] is True
    assert goal["minimo"] == 30
    assert goal["tipo"] == "texto"
    assert set(goal["rotulo_i18n"]) == {"en", "pt"}


def test_pergunta_obrigatoria_sem_resposta_e_422_ANTES_dos_criterios(
    cliente: TestClient,
) -> None:
    """A ordem dos erros é a ordem do trabalho: o brief manda declarar o user
    goal antes de dar nota, então a pergunta reclama primeiro."""
    quem = _papeis(cliente)["anotador"][0]
    env = _env_severidade(cliente, quem)
    corpo = _payload_severidade(env)
    corpo["respostas"] = {}
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": corpo},
    )
    assert r.status_code == 422
    assert "sem resposta" in r.json()["detail"]


def test_resposta_abaixo_do_minimo_e_422_com_o_numero(cliente: TestClient) -> None:
    quem = _papeis(cliente)["anotador"][0]
    env = _env_severidade(cliente, quem)
    corpo = _payload_severidade(env, respostas={"user-goal": "too short"})
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": corpo},
    )
    assert r.status_code == 422
    assert "mínimo é 30" in r.json()["detail"]


def test_resposta_para_pergunta_nao_declarada_e_422(cliente: TestClient) -> None:
    """O simétrico do critério intruso: o payload não pode afirmar uma resposta
    que o instrumento nunca perguntou."""
    quem = _papeis(cliente)["anotador"][0]
    env = _env_severidade(cliente, quem)
    corpo = _payload_severidade(env)
    corpo["respostas"]["inventada"] = "x" * 40
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": corpo},
    )
    assert r.status_code == 422
    assert "não declara" in r.json()["detail"]


def test_a_resposta_valida_grava_no_payload_com_o_schema_novo(
    cliente: TestClient, semeado: Path
) -> None:
    quem = _papeis(cliente)["anotador"][0]
    env = _env_severidade(cliente, quem)
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": _payload_severidade(env)},
    )
    assert r.status_code == 200, r.text
    conn = dbmod.connect(semeado)
    try:
        linha = conn.execute(
            "SELECT payload_schema, payload_json FROM anotacoes WHERE id = ?",
            (r.json()["anotacao_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert str(linha["payload_schema"]) == "avaliar_rubrica@3"
    gravado = json.loads(str(linha["payload_json"]))
    assert gravado["respostas"]["user-goal"].startswith("The person wanted")


def test_rubrica_sem_perguntas_continua_como_sempre(cliente: TestClient) -> None:
    """Os onze itens pré-P9 não declaram pergunta nenhuma, e nada muda para
    eles — inclusive mandar `respostas` vazio é aceito."""
    quem = _papeis(cliente)["anotador"][1]
    env = cliente.post(
        "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": "avaliar_rubrica"}
    ).json()["tarefa"]
    while env and env["rubrica"]["titulo"] == "Severity review":
        cliente.post(
            f"/api/atribuicoes/{env['atribuicao_id']}/abandonar",
            json={"anotador_id": quem},
        )
        env = cliente.post(
            "/api/tarefas/proxima", json={"anotador_id": quem, "tipo": "avaliar_rubrica"}
        ).json()["tarefa"]
    assert env is not None, "só havia a tarefa de severidade na fila"
    assert env["rubrica"]["perguntas"] == []
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": notas_da_rubrica(env)},
    )
    assert r.status_code == 200, r.text


def test_uma_resposta_booleana_e_recusada_antes_de_virar_texto() -> None:
    """Quarta aparição da pegadinha: `True` viraria "True", uma resposta válida
    e inventada."""
    from prompt_factory.annotate import payloads

    with pytest.raises(ValueError, match="não é texto"):
        payloads.AvaliarRubrica.model_validate(
            {"notas": [{"criterio": "x", "nota": 3}], "respostas": {"user-goal": True}}
        )


def test_editar_uma_resposta_no_RR_e_UM_caminho_de_diff() -> None:
    """A lição do `tipos_issue`, aplicada às respostas: mapa com todos os ids
    sempre presentes — cada edição do revisor custa exatamente um motivo."""
    from prompt_factory.annotate import avaliacoes as avmod

    antes = {"notas": [{"criterio": "x", "nota": 3}], "respostas": {"user-goal": "a" * 30}}
    depois = {"notas": [{"criterio": "x", "nota": 3}], "respostas": {"user-goal": "b" * 30}}
    diff = avmod.diferencas(antes, depois)
    assert [d["campo"] for d in diff] == ["respostas.user-goal"]


def test_o_glossario_da_entrega_explica_o_campo_novo() -> None:
    from prompt_factory.annotate import entrega, payloads

    campos = set(payloads.AvaliarRubrica.model_fields)
    assert campos <= set(entrega.GLOSSARIO) | {"notas"}, campos - set(entrega.GLOSSARIO)
    assert "respostas" in entrega.GLOSSARIO


def test_a_tela_desenha_e_valida_as_perguntas(js: str) -> None:
    """As cinco superfícies do JS: desenho, form, falta, payload, leitura."""
    assert "function caixasDePergunta(" in js
    assert "function perguntasDe(" in js
    assert 'form.respostas[pergunta.id]' in js
    assert '"falta.pergunta"' in js and '"falta.pergunta_minimo"' in js
    # E o payload emite o mapa na ordem do INSTRUMENTO, nunca na ordem do clique.
    assert "corpo.respostas[pergunta.id]" in js


def test_o_seed_acrescenta_a_pergunta_a_uma_fixture_ja_semeada(
    tmp_path: Path, corpus: Path
) -> None:
    """O caso do banco REAL: a rubrica de severidade foi semeada antes de o
    instrumento perguntar qualquer coisa. Acrescentar perguntas a uma fixture
    sem nenhuma não muda a leitura de anotação alguma (nenhum payload antigo
    referencia `respostas`) — EDITAR uma pergunta existente continua proibido."""
    banco = tmp_path / "annotate.sqlite"
    conn = dbmod.connect(banco)
    corpo = dbmod.connect(corpus, readonly=True)
    try:
        adb.init_db(conn)
        # Semeia o pacote SEM as perguntas — o estado que o banco do dono tem.
        pacote = seedmod.carregar_pacote()
        podado = json.loads(json.dumps(pacote))
        for item in podado["itens"]:
            item["rubrica"].pop("perguntas", None)
        seedmod.semear_personas(conn)
        seedmod.semear_pacote(conn, podado)
        antes = conn.execute(
            "SELECT criterios_json FROM rubricas WHERE titulo = 'Severity review'"
        ).fetchone()
        assert "perguntas" not in json.loads(str(antes["criterios_json"]))

        # O seed de HOJE acrescenta a pergunta — e é idempotente depois.
        conta = seedmod.semear_pacote(conn)
        assert conta["traduzidas"] >= 1
        depois = json.loads(
            str(
                conn.execute(
                    "SELECT criterios_json FROM rubricas WHERE titulo = 'Severity review'"
                ).fetchone()["criterios_json"]
            )
        )
        assert [q["id"] for q in depois["perguntas"]] == ["user-goal", "task-type", "domain"]
        # O blob guarda a REFERÊNCIA (`vocabulario`), nunca as 16 classes: a
        # expansão é da leitura, e materializar copiaria a taxonomia para o
        # banco — N cópias mentindo na primeira classe renomeada.
        assert depois["perguntas"][1]["vocabulario"] == "task_type"
        assert "opcoes" not in depois["perguntas"][1]
        assert seedmod.semear_pacote(conn)["traduzidas"] == 0

        # E a porta é ESTREITA: mudar uma pergunta existente continua recusado.
        mudado = json.loads(json.dumps(pacote))
        alvo = next(
            i for i in mudado["itens"] if i["chave"] == "procedimento-incidente-academia"
        )
        alvo["rubrica"]["perguntas"][0]["minimo"] = 99
        seedmod.semear_pacote(conn, mudado)
        final = json.loads(
            str(
                conn.execute(
                    "SELECT criterios_json FROM rubricas WHERE titulo = 'Severity review'"
                ).fetchone()["criterios_json"]
            )
        )
        assert final["perguntas"][0]["minimo"] == 30, "pergunta editada por baixo"
    finally:
        conn.close()
        corpo.close()


# ---------------------------------------------------------------------------
# 10. a pergunta de CATEGORIA — o laço com o corpus vira dado
# ---------------------------------------------------------------------------


def test_normalizar_pergunta_conhece_os_dois_tipos_e_degrada_o_desconhecido() -> None:
    """Tipo fora da tupla vira caixa livre, nunca exceção: a normalização roda
    na LEITURA, e derrubar a tarefa por um typo no blob seria pior que mostrar
    uma caixa de texto onde se esperava um seletor — a degradação é visível."""
    assert tarmod.normalizar_pergunta({"id": "x"})["tipo"] == "texto"
    assert tarmod.normalizar_pergunta({"id": "x", "tipo": "categoria"})["tipo"] == "categoria"
    assert tarmod.normalizar_pergunta({"id": "x", "tipo": "categora"})["tipo"] == "texto"


def test_o_minimo_de_uma_categoria_e_zero_por_construcao() -> None:
    """O comprimento de um id não mede nada — um mínimo herdado de 30 tornaria
    `codigo` (6 caracteres) uma resposta impossível: o 422 que nenhum select
    satisfaz."""
    p = tarmod.normalizar_pergunta({"id": "x", "tipo": "categoria", "minimo": 30})
    assert p["minimo"] == 0


def test_opcoes_aceitam_a_forma_curta_como_o_catalogo_de_tipos_issue() -> None:
    p = tarmod.normalizar_pergunta({"id": "x", "tipo": "categoria", "opcoes": ["a", "b"]})
    assert p["opcoes"] == [{"id": "a", "rotulo": "a"}, {"id": "b", "rotulo": "b"}]


def test_a_expansao_do_vocabulario_acontece_na_leitura() -> None:
    """O blob guarda `vocabulario: "task_type"`; as 16 classes saem da taxonomia
    na leitura, com as duas línguas — canônico inglês, que é a convenção de todo
    conteúdo de instrumento."""
    (p,) = tarmod.perguntas_da_rubrica(
        {"perguntas": [{"id": "task-type", "tipo": "categoria", "vocabulario": "task_type"}]}
    )
    ids = [o["id"] for o in p["opcoes"]]
    assert len(ids) == 16 and "geracao-criativa" in ids and "outro" in ids
    criativa = next(o for o in p["opcoes"] if o["id"] == "geracao-criativa")
    assert criativa["rotulo"] == "Creative writing"
    assert criativa["rotulo_i18n"] == {"en": "Creative writing", "pt": "Geração criativa"}


def test_opcoes_declaradas_vencem_a_referencia_e_a_expansao_e_idempotente() -> None:
    """O que a pergunta declarar VENCE (vocabulário próprio de um instrumento é
    legítimo) — e é isso que torna reexpandir uma lista já expandida inócuo:
    `erro_contra_a_rubrica` chama `perguntas_da_rubrica` sobre uma rubrica que o
    envelope já expandiu."""
    rubrica = {
        "perguntas": [
            {
                "id": "x",
                "tipo": "categoria",
                "vocabulario": "task_type",
                "opcoes": [{"id": "so-esta", "rotulo": "Só esta"}],
            }
        ]
    }
    (uma_vez,) = tarmod.perguntas_da_rubrica(rubrica)
    assert [o["id"] for o in uma_vez["opcoes"]] == ["so-esta"]
    (duas_vezes,) = tarmod.perguntas_da_rubrica({"perguntas": [uma_vez]})
    assert duas_vezes == uma_vez


def test_resposta_fora_do_vocabulario_e_recusada_com_o_id_errado_na_frase() -> None:
    rubrica = {
        "criterios": [],
        "perguntas": [{"id": "task-type", "tipo": "categoria", "vocabulario": "task_type"}],
    }
    erro = tarmod.erro_contra_a_rubrica(rubrica, [], {"task-type": "nao-existe"})
    assert erro is not None and "não é uma categoria" in erro and "nao-existe" in erro
    assert tarmod.erro_contra_a_rubrica(rubrica, [], {"task-type": "codigo"}) is None


def test_categoria_sem_vocabulario_em_maos_deixa_passar() -> None:
    """Recusar exige certeza (a lição do s02): sem opções em mãos o
    pertencimento não é conferível, e um 422 que nenhum valor satisfaz travaria
    a submissão por causa de um arquivo."""
    rubrica = {"criterios": [], "perguntas": [{"id": "x", "tipo": "categoria"}]}
    assert tarmod.erro_contra_a_rubrica(rubrica, [], {"x": "qualquer-coisa"}) is None


def test_o_modelo_de_severidade_classifica_nos_DOIS_eixos_do_corpus() -> None:
    """As duas perguntas de categoria apontam para as seções reais da taxonomia
    — e um modelo com perguntas continua fora do formulário do anotador."""
    modelo = next(m for m in rubricas_modelo.carregar() if m["id"] == "severidade")
    por_id = {p["id"]: p for p in modelo["perguntas"]}
    assert list(por_id) == ["user-goal", "task-type", "domain"]
    assert por_id["task-type"]["vocabulario"] == "task_type"
    assert por_id["domain"]["vocabulario"] == "domain"
    assert all(p["obrigatoria"] for p in por_id.values())
    assert modelo["cabe_no_formulario"] is False


def test_o_arsenal_recusa_categoria_sem_vocabulario_conhecido(tmp_path: Path) -> None:
    """Portão de FIXTURE: um arquivo do repositório falha no terminal, não como
    um seletor que nasce vazio na tela."""
    torto = {
        "schema": rubricas_modelo.SCHEMA,
        "modelos": [
            {
                "id": "m",
                "nome_chave": "modelo.m",
                "titulo": "M",
                "escala_min": 1,
                "escala_max": 3,
                "criterios": [{"nome": "C", "rotulo_min": "a", "rotulo_max": "b"}],
                "perguntas": [{"id": "x", "tipo": "categoria", "vocabulario": "task_typo"}],
            }
        ],
    }
    caminho = tmp_path / "modelos.json"
    caminho.write_text(json.dumps(torto), encoding="utf-8")
    with pytest.raises(rubricas_modelo.ModeloInvalido, match="vocabulário"):
        rubricas_modelo.carregar(caminho)


def test_a_categoria_valida_grava_os_ids_da_taxonomia_no_payload(
    cliente: TestClient, semeado: Path
) -> None:
    """O momento em que o laço fecha: o payload entregue carrega o id que o
    corpus tem como nulo nas 159.733 linhas."""
    quem = _papeis(cliente)["anotador"][0]
    env = _env_severidade(cliente, quem)
    corpo = _payload_severidade(env)
    corpo["respostas"]["task-type"] = "redacao-pratica"
    corpo["respostas"]["domain"] = "esportes"
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": corpo},
    )
    assert r.status_code == 200, r.text
    conn = dbmod.connect(semeado)
    try:
        gravado = json.loads(
            str(
                conn.execute(
                    "SELECT payload_json FROM anotacoes WHERE id = ?",
                    (r.json()["anotacao_id"],),
                ).fetchone()["payload_json"]
            )
        )
    finally:
        conn.close()
    assert gravado["respostas"]["task-type"] == "redacao-pratica"
    assert gravado["respostas"]["domain"] == "esportes"


def test_um_id_inventado_e_422_de_ponta_a_ponta(cliente: TestClient) -> None:
    quem = _papeis(cliente)["anotador"][0]
    env = _env_severidade(cliente, quem)
    corpo = _payload_severidade(env)
    corpo["respostas"]["domain"] = "memes"
    r = cliente.post(
        f"/api/atribuicoes/{env['atribuicao_id']}/submeter",
        json={"anotador_id": quem, "payload": corpo},
    )
    assert r.status_code == 422
    assert "não é uma categoria" in r.json()["detail"]


def test_o_seed_acrescenta_categorias_a_fixture_que_JA_TINHA_o_user_goal(
    tmp_path: Path, corpus: Path
) -> None:
    """O caso do banco REAL deste passe: a fixture já tinha a pergunta de user
    goal quando as de categoria chegaram. A porta generalizada aceita o
    acréscimo (toda pergunta existente preservada, idêntica) e continua
    recusando REMOÇÃO — o outro jeito de editar."""
    banco = tmp_path / "annotate.sqlite"
    conn = dbmod.connect(banco)
    corpo = dbmod.connect(corpus, readonly=True)
    try:
        adb.init_db(conn)
        pacote = seedmod.carregar_pacote()
        so_goal = json.loads(json.dumps(pacote))
        for item in so_goal["itens"]:
            perguntas = (item["rubrica"] or {}).get("perguntas")
            if perguntas:
                # A forma que o banco REAL tem: o normalizador do P9-2 não
                # emitia `tipo`, então a pergunta gravada não carrega a chave.
                # Foi exatamente esta diferença que fez a primeira versão da
                # porta recusar o acréscimo no banco do dono — a comparação tem
                # de passar pelo normalizador, que é quem define "a mesma".
                item["rubrica"]["perguntas"] = [
                    {k: v for k, v in p.items() if k != "tipo"}
                    for p in perguntas
                    if p["id"] == "user-goal"
                ]
        seedmod.semear_personas(conn)
        seedmod.semear_pacote(conn, so_goal)

        assert seedmod.semear_pacote(conn)["traduzidas"] >= 1
        depois = json.loads(
            str(
                conn.execute(
                    "SELECT criterios_json FROM rubricas WHERE titulo = 'Severity review'"
                ).fetchone()["criterios_json"]
            )
        )
        assert [q["id"] for q in depois["perguntas"]] == ["user-goal", "task-type", "domain"]

        # Remover uma pergunta existente é edição, e edição é recusada.
        sem_goal = json.loads(json.dumps(pacote))
        for item in sem_goal["itens"]:
            perguntas = (item["rubrica"] or {}).get("perguntas")
            if perguntas:
                item["rubrica"]["perguntas"] = [p for p in perguntas if p["id"] != "user-goal"]
        seedmod.semear_pacote(conn, sem_goal)
        final = json.loads(
            str(
                conn.execute(
                    "SELECT criterios_json FROM rubricas WHERE titulo = 'Severity review'"
                ).fetchone()["criterios_json"]
            )
        )
        assert [q["id"] for q in final["perguntas"]] == ["user-goal", "task-type", "domain"]
    finally:
        conn.close()
        corpo.close()


def test_a_tela_desenha_o_seletor_e_mostra_o_id_junto_do_nome(js: str) -> None:
    """O seletor nasce do mesmo `caixasDePergunta`; a opção carrega nome E id
    (o contrato do chip de categoria do brief); o botão pede a ação certa."""
    assert 'pergunta.tipo === "categoria"' in js
    assert '"falta.pergunta_categoria"' in js
    assert 't("pergunta.escolher")' in js
    assert 'bi(o.rotulo_i18n, o.rotulo) + " · " + o.id' in js
