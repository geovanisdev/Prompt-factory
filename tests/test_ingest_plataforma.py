"""P6: ``pf ingest plataforma`` — a criação escrita na Bancada vira linha do corpus.

O P4 provou que a plataforma calcula os mesmos valores que a pipeline. Este
marco fecha o círculo, e a classe de defeito que ele persegue é a mesma —
**divergência silenciosa** — só que agora ela tem três lugares novos para se
esconder:

* a fonte, a licença e o ``source_id`` do parquet têm de reproduzir o
  ``uid_previsto`` que a aprovação prometeu (nada explode se não reproduzirem:
  o corpus só ganha uma linha com outro uid, e a Bancada aponta para o nada);
* o passe reescreve o parquet INTEIRO, então esquecer as já exportadas apagaria
  do corpus tudo o que entrou antes — sem erro, só com menos linhas;
* o carimbo ``exportada`` tem de vir DEPOIS do arquivo, senão o funil promete
  uma ingestão que não aconteceu.

E um quarto, que só aparece quando se olha o dedup: uma criação idêntica a um
prompt real **não pode** ganhar o desempate do canônico, senão colar um texto do
corpus no formulário reescreveria a proveniência daquela linha.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from prompt_factory import cli, config, dedup, schema, textnorm
from prompt_factory import db as dbmod
from prompt_factory.annotate import criacoes as crimod
from prompt_factory.annotate import db as adb
from prompt_factory.annotate import seed as seedmod
from prompt_factory.ingest import REGISTRY, base, default_sources, plataforma, resolve_names
from prompt_factory.ingest.base import RAW_COLUMNS
from prompt_factory.stages import NORMALIZED, StageConfig, s01_normalize

from .test_api import montar_banco

TEXTO_PT = (
    "Preciso de um roteiro de duas semanas pelo interior de Minas saindo de Belo "
    "Horizonte, sem carro, usando só ônibus intermunicipal."
)
TEXTO_EN = (
    "Draft a one-paragraph incident note for a climbing gym after a hold spun "
    "during a lead climb, addressed to the members who were on the wall."
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    caminho = tmp_path / "prompts.sqlite"
    montar_banco(caminho)
    return caminho


@pytest.fixture
def banco(tmp_path: Path, corpus: Path) -> Path:
    """``annotate.sqlite`` semeado, como o de uma máquina de verdade."""
    caminho = tmp_path / "annotate.sqlite"
    conn = dbmod.connect(caminho)
    corpo = dbmod.connect(corpus, readonly=True)
    try:
        adb.init_db(conn)
        seedmod.semear(conn, corpo)
    finally:
        conn.close()
        corpo.close()
    return caminho


def _quem(conn: Any) -> tuple[int, int]:
    """``(autor, revisor)`` — dois ids de personas distintas."""
    autor = conn.execute(
        "SELECT id FROM anotadores WHERE papel = 'anotador' ORDER BY id LIMIT 1"
    ).fetchone()
    revisor = conn.execute(
        "SELECT id FROM anotadores WHERE papel IN ('revisor','admin') ORDER BY id LIMIT 1"
    ).fetchone()
    return int(autor["id"]), int(revisor["id"])


def criar_e_aprovar(
    banco: Path, corpus: Path, textos: tuple[str, ...] = (TEXTO_PT,), *, aprovar: bool = True
) -> list[int]:
    """Escreve N criações e (por padrão) aprova todas. Devolve os ids."""
    conn = dbmod.connect(banco)
    corpo = dbmod.connect(corpus, readonly=True)
    try:
        autor, revisor = _quem(conn)
        ids = []
        for i, texto in enumerate(textos):
            linha = crimod.criar(
                conn, corpo, autor_id=autor, texto=texto,
                lang="en" if i % 2 else "pt",
                task_type_sugerido="planejamento",
                brief="quero comparar respostas longas contra respostas curtas",
            )
            ids.append(int(linha["id"]))
            if aprovar:
                crimod.revisar(conn, linha["id"], revisor_id=revisor, aprovar=True, comentario=None)
        return ids
    finally:
        conn.close()
        corpo.close()


def ler_parquet(caminho: Path) -> list[dict[str, Any]]:
    return pq.read_table(caminho).to_pylist()


def status_de(banco: Path) -> dict[int, str]:
    conn = dbmod.connect(banco, readonly=True)
    try:
        return {
            int(x["id"]): str(x["status"])
            for x in conn.execute("SELECT id, status FROM criacoes")
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. as constantes que precisam concordar entre três arquivos
# ---------------------------------------------------------------------------


def test_a_licenca_e_a_mesma_no_enum_no_toml_e_na_plataforma() -> None:
    """Três lugares dizem "cc0-1.0", e divergir não levanta erro em lugar nenhum.

    O ``write_raw`` carimba a licença do TOML na linha; a plataforma promete
    ``criacoes.LICENCA`` no formulário; o ``schema.License`` é o vocabulário que
    o dedup e o export conhecem. Se o TOML dissesse outra coisa, a criação sairia
    do formulário sob uma licença e entraria no corpus sob outra — e o único
    lugar onde isso apareceria é numa auditoria jurídica, tarde demais.
    """
    spec = config.sources()["plataforma"]
    assert crimod.LICENCA == schema.License.CC0_1_0.value == str(spec["license"])
    politica = schema.license_policy(crimod.LICENCA)
    assert (politica.commercial_ok, politica.redistributable) == (True, True)
    assert bool(spec["commercial_ok"]) is politica.commercial_ok
    assert bool(spec["redistributable"]) is politica.redistributable


def test_o_nome_da_fonte_e_a_secao_do_toml_e_o_modulo() -> None:
    assert plataforma.SOURCE == crimod.FONTE == "plataforma"
    assert "plataforma" in config.sources()
    assert REGISTRY["plataforma"] is plataforma


def test_a_secao_plataforma_e_a_ULTIMA_do_sources_toml() -> None:
    """A ordem das seções é o critério 2 do desempate do canônico.

    Não é ela que decide o caso da plataforma (ver o teste do dedup abaixo), mas
    a posição editorial continua sendo a certa: a fonte local vem depois de tudo
    o que foi publicado por terceiros.
    """
    assert list(config.sources())[-1] == "plataforma"


# ---------------------------------------------------------------------------
# 2. as duas guardas contra ingerir o próprio demo sem pedir
# ---------------------------------------------------------------------------


def test_pf_ingest_sem_argumentos_NAO_inclui_a_plataforma() -> None:
    """A DoD do marco. Ingerir o que a própria plataforma escreveu é ato explícito."""
    assert "plataforma" not in default_sources()
    assert "plataforma" not in resolve_names([])
    assert "plataforma" not in resolve_names(["all"])


def test_mas_pedida_pelo_nome_ela_resolve() -> None:
    assert resolve_names(["plataforma"]) == ["plataforma"]


def test_sao_DUAS_guardas_independentes() -> None:
    """``default_on = false`` no TOML **e** ausência de ``ORDEM_PADRAO``.

    Uma só bastaria hoje; duas existem porque o modo de falha é mudo — o corpus
    ganharia linhas de demonstração e nada nele denunciaria isso depois.
    """
    from prompt_factory.ingest import ORDEM_PADRAO

    assert config.sources()["plataforma"]["default_on"] is False
    assert "plataforma" not in ORDEM_PADRAO


# ---------------------------------------------------------------------------
# 3. a linha: 12 colunas, o uid prometido e o que NÃO viaja
# ---------------------------------------------------------------------------


def test_a_linha_tem_as_12_colunas_do_raw_schema(banco: Path, corpus: Path) -> None:
    criar_e_aprovar(banco, corpus)
    conn = dbmod.connect(banco, readonly=True)
    try:
        linha = plataforma.montar_linha(plataforma.selecionar(conn)[0])
    finally:
        conn.close()
    assert set(linha) == set(RAW_COLUMNS)
    assert linha["source"] == "plataforma"
    # A licença sai VAZIA daqui: quem carimba é o write_raw, a partir do TOML.
    assert linha["license"] == ""


def test_o_source_id_e_o_id_da_criacao_e_e_ele_que_reproduz_o_uid(
    banco: Path, corpus: Path
) -> None:
    """O teste central do marco.

    Com ``source_id`` presente, ``make_uid`` sai de ``"plataforma:<id>"`` e não
    depende do texto — é isso que faz o uid da aprovação e o uid da pipeline
    serem o mesmo, e é isso que reingerir a mesma criação reproduz.
    """
    (criacao_id,) = criar_e_aprovar(banco, corpus)
    conn = dbmod.connect(banco, readonly=True)
    try:
        bruta = plataforma.selecionar(conn)[0]
        linha = plataforma.montar_linha(bruta)
        prometido = str(bruta["uid_previsto"])
    finally:
        conn.close()

    assert linha["source_id"] == str(criacao_id)
    esperado = schema.make_uid(linha["source"], linha["source_id"], linha["text_raw"])
    assert esperado == prometido
    assert esperado == crimod.uid_previsto(criacao_id, linha["text_raw"])


def test_uid_gravado_divergente_PARA_o_passe(banco: Path, corpus: Path) -> None:
    """A única forma de essa classe de erro fazer barulho.

    Um ``uid_previsto`` que não bate com o recalculado significa que a cadeia
    divergiu entre a plataforma e a pipeline. O sintoma natural seria mudo: o
    corpus ganharia a linha com o uid novo e a Bancada mostraria o velho para
    sempre.
    """
    (criacao_id,) = criar_e_aprovar(banco, corpus)
    conn = dbmod.connect(banco)
    try:
        conn.execute(
            "UPDATE criacoes SET uid_previsto = 'deadbeefdeadbeef' WHERE id = ?",
            (criacao_id,),
        )
        linha = plataforma.selecionar(conn)[0]
        with pytest.raises(SystemExit, match="uid prometido"):
            plataforma.montar_linha(linha)
    finally:
        conn.close()


def test_o_idioma_declarado_viaja_como_lang_source(banco: Path, corpus: Path) -> None:
    """Prior, não veredito: o s01 copia ``pt``/``en`` exatos e o s02 roda os dois
    detectores em toda linha de qualquer jeito — a declaração só aciona o árbitro
    quando o detector discorda dela."""
    criar_e_aprovar(banco, corpus, (TEXTO_PT, TEXTO_EN))
    conn = dbmod.connect(banco, readonly=True)
    try:
        linhas = [plataforma.montar_linha(x) for x in plataforma.selecionar(conn)]
    finally:
        conn.close()
    assert [x["lang_source"] for x in linhas] == ["pt", "en"]
    assert set(linhas[0]["lang_source"]) <= set("pt")  # nada de "Portuguese"


def test_o_meta_json_nao_carrega_texto_livre(banco: Path, corpus: Path) -> None:
    """O s03 limpa PII de ``text``, e só de ``text``.

    O ``brief`` do autor é campo livre; entrando pelo metadado ele atravessaria
    a pipeline inteira sem passar pela peneira. As sugestões, que são ids da
    taxonomia, vão — mas fora de ``native_category``, que é a coluna que o s08
    promoveria a rótulo.
    """
    criar_e_aprovar(banco, corpus)
    conn = dbmod.connect(banco, readonly=True)
    try:
        linha = plataforma.montar_linha(plataforma.selecionar(conn)[0])
    finally:
        conn.close()
    meta = json.loads(linha["meta_json"])
    assert "brief" not in meta and "comentario_revisao" not in meta
    assert "autor" not in meta and "revisor" not in meta
    assert meta["task_type_sugerido"] == "planejamento"
    assert linha["native_category"] == ""


def test_a_normalizacao_do_texto_guardado_e_idempotente(banco: Path, corpus: Path) -> None:
    """O texto do banco já passou por ``norm_display`` no envio, e o s01 vai
    aplicá-la de novo. Se não fosse idempotente, o corpus guardaria um texto
    diferente do que a plataforma mostra — sem erro nenhum."""
    criar_e_aprovar(banco, corpus, ("  Olá,   MUNDO!!!  \r\n\r\n\r\n  planeje uma viagem  ",))
    conn = dbmod.connect(banco, readonly=True)
    try:
        linha = plataforma.montar_linha(plataforma.selecionar(conn)[0])
    finally:
        conn.close()
    assert textnorm.norm_display(linha["text_raw"]) == linha["text_raw"]


# ---------------------------------------------------------------------------
# 4. o passe: quem entra, quem é carimbado, e em que ordem
# ---------------------------------------------------------------------------


def test_so_aprovada_e_exportada_entram(banco: Path, corpus: Path) -> None:
    ids = criar_e_aprovar(banco, corpus, (TEXTO_PT, TEXTO_EN), aprovar=False)
    conn = dbmod.connect(banco)
    try:
        _, revisor = _quem(conn)
        crimod.revisar(conn, ids[0], revisor_id=revisor, aprovar=True, comentario=None)
        crimod.revisar(
            conn, ids[1], revisor_id=revisor, aprovar=False,
            comentario="isto é um pedido de tradução, não um prompt de instrução",
        )
        selecionadas = [int(x["id"]) for x in plataforma.selecionar(conn)]
    finally:
        conn.close()
    assert selecionadas == [ids[0]]


def test_reingerir_LEVA_as_ja_exportadas_junto(banco: Path, corpus: Path, tmp_path: Path) -> None:
    """O que parece trabalho repetido é o que impede a perda de linhas.

    O ``write_raw`` reescreve o parquet inteiro. Um passe que levasse só as
    novas produziria um arquivo sem as antigas, e o próximo ``pf run`` apagaria
    do corpus tudo o que entrou antes — sem erro, só com menos linhas.
    """
    raw = tmp_path / "raw"
    primeiro = criar_e_aprovar(banco, corpus, (TEXTO_PT,))
    conn = dbmod.connect(banco)
    try:
        r1 = plataforma.exportar(conn, raw_dir=raw)
        assert (r1["linhas"], r1["aprovadas"], r1["carimbadas"]) == (1, 1, 1)
    finally:
        conn.close()

    segundo = criar_e_aprovar(banco, corpus, (TEXTO_EN,))
    conn = dbmod.connect(banco)
    try:
        r2 = plataforma.exportar(conn, raw_dir=raw)
    finally:
        conn.close()

    assert (r2["linhas"], r2["aprovadas"], r2["reexportadas"]) == (2, 1, 1)
    ids = [int(json.loads(x["meta_json"])["criacao_id"]) for x in ler_parquet(r2["parquet"])]
    assert ids == [primeiro[0], segundo[0]]


def test_o_carimbo_so_toca_as_aprovadas(banco: Path, corpus: Path, tmp_path: Path) -> None:
    """``exportada_em`` de quem já era exportada não se mexe: a data descreve a
    primeira vez, e reescrever o parquet não é uma exportação nova."""
    criar_e_aprovar(banco, corpus, (TEXTO_PT,))
    conn = dbmod.connect(banco)
    try:
        plataforma.exportar(conn, raw_dir=tmp_path / "raw")
        antes = conn.execute("SELECT id, exportada_em FROM criacoes").fetchone()["exportada_em"]
        resumo = plataforma.exportar(conn, raw_dir=tmp_path / "raw")
        depois = conn.execute("SELECT exportada_em FROM criacoes").fetchone()["exportada_em"]
    finally:
        conn.close()
    assert resumo["carimbadas"] == 0
    assert depois == antes


def test_a_escrita_vem_ANTES_do_carimbo(
    banco: Path, corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disciplina do part-file do WildChat (part -> state), pelo mesmo motivo.

    Carimbar primeiro e falhar na escrita deixaria o funil dizendo "exportada"
    sobre uma linha que nunca chegou a ``data/raw/`` — o painel do admin
    prometeria uma ingestão que ninguém rodou.
    """
    (criacao_id,) = criar_e_aprovar(banco, corpus)

    def explodir(*a: Any, **k: Any) -> Path:
        raise OSError("disco cheio no meio da escrita")

    monkeypatch.setattr(base, "write_raw", explodir)
    conn = dbmod.connect(banco)
    try:
        with pytest.raises(OSError, match="disco cheio"):
            plataforma.exportar(conn, raw_dir=tmp_path / "raw")
    finally:
        conn.close()
    assert status_de(banco)[criacao_id] == "aprovada"


