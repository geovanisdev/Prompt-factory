"""wildchat — testes OFFLINE (nenhum byte de rede, nenhum `datasets`).

Cobre as quatro funções puras onde um erro custaria um passe de 1-3 h
(`hash_gate`, `first_user_text`, `family_of`, `allocate`), mais a mecânica de
disco que protege esse passe: checkpoint/parts, consolidação com dedup e
`validate_state`.

O `hash_gate` tem um valor CRAVADO para um `conversation_hash` real do dataset:
se alguém "arrumar" o fatiamento do hexdigest para fatiar o digest binário, o
pool inglês muda de tamanho silenciosamente e o teste é o único a gritar.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from prompt_factory import paths
from prompt_factory.ingest import wildchat as wc
from prompt_factory.ingest.base import RAW_COLUMNS, make_row

# ---------------------------------------------------------------------------
# hash_gate
# ---------------------------------------------------------------------------

#: conversation_hash real do WildChat-4.8M (32 hex).
HASH_REAL = "34f1581760df304d539e2fe4653b40d3"


def test_hash_gate_fatia_o_hexdigest_e_nao_o_digest() -> None:
    # A conta explícita, para o teste falhar por motivo legível se alguém trocar
    # `.hexdigest()[:8]` por `.digest()[:8]` (universo 2**64 -> corte em ~0).
    esperado = int(hashlib.sha256(HASH_REAL.encode("utf-8")).hexdigest()[:8], 16)
    assert esperado < 2**32
    assert wc.hash_gate(HASH_REAL, 0.11) is (esperado < int(0.11 * 2**32))


def test_hash_gate_valor_cravado_do_hash_real() -> None:
    # hexdigest[:8] = 0x2fcff010 = 802.156.560. Cortes: 0,11*2**32 = 472.446.402
    # (fora do pool) e 0,20*2**32 = 858.993.459 (dentro).
    assert int(hashlib.sha256(HASH_REAL.encode("utf-8")).hexdigest()[:8], 16) == 0x2FCFF010
    assert wc.hash_gate(HASH_REAL, 0.11) is False
    # Com 0,20 o mesmo hash entra: o corte é monotônico na fração.
    assert wc.hash_gate(HASH_REAL, 0.20) is True


def test_hash_gate_e_deterministico() -> None:
    assert all(wc.hash_gate(HASH_REAL, 0.11) == wc.hash_gate(HASH_REAL, 0.11) for _ in range(5))


@pytest.mark.parametrize("h", ["", "a", HASH_REAL, "ç" * 40])
def test_hash_gate_fronteiras(h: str) -> None:
    assert wc.hash_gate(h, 0.0) is False
    assert wc.hash_gate(h, 1.0) is True


def test_hash_gate_taxa_empirica_bate_com_a_fracao() -> None:
    hashes = [hashlib.md5(str(i).encode(), usedforsecurity=False).hexdigest() for i in range(20_000)]
    for fracao in (0.11, 0.5):
        taxa = sum(wc.hash_gate(h, fracao) for h in hashes) / len(hashes)
        assert abs(taxa - fracao) < 0.01, f"fracao={fracao} taxa={taxa}"


def test_hash_gate_independe_da_ordem() -> None:
    hashes = [hashlib.md5(str(i).encode(), usedforsecurity=False).hexdigest() for i in range(500)]
    direto = {h for h in hashes if wc.hash_gate(h)}
    reverso = {h for h in reversed(hashes) if wc.hash_gate(h)}
    assert direto == reverso


# ---------------------------------------------------------------------------
# first_user_text
# ---------------------------------------------------------------------------


def test_first_user_text_pega_o_primeiro_user() -> None:
    conversa = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    assert wc.first_user_text(conversa) == "u1"


def test_first_user_text_pula_assistant_ate_achar_user() -> None:
    conversa = [{"role": "assistant", "content": "oi"}, {"role": "user", "content": "pergunta"}]
    assert wc.first_user_text(conversa) == "pergunta"


def test_first_user_text_struct_completo_do_4_8m() -> None:
    # As 18 chaves reais do struct de `conversation`: só role/content importam.
    msg = {
        "content": "  Traduza isto para o japonês: 🌸  ",
        "created": 1712000000,
        "header": None,
        "hashed_ip": "abc",
        "country": "Brazil",
        "toxic": False,
        "redacted": False,
        "state": "São Paulo",
        "language": "Portuguese",
        "openai_id": "chatcmpl-1",
        "role": "user",
        "temperature": 1.0,
        "timestamp": None,
        "token_counter": 12,
        "top_p": 1.0,
        "turn_identifier": 1,
        "system_fingerprint": None,
        "usage": None,
    }
    # Texto devolvido EXATO: sem strip, sem normalizar (raw é raw; quem normaliza é o s01).
    assert wc.first_user_text([msg]) == "  Traduza isto para o japonês: 🌸  "


def test_first_user_text_mensagem_sem_role_e_ignorada() -> None:
    assert wc.first_user_text([{"content": "sem role"}, {"role": "user", "content": "com"}]) == "com"


def test_first_user_text_ignora_item_nao_dict_no_meio() -> None:
    assert wc.first_user_text(["lixo", None, {"role": "user", "content": "ok"}]) == "ok"


@pytest.mark.parametrize("content", [None, "", "   \n ", 42, ["a"], {"text": "a"}])
def test_first_user_text_conteudo_vazio_ou_exotico_descarta(content: object) -> None:
    # None e não-"" viram None: a conversa inteira é descartada, e a diferença
    # entre "não achei user" e "achei vazio" é o que alimenta dropped_empty_user.
    assert wc.first_user_text([{"role": "user", "content": content}]) is None


@pytest.mark.parametrize("conversa", [None, [], "string", 42, [{"role": "assistant", "content": "a"}]])
def test_first_user_text_conversa_invalida(conversa: object) -> None:
    assert wc.first_user_text(conversa) is None


def test_first_user_text_aceita_tupla() -> None:
    assert wc.first_user_text(({"role": "user", "content": "t"},)) == "t"


# ---------------------------------------------------------------------------
# family_of / len_bucket / year_of
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("modelo", "familia"),
    [
        ("gpt-4-0125-preview", "gpt-4"),
        ("gpt-4-1106-preview", "gpt-4"),
        ("gpt-4-turbo-2024-04-09", "gpt-4-turbo"),
        ("gpt-4o-2024-08-06", "gpt-4o"),
        ("gpt-4o-mini-2024-07-18", "gpt-4o"),
        ("gpt-4.1-mini-2025-04-14", "gpt-4.1-mini"),
        ("gpt-4.1-2025-04-14", "gpt-4.1"),
        ("gpt-3.5-turbo-0613", "gpt-3.5-turbo"),
        ("o1-mini-2024-09-12", "o1-mini"),
        ("o1-preview", "o1-preview"),
        ("modelo-que-ainda-nao-existe", "modelo-que-ainda-nao-existe"),
        ("", ""),
    ],
)
def test_family_of(modelo: str, familia: str) -> None:
    # O que este teste protege é a ORDEM dos prefixos: com gpt-4 antes de
    # gpt-4-turbo, ou gpt-4.1 antes de gpt-4.1-mini, os estratos colapsam.
    assert wc.family_of(modelo) == familia


@pytest.mark.parametrize(
    ("n", "bucket"),
    [(0, "1_lt120"), (119, "1_lt120"), (120, "2_120a600"), (600, "2_120a600"), (601, "3_gt600")],
)
def test_len_bucket_fronteiras(n: int, bucket: str) -> None:
    assert wc.len_bucket("x" * n) == bucket


@pytest.mark.parametrize(
    ("ts", "ano"),
    [("2023-04-09T12:00:00+00:00", "2023"), ("2025-07-31", "2025"), ("", "????"), ("lixo", "????")],
)
def test_year_of(ts: str, ano: str) -> None:
    assert wc.year_of(ts) == ano


def test_strata_key_junta_os_tres_eixos() -> None:
    assert wc.strata_key("gpt-4o-2024-08-06", "x" * 300, "2024-01-01") == "gpt-4o|2_120a600|2024"


# ---------------------------------------------------------------------------
# allocate
# ---------------------------------------------------------------------------

CONTAGENS = {"a": 3, "b": 40, "c": 100, "d": 500, "e": 2000, "f": 7000}


def test_allocate_soma_exata_e_respeita_pisos_e_tetos() -> None:
    alloc = wc.allocate(CONTAGENS, target=1000, floor_cap=50)
    assert sum(alloc.values()) == 1000
    for k, n in CONTAGENS.items():
        assert alloc[k] <= n, f"estrato {k} estourou"
        assert alloc[k] >= min(n, 50), f"estrato {k} abaixo do piso"
    # Estratos menores que o piso entram INTEIROS.
    assert alloc["a"] == 3 and alloc["b"] == 40


def test_allocate_e_deterministico_e_independe_da_ordem_de_entrada() -> None:
    a = wc.allocate(CONTAGENS, 1000, 50)
    b = wc.allocate(dict(reversed(list(CONTAGENS.items()))), 1000, 50)
    c = wc.allocate(CONTAGENS, 1000, 50)
    assert a == b == c


def test_allocate_recusa_pool_menor_que_o_alvo() -> None:
    with pytest.raises(ValueError, match="pool insuficiente"):
        wc.allocate({"a": 10, "b": 20}, target=100)


def test_allocate_com_alvo_igual_ao_pool_leva_tudo() -> None:
    total = sum(CONTAGENS.values())
    assert wc.allocate(CONTAGENS, total, 50) == CONTAGENS


def test_allocate_quando_nem_os_pisos_cabem() -> None:
    # 40 estratos de 500 com piso 500 e alvo 1000: reparte os próprios pisos.
    counts = {f"s{i:02d}": 500 for i in range(40)}
    alloc = wc.allocate(counts, target=1000, floor_cap=500)
    assert sum(alloc.values()) == 1000
    assert max(alloc.values()) <= 500


def test_allocate_redistribui_quando_um_estrato_estoura_o_teto() -> None:
    # 'peq' satura no piso e o excedente tem de sobrar para 'gde'.
    alloc = wc.allocate({"peq": 60, "gde": 100_000}, target=10_000, floor_cap=500)
    assert alloc == {"peq": 60, "gde": 9_940}


def test_allocate_alvo_zero() -> None:
    assert wc.allocate(CONTAGENS, target=0) == dict.fromkeys(CONTAGENS, 0)


def test_allocate_no_alvo_real_do_projeto() -> None:
    # Forma plausível do pool: 63 estratos muito desiguais.
    counts = {f"f{i}|b{j}|20{2 + k}3": 10 ** (1 + (i + j + k) % 4) for i in range(7) for j in range(3) for k in range(3)}
    alloc = wc.allocate(counts, wc.DOWNSAMPLE_TARGET, wc.DOWNSAMPLE_FLOOR)
    assert sum(alloc.values()) == wc.DOWNSAMPLE_TARGET
    assert all(alloc[k] <= counts[k] for k in counts)


# ---------------------------------------------------------------------------
# validate_state
# ---------------------------------------------------------------------------


def _estado_valido() -> dict[str, Any]:
    return {
        "version": wc.STATE_VERSION,
        "source": "wildchat_en",
        "hf_id": "allenai/WildChat-4.8M",
        "split": "train",
        "language": "English",
        "fraction": wc.EN_POOL_FRACTION,
        "max_rows": None,
        "columns": list(wc.COLUMNS),
    }


_KWARGS: dict[str, Any] = {
    "source": "wildchat_en",
    "hf_id": "allenai/WildChat-4.8M",
    "language": "English",
    "fraction": wc.EN_POOL_FRACTION,
    "max_rows": None,
}


def test_validate_state_aceita_o_mesmo_config() -> None:
    assert wc.validate_state(_estado_valido(), **_KWARGS) is None


@pytest.mark.parametrize(
    ("chave", "valor"),
    [
        ("fraction", 0.08),
        ("columns", ["conversation_hash", "language"]),
        ("hf_id", "allenai/WildChat-1M"),
        ("language", "Portuguese"),
        ("source", "wildchat_pt"),
        ("split", "test"),
        ("max_rows", 500),
        ("version", 99),
    ],
)
def test_validate_state_recusa_mismatch(chave: str, valor: Any) -> None:
    estado = _estado_valido()
    estado[chave] = valor
    motivo = wc.validate_state(estado, **_KWARGS)
    assert motivo is not None and motivo.startswith(chave)


def test_validate_state_recusa_state_vazio() -> None:
    assert wc.validate_state({}, **_KWARGS) is not None


# ---------------------------------------------------------------------------
# checkpoint / parts / consolidação
# ---------------------------------------------------------------------------


@pytest.fixture
def raw_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    destino = tmp_path / "raw"
    destino.mkdir()
    monkeypatch.setattr(paths, "RAW", destino)
    return destino


def _linha(sid: str, texto: str = "prompt") -> dict[str, Any]:
    return make_row(
        source="wildchat_en",
        source_id=sid,
        source_split="train",
        text_raw=texto,
        lang_source="English",
        created_ts="2024-01-01T00:00:00+00:00",
        country="Brazil",
        model_family="gpt-4o-2024-08-06",
        meta={"redacted": False, "state": "", "turn": 1},
    )


def test_write_part_grava_as_12_colunas_e_nao_deixa_tmp(raw_dir: Path) -> None:
    import pyarrow.parquet as pq

    caminho = wc.write_part("wildchat_en", 7, [_linha("a"), _linha("b")])
    assert caminho.name == "part_007.parquet"
    tabela = pq.read_table(caminho)
    assert tuple(tabela.schema.names) == RAW_COLUMNS
    assert tabela.num_rows == 2
    assert list(caminho.parent.glob("*.tmp")) == []


def test_state_roundtrip_em_json(raw_dir: Path) -> None:
    estado = {"scanned": 100, "hf_state": {"shard_idx": 2, "shard_example_idx": 51}}
    wc.save_state("wildchat_pt", estado)
    assert wc.load_state("wildchat_pt") == estado
    # JSON de verdade, nunca pickle: dá para ler com os olhos durante o passe.
    assert json.loads(wc._state_path("wildchat_pt").read_text(encoding="utf-8")) == estado


def test_load_state_ausente_ou_corrompido(raw_dir: Path) -> None:
    assert wc.load_state("wildchat_pt") is None
    caminho = wc._state_path("wildchat_pt")
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text("{isto não é json", encoding="utf-8")
    assert wc.load_state("wildchat_pt") is None


def test_consolidate_deduplica_ordena_e_carimba_a_licenca(raw_dir: Path) -> None:
    import pyarrow.parquet as pq

    # part_001 REESCRITO por uma retomada: sobrepõe o fim do part_000.
    wc.write_part("wildchat_en", 0, [_linha("ccc"), _linha("aaa")])
    wc.write_part("wildchat_en", 1, [_linha("aaa"), _linha("bbb")])
    cfg = {"license": "odc-by-1.0", "hf_id": "allenai/WildChat-4.8M", "attribution": "Zhao et al."}

    destino, linhas, duplicatas = wc.consolidate("wildchat_en", "wildchat_en_pool", 2, cfg=cfg)
    assert destino.name == "wildchat_en_pool.parquet"
    assert (linhas, duplicatas) == (3, 1)
    tabela = pq.read_table(destino)
    assert tabela.column("source_id").to_pylist() == ["aaa", "bbb", "ccc"]  # ordem total
    assert set(tabela.column("license").to_pylist()) == {"odc-by-1.0"}
    assert tuple(tabela.schema.names) == RAW_COLUMNS
    assert list(raw_dir.glob("*.tmp")) == []


def test_consolidate_e_deterministico_byte_a_byte(raw_dir: Path) -> None:
    wc.write_part("wildchat_en", 0, [_linha("bbb"), _linha("aaa")])
    cfg = {"license": "odc-by-1.0", "hf_id": "x", "attribution": "y"}
    primeiro = wc.consolidate("wildchat_en", "wildchat_en_pool", 1, cfg=cfg)[0].read_bytes()
    segundo = wc.consolidate("wildchat_en", "wildchat_en_pool", 1, cfg=cfg)[0].read_bytes()
    # Sem pf_ingested_at no metadata: re-consolidar dá o MESMO arquivo.
    assert primeiro == segundo


def test_consolidate_acusa_part_faltando(raw_dir: Path) -> None:
    wc.write_part("wildchat_en", 0, [_linha("a")])
    with pytest.raises(SystemExit, match="part faltando"):
        wc.consolidate("wildchat_en", "wildchat_en_pool", 3, cfg={"license": "odc-by-1.0"})


def test_consolidate_sem_parts_gera_parquet_vazio(raw_dir: Path) -> None:
    destino, linhas, duplicatas = wc.consolidate(
        "wildchat_pt", "wildchat_pt", 0, cfg={"license": "odc-by-1.0"}
    )
    assert (linhas, duplicatas) == (0, 0) and destino.is_file()


def test_limpar_apaga_state_e_parts_mas_nao_o_final(raw_dir: Path) -> None:
    wc.write_part("wildchat_pt", 0, [_linha("a")])
    wc.save_state("wildchat_pt", {"scanned": 1})
    final = raw_dir / "wildchat_pt.parquet"
    final.write_bytes(b"parquet-de-mentira")

    wc._limpar("wildchat_pt")
    assert not wc._state_path("wildchat_pt").exists()
    assert list(wc._parts_dir("wildchat_pt").glob("part_*.parquet")) == []
    assert final.read_bytes() == b"parquet-de-mentira"


# ---------------------------------------------------------------------------
# montagem da linha
# ---------------------------------------------------------------------------


def test_row_mapeia_o_registro_do_hub(raw_dir: Path) -> None:
    from datetime import UTC, datetime

    rec = {
        "conversation_hash": HASH_REAL,
        "model": "gpt-4o-2024-08-06",
        "timestamp": datetime(2024, 5, 1, 12, 0, tzinfo=UTC),
        "turn": 3,
        "language": "Portuguese",
        "toxic": False,
        "redacted": True,
        "state": "São Paulo",
        "country": None,
    }
    linha = wc._row(wc.MODOS["wildchat_pt"], rec, "Traduza isto")
    assert tuple(linha) == RAW_COLUMNS
    assert linha["source"] == "wildchat_pt"
    assert linha["source_id"] == HASH_REAL
    assert linha["lang_source"] == "Portuguese"
    assert linha["created_ts"] == "2024-05-01T12:00:00+00:00"
    assert linha["country"] == ""  # None nunca vira nulo
    assert linha["model_family"] == "gpt-4o-2024-08-06"  # string CRUA
    assert linha["nsfw_hint"] is False  # toxic é sempre False neste release
    # Chaves alfabéticas: o meta_json sai byte-idêntico entre execuções.
    assert linha["meta_json"] == '{"redacted":true,"state":"São Paulo","turn":3}'
    assert json.dumps(json.loads(linha["meta_json"]), sort_keys=True) == json.dumps(
        json.loads(linha["meta_json"])
    )


# ---------------------------------------------------------------------------
# downsample (offline, sobre um pool sintético)
# ---------------------------------------------------------------------------


@pytest.fixture
def pool_sintetico(raw_dir: Path) -> Path:
    """Pool pequeno mas com a mesma forma do real: 3 famílias x 3 tamanhos x 3 anos."""
    modelos = ["gpt-4o-2024-08-06", "gpt-3.5-turbo-0613", "o1-mini-2024-09-12"]
    tamanhos = [50, 300, 900]
    anos = ["2023", "2024", "2025"]
    linhas = []
    for i in range(2_700):
        m = modelos[i % 3]
        t = tamanhos[(i // 3) % 3]
        a = anos[(i // 9) % 3]
        linhas.append(
            make_row(
                source="wildchat_en",
                source_id=f"{i:08x}" * 4,
                source_split="train",
                text_raw="x" * t,
                lang_source="English",
                created_ts=f"{a}-06-01T00:00:00+00:00",
                model_family=m,
                meta={"redacted": False, "state": "", "turn": 1},
            )
        )
    wc.write_part("wildchat_en", 0, linhas)
    return wc.consolidate(
        "wildchat_en", "wildchat_en_pool", 1, cfg={"license": "odc-by-1.0"}
    )[0]


def test_downsample_e_exato_e_byte_identico_entre_re_runs(
    pool_sintetico: Path, raw_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pyarrow.parquet as pq

    monkeypatch.setattr(wc, "DOWNSAMPLE_TARGET", 1_000)
    monkeypatch.setattr(wc, "DOWNSAMPLE_FLOOR", 50)
    cfg = {"license": "odc-by-1.0", "hf_id": "allenai/WildChat-4.8M", "attribution": "Zhao et al."}

    assert wc.downsample("wildchat_en", cfg) == 0
    destino = raw_dir / "wildchat_en.parquet"
    primeiro = destino.read_bytes()
    tabela = pq.read_table(destino)
    assert tabela.num_rows == 1_000
    ids = tabela.column("source_id").to_pylist()
    assert ids == sorted(ids) and len(set(ids)) == 1_000  # ordenado e sem repetir

    assert wc.downsample("wildchat_en", cfg) == 0
    assert destino.read_bytes() == primeiro  # determinismo byte a byte

    relatorio = (raw_dir / "wildchat_en.strata.txt").read_text(encoding="utf-8")
    assert "gpt-4o|1_lt120|2023" in relatorio
    assert "TOTAL" in relatorio and "1000" in relatorio


def test_downsample_sem_pool_falha_com_instrucao(raw_dir: Path) -> None:
    with pytest.raises(SystemExit, match="pool ausente"):
        wc.downsample("wildchat_en", {"license": "odc-by-1.0"})


def test_downsample_seleciona_independente_da_ordem_fisica_do_pool(
    pool_sintetico: Path, raw_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    monkeypatch.setattr(wc, "DOWNSAMPLE_TARGET", 1_000)
    monkeypatch.setattr(wc, "DOWNSAMPLE_FLOOR", 50)
    cfg = {"license": "odc-by-1.0"}
    wc.downsample("wildchat_en", cfg)
    esperado = pq.read_table(raw_dir / "wildchat_en.parquet").column("source_id").to_pylist()

    # Reembaralha o POOL fisicamente e refaz: a seleção tem de ser a mesma,
    # porque o downsample ordena por source_id antes de montar os estratos.
    tabela = pq.read_table(pool_sintetico)
    invertido = tabela.take(pa.array(list(reversed(range(tabela.num_rows))), type=pa.int64()))
    pq.write_table(invertido, pool_sintetico, compression="zstd")
    wc.downsample("wildchat_en", cfg)
    assert pq.read_table(raw_dir / "wildchat_en.parquet").column("source_id").to_pylist() == esperado


# ---------------------------------------------------------------------------
# CLI: guardas que não precisam de rede
# ---------------------------------------------------------------------------


def test_run_ingest_recusa_downsample_fora_do_en(raw_dir: Path) -> None:
    import argparse

    args = argparse.Namespace(downsample=True, max_rows=None, restart=False)
    assert wc.run_ingest("wildchat_pt", {"enabled": True, "hf_id": "x"}, args) == 2


def test_run_ingest_recusa_downsample_com_max_rows(raw_dir: Path) -> None:
    import argparse

    args = argparse.Namespace(downsample=True, max_rows=500, restart=False)
    assert wc.run_ingest("wildchat_en", {"enabled": True, "hf_id": "x"}, args) == 2


def test_run_ingest_fonte_desabilitada_sai_2(raw_dir: Path) -> None:
    import argparse

    args = argparse.Namespace(downsample=False, max_rows=None, restart=False)
    assert wc.run_ingest("wildchat_pt", {"enabled": False, "hf_id": "x"}, args) == 2


def test_passe_recusa_checkpoint_incompativel(raw_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    estado = _estado_valido()
    estado["fraction"] = 0.99
    wc.save_state("wildchat_en", estado)
    args = argparse.Namespace(downsample=False, max_rows=None, restart=False)
    cfg = {"enabled": True, "hf_id": "allenai/WildChat-4.8M", "license": "odc-by-1.0"}
    # Sai 1 ANTES de tocar a rede: `datasets` nem chega a ser importado.
    assert wc.run_ingest("wildchat_en", cfg, args) == 1
    saida = capsys.readouterr().out
    assert "checkpoint incompatível" in saida and "--restart" in saida


def test_passe_completo_so_reconsolida(raw_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    wc.write_part("wildchat_pt", 0, [_linha("aaa"), _linha("bbb")])
    estado = _estado_valido() | {
        "source": "wildchat_pt",
        "language": "Portuguese",
        "fraction": 1.0,
        "scanned": 3_199_860,
        "kept": 2,
        "dropped_empty_user": 0,
        "next_part": 1,
        "elapsed_s": 42.0,
        "complete": True,
    }
    wc.save_state("wildchat_pt", estado)
    args = argparse.Namespace(downsample=False, max_rows=None, restart=False)
    cfg = {
        "enabled": True,
        "hf_id": "allenai/WildChat-4.8M",
        "license": "odc-by-1.0",
        "expected_min": 30_000,
        "expected_max": 45_000,
    }
    assert wc.run_ingest("wildchat_pt", cfg, args) == 0
    saida = capsys.readouterr().out
    assert "já completo" in saida
    assert "WARN" in saida  # 2 linhas está fora de [30000-45000]
    assert (raw_dir / "wildchat_pt.parquet").is_file()


# ---------------------------------------------------------------------------
# retomada automática depois de erro de rede (sem rede: `_varrer` é dublê)
# ---------------------------------------------------------------------------


def _args() -> Any:
    import argparse

    return argparse.Namespace(downsample=False, max_rows=None, restart=False)


_CFG_EN: dict[str, Any] = {
    "enabled": True,
    "hf_id": "allenai/WildChat-4.8M",
    "license": "odc-by-1.0",
}


@pytest.fixture
def sem_espera(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wc.time, "sleep", lambda _s: None)


def test_passe_reabre_o_stream_depois_de_erro_de_rede(
    raw_dir: Path, sem_espera: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    chamadas: list[int] = []

    def falha_duas_vezes(nome, modo, hf_id, estado, max_rows, *, quieto=False):
        chamadas.append(1)
        if len(chamadas) <= 2:
            raise ConnectionError("Connection broken: IncompleteRead")
        estado["scanned"] = 3_199_860
        estado["kept"] = 0
        return "ok"

    monkeypatch.setattr(wc, "_varrer", falha_duas_vezes)
    assert wc.run_ingest("wildchat_en", _CFG_EN, _args()) == 0
    assert len(chamadas) == 3
    saida = capsys.readouterr().out
    assert "rede caiu" in saida and "Tentativa 1/" in saida and "Tentativa 2/" in saida


def test_passe_desiste_quando_a_falha_nao_avanca_nenhum_checkpoint(
    raw_dir: Path, sem_espera: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    chamadas: list[int] = []

    def sempre_falha(nome, modo, hf_id, estado, max_rows, *, quieto=False):
        chamadas.append(1)
        raise ConnectionError("middlebox cortou em 4 MB")

    monkeypatch.setattr(wc, "MAX_TENTATIVAS_REDE", 2)
    monkeypatch.setattr(wc, "_varrer", sempre_falha)
    with pytest.raises(ConnectionError):
        wc.run_ingest("wildchat_en", _CFG_EN, _args())
    # 2 tentativas + a que estourou o limite: uma falha determinística não gira.
    assert len(chamadas) == 3
    assert "desistindo" in capsys.readouterr().out


def test_passe_zera_o_contador_quando_o_checkpoint_avanca(
    raw_dir: Path, sem_espera: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    chamadas: list[int] = []

    def falha_mas_avanca(nome, modo, hf_id, estado, max_rows, *, quieto=False):
        chamadas.append(1)
        estado["scanned"] += 50_000  # cada tentativa gravou um checkpoint novo
        if len(chamadas) <= 5:  # mais falhas que MAX_TENTATIVAS_REDE=2
            raise ConnectionError("rede ruim a noite inteira")
        return "ok"

    monkeypatch.setattr(wc, "MAX_TENTATIVAS_REDE", 2)
    monkeypatch.setattr(wc, "_varrer", falha_mas_avanca)
    assert wc.run_ingest("wildchat_en", _CFG_EN, _args()) == 0
    assert len(chamadas) == 6  # nunca desistiu, porque sempre houve progresso


def test_erros_de_rede_cobre_o_que_o_hub_levanta() -> None:
    erros = wc._erros_de_rede()
    import requests

    assert issubclass(requests.exceptions.ChunkedEncodingError, erros)
    assert issubclass(ConnectionError, erros) and issubclass(TimeoutError, erros)
    # ValueError NÃO é erro de rede: schema quebrado tem de estourar na hora.
    assert not issubclass(ValueError, erros)


# ---------------------------------------------------------------------------
# lmsys: opt-in, zero rede
# ---------------------------------------------------------------------------


def test_lmsys_desabilitada_sai_2_sem_tocar_a_rede(capsys: pytest.CaptureFixture[str]) -> None:
    import argparse

    from prompt_factory import config
    from prompt_factory.ingest import lmsys_chat_1m

    cfg = config.source("lmsys")
    assert cfg["enabled"] is False and cfg["redistributable"] is False
    assert lmsys_chat_1m.run_ingest("lmsys", cfg, argparse.Namespace()) == 2
    saida = capsys.readouterr().out
    assert "DESLIGADA" in saida and "hf auth login" in saida and "NAME_1" in saida


def test_lmsys_iter_rows_e_esqueleto() -> None:
    from prompt_factory.ingest import lmsys_chat_1m

    with pytest.raises(NotImplementedError, match="não implementado"):
        next(lmsys_chat_1m.iter_rows({}))