def test_o_evento_do_passe_e_UM_e_nao_um_por_linha(
    banco: Path, corpus: Path, tmp_path: Path
) -> None:
    ids = criar_e_aprovar(banco, corpus, (TEXTO_PT, TEXTO_EN))
    conn = dbmod.connect(banco)
    try:
        plataforma.exportar(conn, raw_dir=tmp_path / "raw")
        linhas = conn.execute(
            "SELECT detalhe_json, entidade_id FROM eventos WHERE acao = 'criacao_exportada'"
        ).fetchall()
    finally:
        conn.close()
    assert len(linhas) == 1
    assert linhas[0]["entidade_id"] is None
    detalhe = json.loads(str(linhas[0]["detalhe_json"]))
    assert detalhe["n"] == 2 and detalhe["ids"] == ids


def test_ensaio_escreve_o_parquet_e_NAO_carimba(
    banco: Path, corpus: Path, tmp_path: Path
) -> None:
    (criacao_id,) = criar_e_aprovar(banco, corpus)
    conn = dbmod.connect(banco)
    try:
        resumo = plataforma.exportar(conn, raw_dir=tmp_path / "raw", marcar=False)
    finally:
        conn.close()
    assert resumo["linhas"] == 1 and resumo["carimbadas"] == 0
    assert resumo["parquet"].is_file()
    assert status_de(banco)[criacao_id] == "aprovada"


# ---------------------------------------------------------------------------
# 5. o parquet e a costura com a pipeline
# ---------------------------------------------------------------------------


def test_a_licenca_e_carimbada_em_TODA_linha_pelo_write_raw(
    banco: Path, corpus: Path, tmp_path: Path
) -> None:
    """A licença por linha é a tese do projeto — e ela não sai do ingester."""
    criar_e_aprovar(banco, corpus, (TEXTO_PT, TEXTO_EN))
    conn = dbmod.connect(banco)
    try:
        resumo = plataforma.exportar(conn, raw_dir=tmp_path / "raw", marcar=False)
    finally:
        conn.close()
    linhas = ler_parquet(resumo["parquet"])
    assert {x["license"] for x in linhas} == {"cc0-1.0"}
    tabela = pq.read_table(resumo["parquet"])
    assert tuple(tabela.schema.names) == RAW_COLUMNS
    assert tabela.schema.metadata[b"pf_license"] == b"cc0-1.0"


def test_o_s01_produz_EXATAMENTE_o_uid_prometido(
    banco: Path, corpus: Path, tmp_path: Path
) -> None:
    """O smoke da DoD, na parte que é determinística e barata.

    criação aprovada -> ingest -> s01 -> o uid do universo. Os estágios s02..s06
    não mudam uid nenhum (o s06 só REMOVE linhas), então é aqui que a promessa
    do ``uid_previsto`` se cumpre ou se quebra.
    """
    ids = criar_e_aprovar(banco, corpus, (TEXTO_PT, TEXTO_EN))
    cfg = StageConfig(data_dir=tmp_path / "data")
    cfg.preparar_dirs()

    conn = dbmod.connect(banco)
    try:
        plataforma.exportar(conn, raw_dir=cfg.raw, marcar=False)
        prometidos = [
            str(x["uid_previsto"])
            for x in conn.execute("SELECT uid_previsto FROM criacoes ORDER BY id")
        ]
    finally:
        conn.close()

    assert s01_normalize.run(cfg) == 0
    tabela = pq.read_table(cfg.caminho(NORMALIZED))
    saida = tabela.to_pylist()
    assert [x["uid"] for x in saida] == prometidos
    assert [x["source_id"] for x in saida] == [str(i) for i in ids]
    assert {x["license"] for x in saida} == {"cc0-1.0"}
    assert all(x["commercial_ok"] and x["redistributable"] for x in saida)
    # `lang` provisório: o s01 aceita pt/en exatos e deixa o s02 confirmar.
    assert [x["lang"] for x in saida] == ["pt", "en"]


def test_o_dedup_prefere_o_ORIGINAL_e_nao_a_criacao(banco: Path, corpus: Path) -> None:
    """Colar um prompt do corpus no formulário não pode reescrever a proveniência.

    O critério 1 do canônico é a licença, e o ``cc0-1.0`` é a mais permissiva que
    existe — se ela ganhasse o desempate, a linha do aya (com autor, citação e
    atribuição publicada) seria descartada em favor de uma linha cuja
    proveniência é o nosso próprio demo. Em silêncio.
    """
    criacao = {
        "uid": "aaaa000000000000",
        "license": crimod.LICENCA,
        "source": "plataforma",
        "source_id": "1",
    }
    original = {
        "uid": "bbbb000000000000",
        "license": "apache-2.0",
        "source": "aya",
        "source_id": "9",
    }
    assert dedup.choose_canonical([criacao, original]) is original
    assert dedup.choose_canonical([original, criacao]) is original
    # E continua atrás de todas as (True, True) do Hub, não só do aya.
    rank = dedup.LICENSE_RANK
    permissivas = [
        lic for lic, pol in schema.LICENSE_POLICY.items()
        if pol.commercial_ok and pol.redistributable and lic != crimod.LICENCA
    ]
    assert all(rank[lic] < rank[crimod.LICENCA] for lic in permissivas)
    # ...mas à frente de tudo o que tem política mais fechada.
    fechadas = [
        lic for lic, pol in schema.LICENSE_POLICY.items()
        if not (pol.commercial_ok and pol.redistributable)
    ]
    assert all(rank[lic] > rank[crimod.LICENCA] for lic in fechadas)


# ---------------------------------------------------------------------------
# 6. a CLI
# ---------------------------------------------------------------------------


def test_a_cli_roda_o_passe_em_ensaio_com_data_dir(
    banco: Path, corpus: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--data-dir`` contra o banco de verdade não pode mover o funil.

    O prejuízo seria auto-curável (o passe seguinte relê as exportadas), mas um
    funil que mente até alguém reparar é pior que um passe que não carimba e
    diz isso na tela.
    """
    (criacao_id,) = criar_e_aprovar(banco, corpus)
    alvo = tmp_path / "ensaio"
    codigo = cli.main(
        ["ingest", "plataforma", "--annotate-db", str(banco), "--data-dir", str(alvo)]
    )
    saida = capsys.readouterr().out
    assert codigo == 0
    assert "ENSAIO" in saida
    assert (alvo / "raw" / "plataforma.parquet").is_file()
    assert status_de(banco)[criacao_id] == "aprovada"


def test_a_cli_carimba_no_passe_canonico(
    banco: Path, corpus: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sem ``--data-dir`` e sem ``--max-rows``, o carimbo acontece.

    ``paths.RAW`` é redirecionado pelo teste porque este é o caminho que escreve
    na árvore canônica — e a árvore canônica de uma máquina de verdade não é
    lugar de teste.
    """
    (criacao_id,) = criar_e_aprovar(banco, corpus)
    raw = tmp_path / "canonico" / "raw"
    monkeypatch.setattr("prompt_factory.paths.RAW", raw)
    assert cli.main(["ingest", "plataforma", "--annotate-db", str(banco)]) == 0
    assert (raw / "plataforma.parquet").is_file()
    assert status_de(banco)[criacao_id] == "exportada"


def test_banco_da_plataforma_ausente_sai_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    codigo = cli.main(
        ["ingest", "plataforma", "--annotate-db", str(tmp_path / "nao-existe.sqlite")]
    )
    assert codigo == 2
    assert "pf annotate" in capsys.readouterr().out


def test_sem_criacao_aprovada_sai_0_com_o_caminho(
    banco: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Funil vazio é o estado de um clone limpo, não uma falha."""
    codigo = cli.main(
        ["ingest", "plataforma", "--annotate-db", str(banco), "--data-dir", str(tmp_path / "d")]
    )
    assert codigo == 0
    assert "nenhuma criação aprovada" in capsys.readouterr().out


def test_annotate_db_fora_da_plataforma_e_recusado(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    codigo = cli.main(["ingest", "aya", "--annotate-db", str(tmp_path / "x.sqlite")])
    assert codigo == 2
    assert "--annotate-db só vale" in capsys.readouterr().err


def test_o_wildchat_RECUSA_data_dir_em_vez_de_honrar_pela_metade(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Part-files e checkpoint dele moram em ``data/raw/_parts`` com caminho
    próprio: honrar só o parquet final gravaria na árvore de produção e
    consolidaria num diretório temporário."""
    codigo = cli.main(["ingest", "wildchat-pt", "--data-dir", str(tmp_path)])
    assert codigo == 2
    assert "--data-dir não vale para o WildChat" in capsys.readouterr().err
