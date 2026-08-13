"""Entrypoint `pf` — CLI única do Prompt Factory (argparse, só stdlib).

Cada subcomando corresponde a um estágio da pipeline e vai saindo do stub no
marco indicado em `_Cmd.milestone` (implementados: `db-check` no M1; `ingest` e
`report raw` no M2; `run` s01-s06 e `report universe`/`dedup-sample` no M4;
`make-seed`, `labels` e `merge-labels` no M5; `train` e `apply` no M7; `load-db`
e `db-check --bench` no M8; `serve` e `export` no M9).
Enquanto é stub, o comando imprime em que marco chega e sai com código 2.
`pf --help` e `pf <cmd> --help` funcionam e saem 0 — é isso que a DoD do M0
exige.

O `main()` faz o *bootstrap* de ambiente ANTES de qualquer import pesado:
UTF-8 forçado (Windows) e ``HF_HOME`` vindo do ``config/settings.toml``, para
que datasets/torch nunca escrevam cache no C:.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__

PROG = "pf"

# (flags, kwargs) — só o suficiente para `pf <cmd> --help` já documentar a forma
# final do comando. Os handlers reais chegam nos marcos indicados.
_Option = tuple[tuple[str, ...], dict[str, object]]

_MAX_ROWS: _Option = (
    ("--max-rows",),
    {
        "type": int,
        "metavar": "N",
        "help": "processa no máximo N linhas (smoke test; no wildchat conta linhas VARRIDAS, não mantidas)",
    },
)
_FORCE: _Option = (
    ("--force",),
    {"action": "store_true", "help": "reescreve saídas já existentes"},
)
_DATA_DIR: _Option = (
    ("--data-dir",),
    {
        "metavar": "DIR",
        "help": "redireciona a árvore de dados (padrão: data/) — é assim que o smoke roda sem tocar no corpus real",
    },
)
_LABELING_DIR: _Option = (
    ("--labeling-dir",),
    {
        "metavar": "DIR",
        "help": "redireciona a árvore da campanha (padrão: labeling/): semente, lotes, manifest e rótulos",
    },
)


class _Cmd:
    """Descrição declarativa de um subcomando.

    Ao implementar um comando (ex.: ``db-check`` no M1): escreva o handler,
    passe-o em ``handler=`` e marque ``implemented=True``. Só isso — o help, o
    dispatch e a parametrização dos testes seguem essa flag automaticamente.
    """

    __slots__ = ("handler", "help", "implemented", "milestone", "name", "options")

    def __init__(
        self,
        name: str,
        milestone: str,
        help: str,
        options: Sequence[_Option] = (),
        *,
        implemented: bool = False,
        handler: object | None = None,
    ) -> None:
        self.name = name
        self.milestone = milestone
        self.help = help
        self.options = tuple(options)
        self.implemented = implemented
        self.handler = handler


def _db_check(args: argparse.Namespace) -> int:
    """``pf db-check`` (M1) — autoteste do SQLite num banco temporário.

    Com ``--bench`` (M8), depois do autoteste ele mede o banco REAL em somente
    leitura: sem banco carregado, avisa e sai 0.

    Import lazy de propósito: ``pf --help`` não precisa carregar o DDL nem o
    módulo ``sqlite3``.
    """
    from .db import run_db_check

    return run_db_check(bench=args.bench, query=args.query, db_path=args.db)


def _ingest(args: argparse.Namespace) -> int:
    """``pf ingest [fonte...]`` (M2/M3) — baixa fontes e grava ``data/raw/<fonte>.parquet``.

    Sequencial e fail-fast: a primeira fonte que explodir aborta a rodada, com o
    traceback inteiro. As fontes já gravadas ficam válidas (cada parquet é
    escrito atomicamente), então basta re-rodar o mesmo comando — os downloads
    do HuggingFace retomam do cache e o WildChat retoma do checkpoint.

    Dois contratos de ingester (ver ``ingest/__init__.py``): ``run_ingest`` para
    quem controla a própria escrita (WildChat, streaming com checkpoint; e a
    ``plataforma``, que precisa carimbar o banco DEPOIS de o parquet existir) e
    ``iter_rows`` + ``write_raw`` para as fontes pequenas do M2.

    ``--data-dir`` redireciona o ``raw/`` e é o que permite o smoke da fonte
    ``plataforma`` rodar a cadeia inteira num diretório descartável (é a única
    fonte com insumo local, logo a única testável de ponta a ponta). O WildChat
    **recusa** a flag em vez de honrá-la pela metade: checkpoints e part-files
    dele moram em ``data/raw/_parts``.

    ``datasets``/``pyarrow`` entram só aqui dentro: ``pf --help`` não paga por eles.
    """
    from pathlib import Path

    from . import paths
    from .ingest import REGISTRY, resolve_names
    from .ingest.base import get_source_cfg, write_raw

    paths.ensure_dirs()
    try:
        fontes = resolve_names(args.source)
    except ValueError as exc:
        print(f"[pf] {exc}", file=sys.stderr)
        return 2

    if args.resume:
        # Retomar já é o padrão: a flag existe só para quem espera digitá-la.
        print("[pf] --resume é o comportamento PADRÃO (existindo checkpoint válido, o passe continua)")
    if args.force:
        print("[pf] --force não tem efeito: raw é regenerável, a ingestão sempre reescreve")
    if args.downsample and fontes != ["wildchat_en"]:
        print("[pf] --downsample só vale para `pf ingest wildchat-en`", file=sys.stderr)
        return 2
    if args.annotate_db and fontes != ["plataforma"]:
        print("[pf] --annotate-db só vale para `pf ingest plataforma`", file=sys.stderr)
        return 2

    raw_dir = Path(args.data_dir).resolve() / "raw" if args.data_dir else None
    if raw_dir is not None:
        print(f"[pf] --data-dir: o raw vai para {raw_dir}")

    print(f"[pf] ingerindo {len(fontes)} fonte(s): {', '.join(fontes)}")
    for nome in fontes:
        try:
            modulo = REGISTRY[nome]
            runner = getattr(modulo, "run_ingest", None)
            if runner is not None:
                codigo = int(runner(nome, get_source_cfg(nome), args))
                if codigo != 0:
                    return codigo
            else:
                write_raw(
                    nome,
                    modulo.iter_rows(get_source_cfg(nome), args.max_rows),
                    args.max_rows,
                    raw_dir=raw_dir,
                )
        except Exception as exc:  # o traceback inteiro vai para o stderr logo abaixo
            import traceback

            traceback.print_exc()
            print(f"[{nome}] FALHOU: {exc}", file=sys.stderr)
            return 1
    return 0


def expandir_estagios(
    tokens: Sequence[str], validos: Sequence[str], todos: Sequence[str] | None = None
) -> list[str]:
    """``["s01", "s03-s05"]`` → ``["s01", "s03", "s04", "s05"]``.

    Aceita ``all``, nomes soltos e intervalos ``sNN-sMM``. Exige ordem
    **crescente** no resultado: pedir ``s04 s02`` é quase sempre engano de quem
    digitou, e rodar fora de ordem produziria um universo montado com insumo
    velho, em silêncio.

    ``todos`` é o que ``all`` significa, quando isso não é ``validos`` inteiro:
    o s07 e o s08 são estágios legítimos de ``pf run s07``, mas ficam fora do
    ``all`` porque entre eles existe uma campanha de rotulagem com revisão
    humana (ver ``stages.CADEIA``).
    """
    ordem = list(validos)
    padrao = list(todos) if todos is not None else ordem
    escolhidos: list[str] = []
    for token in tokens:
        bruto = token.strip().lower()
        if bruto == "all":
            escolhidos.extend(padrao)
            continue
        if "-" in bruto:
            inicio, _, fim = bruto.partition("-")
            for ponta in (inicio, fim):
                if ponta not in ordem:
                    raise ValueError(f"estágio desconhecido: {ponta!r} (conhecidos: {', '.join(ordem)})")
            i, j = ordem.index(inicio), ordem.index(fim)
            if i > j:
                raise ValueError(f"intervalo invertido: {bruto!r}")
            escolhidos.extend(ordem[i : j + 1])
            continue
        if bruto not in ordem:
            raise ValueError(f"estágio desconhecido: {bruto!r} (conhecidos: {', '.join(ordem)})")
        escolhidos.append(bruto)

    vistos: list[str] = []
    for nome in escolhidos:
        if nome not in vistos:
            vistos.append(nome)
    posicoes = [ordem.index(n) for n in vistos]
    if posicoes != sorted(posicoes):
        raise ValueError(
            f"estágios fora de ordem: {' '.join(vistos)} — a pipeline é uma cadeia, rode em ordem crescente"
        )
    return vistos


def _run(args: argparse.Namespace) -> int:
    """``pf run [estágios]`` (M4) — roda s01..s06 em cadeia, parando no primeiro erro.

    O único import do módulo de estágios acontece aqui dentro: ``stages`` puxa
    pyarrow e, mais fundo, torch/lingua — ``pf --help`` não paga por isso.
    """
    from pathlib import Path

    from . import paths
    from .stages import CADEIA, DESCRICOES, STAGES, StageConfig

    try:
        nomes = expandir_estagios(args.stages or ["all"], list(STAGES), CADEIA)
    except ValueError as exc:
        print(f"[pf] {exc}", file=sys.stderr)
        return 2

    if args.force:
        print("[pf] --force não tem efeito: todo estágio reescreve a própria saída")
    cfg = StageConfig(
        data_dir=Path(args.data_dir).resolve() if args.data_dir else paths.DATA,
        max_rows=args.max_rows,
    )
    cfg.preparar_dirs()
    limite = f", max_rows={args.max_rows} (só o s01 corta)" if args.max_rows else ""
    print(f"[pf] data_dir={cfg.data_dir}{limite}")
    for nome in nomes:
        print(f"[pf] === {nome}: {DESCRICOES.get(nome, '')} ===")
        codigo = int(STAGES[nome](cfg))
        if codigo != 0:
            print(f"[pf] {nome} falhou (código {codigo}) — cadeia interrompida", file=sys.stderr)
            return codigo
    return 0


def _report(args: argparse.Namespace) -> int:
    """``pf report <alvo>`` — ``raw`` (M2), ``universe`` e ``dedup-sample`` (M4)."""
    from pathlib import Path

    raiz = Path(args.data_dir).resolve() if args.data_dir else None
    if args.target == "raw":
        from .report import report_raw

        return report_raw(sources=args.sources, head=args.head)
    if args.target == "universe":
        from .report import report_universe

        return report_universe((raiz / "final" / "universe.parquet") if raiz else None)
    if args.target == "dedup-sample":
        from .report import report_dedup_sample

        return report_dedup_sample(data_dir=raiz)
    if args.target is None:
        print("[pf] 'report' precisa de um alvo: raw | universe | dedup-sample")
        return 2
    print(f"[pf] alvo desconhecido: {args.target!r} (use raw | universe | dedup-sample)")
    return 2


def _stage_config(args: argparse.Namespace, **extra: object) -> object:
    """``StageConfig`` a partir das flags comuns (import lazy do módulo)."""
    from pathlib import Path

    from . import paths
    from .stages import StageConfig

    cfg = StageConfig(
        data_dir=Path(args.data_dir).resolve() if getattr(args, "data_dir", None) else paths.DATA,
        labeling_dir=(
            Path(args.labeling_dir).resolve()
            if getattr(args, "labeling_dir", None)
            else paths.LABELING
        ),
        **extra,  # type: ignore[arg-type]
    )
    cfg.preparar_dirs()
    return cfg


def _make_seed(args: argparse.Namespace) -> int:
    """``pf make-seed`` (M5) — s07: amostra-semente + lotes + manifest."""
    from .stages import STAGES

    if args.total:
        print(f"[pf] --total={args.total} sobrescreve [seed] size só nesta execução")
    cfg = _stage_config(args, force=bool(args.force))
    return int(STAGES["s07"](cfg))  # type: ignore[arg-type]


def _merge_labels(args: argparse.Namespace) -> int:
    """``pf merge-labels`` (M5) — s08: funde rótulos de agente e nativos."""
    from .stages import STAGES

    cfg = _stage_config(args, strict=bool(args.strict))
    return int(STAGES["s08"](cfg))  # type: ignore[arg-type]


def _train(args: argparse.Namespace) -> int:
    """``pf train`` (M7) — s09: regressão logística sobre os embeddings, por eixo.

    O portão continua humano: o comando imprime o macro-F1 contra as metas do
    ``[classifier]`` e sai 0 mesmo abaixo delas — quem decide se o número basta
    (ou se a campanha rotula mais lotes) é quem lê o relatório.
    """
    from .stages import STAGES

    cfg = _stage_config(args, axis=args.axis, folds=int(args.folds))
    return int(STAGES["s09"](cfg))  # type: ignore[arg-type]


def _apply(args: argparse.Namespace) -> int:
    """``pf apply`` (M7) — s10: pontua o universo e grava ``final/labeled.parquet``.

    ``--force`` só destrava seed_labels mudado depois do treino; embeddings de
    outra build são fatais sempre (rótulo plausível da linha errada).
    """
    from .stages import STAGES

    cfg = _stage_config(args, max_rows=args.max_rows, force=bool(args.force))
    return int(STAGES["s10"](cfg))  # type: ignore[arg-type]


def _load_db(args: argparse.Namespace) -> int:
    """``pf load-db`` (M8) — s11: constrói o SQLite do zero e troca por swap.

    Sai **3** quando o banco novo está pronto mas a troca foi recusada (destino
    aberto por outro processo). É um código distinto de propósito: um script que
    veja 3 sabe que basta parar o servidor e rodar ``--swap-only``, sem
    reconstruir nada.
    """
    from .stages import STAGES

    if args.swap:
        print("[pf] --swap é o comportamento PADRÃO (use --no-swap para só construir)")
    cfg = _stage_config(
        args,
        allow_unlabeled_pct=args.allow_unlabeled_pct,
        swap=not args.no_swap,
        swap_only=bool(args.swap_only),
        backup=bool(args.backup),
        deep=bool(args.deep),
    )
    return int(STAGES["s11"](cfg))  # type: ignore[arg-type]


def _labels(args: argparse.Namespace) -> int:
    """``pf labels <ação>`` (M5) — opera o livro-caixa da campanha.

    Toda escrita de estado da campanha passa por aqui: é o único lugar com
    validação estrita e escrita atômica do manifest. Editar
    ``labeling/manifest.json`` à mão é como editar um journal de banco de dados
    à mão — funciona até a primeira vez que não funciona.

    ``labeling_io`` é stdlib puro e entra por import lazy: um ``pf labels
    status`` não carrega pyarrow nem torch.
    """
    from pathlib import Path

    from . import labeling_io as lio
    from .stages import imprimir_funil

    lp = lio.LabelingPaths(Path(args.labeling_dir).resolve() if args.labeling_dir else None)
    acao = args.action or "status"

    if acao == "status":
        p = lio.painel(lp)
        if args.json:
            import json

            print(json.dumps(p, ensure_ascii=False, indent=2))
            return 0
        c = p["contagem"]
        imprimir_funil(
            "labels",
            ("item", "valor"),
            [
                ["taxonomia", p["taxonomy_version"]],
                [
                    "calibração",
                    f"{p['calibracao_status']} ({p['calibracao_n']} itens)",
                ],
                ["lotes pendentes", c.get(lio.PENDING, 0)],
                ["lotes reivindicados", c.get(lio.CLAIMED, 0)],
                ["lotes concluídos", c.get(lio.DONE, 0)],
                ["lotes falhados", c.get(lio.FAILED, 0)],
                ["claims órfãos (voltam no próximo claim)", len(p["orfaos"])],
                ["agreement médio", lio.texto_agreement(p)],
                [
                    f"lotes abaixo de {p['agreement_min']:.2f}",
                    len(p["lotes_baixos"]),
                ],
                ["rótulos", f"{p['n_rotulos']} / {p['n_seed']}"],
            ],
        )
        if p["calibracao_status"] != lio.GOLD:
            print(
                "[labels] a calibração ainda não virou ouro: rotule "
                f"{lio.BATCH_CALIBRACAO}, revise à mão e rode `pf labels gold --file <jsonl>`"
            )
        return 0

    if acao == "list":
        manifest = lio.carregar_manifest(lp)
        filtro = args.status
        linhas = [
            [
                b,
                r.get("status"),
                r.get("n_items"),
                r.get("tentativas", 0),
                (f"{r['agreement']:.2f}" if isinstance(r.get("agreement"), int | float) else "-"),
                r.get("agent_model") or "-",
                (r.get("motivo") or "")[:40],
            ]
            for b, r in sorted(manifest.get("batches", {}).items())
            if filtro is None or r.get("status") == filtro
        ]
        if not linhas:
            print(f"[labels] nenhum lote{f' em {filtro!r}' if filtro else ''}")
            return 0
        imprimir_funil(
            "labels",
            ("lote", "status", "itens", "tent.", "agree", "modelo", "motivo"),
            linhas[: args.limit] if args.limit else linhas,
        )
        return 0

    if acao == "next":
        manifest = lio.carregar_manifest(lp)
        if manifest.get("calibration", {}).get("status") != lio.GOLD:
            print(
                f"[labels] AVISO: a calibração ainda é {manifest['calibration']['status']!r} — "
                "os lotes vão rodar sem portão de agreement até o ouro ser importado"
            )
        alvos = lio.claim(args.batch, n=args.count, lp=lp, manifest=manifest)
        lio.salvar_manifest(manifest, lp)
        if not alvos:
            restam = sum(
                1 for r in manifest.get("batches", {}).values() if r.get("status") == lio.PENDING
            )
            print("[labels] nada pendente" if not restam else f"[labels] {restam} pendentes")
            return 0
        for batch_id in alvos:
            dados = lio.carregar_lote(batch_id, lp)
            if args.out_dir:
                destino = Path(args.out_dir) / f"{batch_id}.json"
                lio.escrever_json_atomico(destino, dados)
                print(f"[labels] {batch_id} claimed -> {destino}")
            elif args.out:
                lio.escrever_json_atomico(Path(args.out), dados)
                print(f"[labels] {batch_id} claimed -> {args.out}")
            else:
                import json

                print(json.dumps(dados, ensure_ascii=False))
        return 0

    if acao == "submit":
        if not args.batch or not args.file:
            print("[pf] 'labels submit' exige --batch e --file", file=sys.stderr)
            return 2
        texto = Path(args.file).read_text(encoding="utf-8")
        ok, linhas, erros = lio.validar_resposta(args.batch, texto, lp)
        if not ok:
            for erro in erros:
                print(f"[labels] {erro}", file=sys.stderr)
            faltam = lio.uids_para_retry(args.batch, linhas, lp)
            # Linha parseável: é ela que o maestro passa para `--only` no retry.
            print(f"RETRY_UIDS: {','.join(faltam)}")
            print(
                f"[labels] {args.batch} SEGUE reivindicado — corrija e submeta de novo",
                file=sys.stderr,
            )
            return 1
        for aviso in erros:  # sem erros, o que sobrou são avisos
            print(f"[labels] aviso: {aviso}")
        manifest = lio.carregar_manifest(lp)
        valor = lio.concluir(args.batch, linhas, args.model, lp=lp, manifest=manifest)
        minimo = lio.agreement_minimo()
        if valor is not None and valor < minimo:
            lio.reenfileirar(
                args.batch,
                f"agreement {valor:.2f} < {minimo:.2f}",
                lp=lp,
                manifest=manifest,
            )
            lio.salvar_manifest(manifest, lp)
            print(
                f"[labels] {args.batch}: agreement {valor:.2f} < {minimo:.2f} — "
                "lote devolvido para `pending` (portão de calibração)",
                file=sys.stderr,
            )
            return 1
        lio.salvar_manifest(manifest, lp)
        medida = (
            f"{valor:.2f}"
            if valor is not None
            else f"não medido ({lio.motivo_sem_agreement(manifest, args.batch)})"
        )
        print(f"[labels] {args.batch}: {len(linhas)} rótulos, agreement {medida} -> done")
        return 0

    if acao == "gold":
        if not args.file:
            print("[pf] 'labels gold' exige --file com o gabarito revisado", file=sys.stderr)
            return 2
        ok, _, erros = lio.importar_ouro(Path(args.file).read_text(encoding="utf-8"), lp)
        for erro in erros:
            print(f"[labels] {erro}", file=sys.stderr if not ok else sys.stdout)
        if not ok:
            print("[labels] ouro RECUSADO — nada foi gravado", file=sys.stderr)
            return 1
        print("[labels] ouro importado; agreement dos lotes já concluídos recalculado")
        return 0

    if acao == "requeue":
        manifest = lio.carregar_manifest(lp)
        if args.failed:
            alvos = [
                b
                for b, r in sorted(manifest.get("batches", {}).items())
                if r.get("status") == lio.FAILED
            ]
        elif args.batch:
            alvos = [args.batch]
        else:
            print("[pf] 'labels requeue' exige --batch ou --failed", file=sys.stderr)
            return 2
        for batch_id in alvos:
            lio.reenfileirar(batch_id, args.reason or "requeue manual", lp=lp, manifest=manifest)
        lio.salvar_manifest(manifest, lp)
        print(f"[labels] {len(alvos)} lote(s) de volta em pending: {', '.join(alvos) or '-'}")
        return 0

    print(f"[pf] ação desconhecida: {acao!r}", file=sys.stderr)
    return 2


def _serve(args: argparse.Namespace) -> int:
    """``pf serve`` (M9) — sobe a interface local em ``[app] host``/``[app] port``.

    **Um worker, sempre.** Com N workers cada processo carregaria a própria
    cópia da matriz de embeddings do M10 (~293 MB) e o próprio modelo e5, para
    servir um usuário só numa máquina só. E, com o SQLite em WAL, escrita
    concorrente de vários processos só traria ``database is locked`` de brinde.

    O import do uvicorn/fastapi mora aqui dentro: ``pf --help`` não paga por ele.
    """
    from pathlib import Path

    from . import paths
    from .config import get

    banco = Path(args.db).resolve() if args.db else paths.DB_FILE
    if not banco.is_file():
        # Falhar aqui, e não no lifespan, é o que transforma um traceback em
        # instrução: o servidor nem sobe e a linha de comando do conserto está
        # na tela.
        print(f"[pf] banco não encontrado em {banco}", file=sys.stderr)
        print("[pf] a interface só LÊ o SQLite; quem o constrói é a pipeline:")
        print("[pf]   pf load-db")
        print("[pf]   pf load-db --allow-unlabeled-pct 100   # antes do M7, sem rótulos")
        return 1

    import uvicorn

    host = args.host or str(get("app", "host", default="127.0.0.1"))
    porta = int(args.port or get("app", "port", default=8765))
    print(f"[pf] banco: {banco}")
    print(f"[pf] interface em http://{host}:{porta}/  (contrato da API em /docs)")

    if args.reload:
        # O --reload do uvicorn precisa de um alvo importável por string; a app
        # é uma FÁBRICA de propósito (ver app/main.py), daí o factory=True.
        # Nesse modo o --db não chega à app: o processo filho é outro.
        if args.db:
            print("[pf] AVISO: --reload ignora --db (o processo recarregado usa o padrão)")
        uvicorn.run(
            "prompt_factory.app.main:criar_app",
            factory=True,
            host=host,
            port=porta,
            reload=True,
            workers=1,
        )
        return 0

    from .app.main import criar_app

    uvicorn.run(criar_app(banco), host=host, port=porta, workers=1)
    return 0


def _annotate(args: argparse.Namespace) -> int:
    """``pf annotate [serve|seed|status|migrate]`` — a plataforma "Bancada".

    Um comando com AÇÃO POSICIONAL, e não quatro subcomandos de topo, porque os
    quatro operam o mesmo par de bancos e compartilham ``--db``/``--corpus-db``:
    ``pf annotate-serve``, ``pf annotate-seed`` e ``pf annotate-status`` soltos
    na raiz esconderiam que a Bancada é UM produto ao lado da pipeline. É o
    mesmo formato de ``pf labels <ação>``.

    Os dois bancos, sempre nesta relação:

    * ``--db`` (``data/db/annotate.sqlite``) — desta app, leitura e escrita,
      **criado sozinho** se faltar;
    * ``--corpus-db`` (``data/db/prompts.sqlite``) — somente leitura, exigido,
      construído pela pipeline.

    O import do uvicorn/fastapi mora dentro do ``serve``: ``pf --help`` não paga
    por ele, e ``pf annotate status`` também não.
    """
    from pathlib import Path

    from . import db as dbmod
    from . import paths
    from .annotate import db as adb
    from .annotate import seed as seedmod
    from .config import get

    banco = Path(args.db).resolve() if args.db else paths.ANNOTATE_DB_FILE
    corpus = Path(args.corpus_db).resolve() if args.corpus_db else paths.DB_FILE
    acao = args.action or "serve"

    if acao == "migrate":
        # A ÚNICA saída para um banco de schema divergente. Ele guarda trabalho
        # humano e não se recria a partir da pipeline — a migração reconstrói ao
        # lado, confere as contagens e as FKs, e só então troca o arquivo,
        # deixando o antigo como `.v<versão de origem>.bak`.
        from .annotate import migracao

        if not banco.is_file():
            print(f"[annotate] {banco} ainda não existe — não há o que migrar")
            print(
                f"[annotate] um banco novo já nasce na versão "
                f"{adb.SCHEMA_VERSION_ANOTACAO}: rode `pf annotate seed`"
            )
            return 0
        try:
            rel = migracao.migrar(banco)
        except migracao.MigracaoImpossivel as exc:
            # Código 3, como a recusa do --force e a do swap do load-db: não é
            # erro de uso, é uma guarda que disparou e nada foi alterado.
            print(f"[annotate] migração recusada: {exc}", file=sys.stderr)
            return 3
        if rel.get("ja_estava"):
            print(f"[annotate] {banco} já está na versão {rel['versao']} — nada a fazer")
            return 0
        print(f"[annotate] {banco}: schema {rel['de']} -> {rel['para']}")
        print(f"[annotate] cópia do banco anterior em {rel['backup']}")
        print(f"[annotate] {rel['diretrizes']} diretriz(es) versionada(s)")
        for nome, pid in rel["projetos"].items():
            quantas = rel["alocacao"].get(nome, 0)
            print(f"[annotate] projeto {nome!r} (id {pid}): {quantas} tarefa(s)")
        for antes, quantas in (rel.get("status_backfill") or {}).items():
            print(
                f"[annotate] status {antes!r} -> "
                f"{migracao.MAPA_STATUS[antes]!r}: {quantas} anotação(ões)"
            )
        from .stages import imprimir_funil

        imprimir_funil(
            "annotate",
            ("tabela", "linhas"),
            [[t, n] for t, n in rel["copiadas"].items()],
        )
        print(
            f"[annotate] {rel['copiadas']['anotacoes']} anotação(ões) preservada(s) — "
            "nenhuma linha de trabalho humano foi apagada"
        )
        # Os avisos vêm DEPOIS do relatório, e não antes: eles falam do que ainda
        # falta fazer (publicar a diretriz de um tipo novo, por exemplo), e o que
        # falta se lê melhor no fim do que no meio de uma lista de contagens.
        for aviso in rel.get("avisos") or []:
            print(f"[annotate] AVISO: {aviso}")
        return 0

    if acao == "serve":
        if not corpus.is_file():
            # Falhar AQUI, e não no lifespan, é o que transforma um traceback em
            # instrução: o servidor nem sobe e a linha do conserto está na tela.
            print(f"[pf] banco de prompts não encontrado em {corpus}", file=sys.stderr)
            print("[pf] a Bancada só LÊ o corpus; quem o constrói é a pipeline:")
            print("[pf]   pf load-db")
            print("[pf]   pf load-db --allow-unlabeled-pct 100   # antes do M7, sem rótulos")
            return 1

        import uvicorn

        from .annotate.main import criar_app

        host = args.host or str(get("annotate", "host", default="127.0.0.1"))
        porta = int(args.port or get("annotate", "port", default=8766))
        print(f"[pf] corpus (só leitura): {corpus}")
        print(f"[pf] plataforma: {banco}")
        print(f"[pf] Bancada em http://{host}:{porta}/  (contrato da API em /docs)")
        # workers=1 pelo mesmo motivo do `pf serve`: com o SQLite em WAL, vários
        # processos escrevendo só trariam `database is locked` de brinde, para
        # servir um usuário numa máquina só.
        uvicorn.run(criar_app(banco, corpus), host=host, port=porta, workers=1)
        return 0

    if acao == "seed":
        # As TRÊS camadas: personas, pacote de demonstração e tarefas sobre o
        # corpus real. Sem corpus, as duas primeiras rodam e o comando AVISA —
        # é o estado de um clone limpo antes do `pf load-db`, não um erro.
        banco.parent.mkdir(parents=True, exist_ok=True)
        conn = dbmod.connect(banco)
        conn_corpus = dbmod.connect(corpus, readonly=True) if corpus.is_file() else None
        try:
            try:
                adb.init_db(conn)
                relatorio = seedmod.semear(conn, conn_corpus, force=bool(args.force))
            except adb.SchemaDivergente as exc:
                print(f"[pf] {banco}: {exc}", file=sys.stderr)
                return 3
            except RuntimeError as exc:
                # A recusa do --force sobre trabalho humano. Código 3 (e não 1)
                # pelo mesmo motivo do swap recusado do `load-db`: não é erro de
                # uso, é uma guarda que disparou, e o conserto é outro comando.
                print(f"[pf] {exc}", file=sys.stderr)
                return 3
        finally:
            conn.close()
            if conn_corpus is not None:
                conn_corpus.close()
                # Conexão read-only em WAL deixa um -shm órfão ao lado do corpus,
                # e é ele que o pré-voo do swap do `pf load-db` lê como "alguém
                # está com isto aberto". Nunca o -wal.
                try:
                    shm = corpus.with_name(corpus.name + "-shm")
                    if shm.is_file():
                        shm.unlink()
                except OSError:
                    pass

        if relatorio["apagados"]:
            apagados = ", ".join(f"{n} {t}" for t, n in relatorio["apagados"].items() if n)
            print(f"[annotate] --force apagou: {apagados or 'nada'}")
        total = len(seedmod.PERSONAS)
        novas = int(relatorio["personas"])
        print(f"[annotate] {novas} persona(s) criada(s), {total - novas} já existia(m)")
        for nome, papel in seedmod.PERSONAS:
            print(f"[annotate]   {nome} — {papel}")
        projetos = ", ".join(f"{nome!r} (id {pid})" for nome, pid in relatorio["projetos"].items())
        print(
            f"[annotate] projetos: {projetos}; "
            f"{relatorio['diretrizes']} diretriz(es) versionada(s) inserida(s)"
        )
        pacote = relatorio["pacote"]
        print(
            f"[annotate] pacote de demonstração: {pacote['prompts_demo']} prompt(s), "
            f"{pacote['rubricas']} rubrica(s), {pacote['respostas_modelo']} resposta(s), "
            f"{pacote['tarefas']} tarefa(s)"
        )
        if pacote.get("traduzidas"):
            print(
                f"[annotate] {pacote['traduzidas']} fixture(s) ganharam a tradução da "
                "estrutura (o texto canônico não mudou)"
            )
        pool = relatorio["pool"]
        # Genérico, e não dois nomes literais: com o P4d são quatro tipos que
        # um prompt cru sustenta, e uma lista escrita à mão aqui esconderia o
        # quinto no dia em que ele aparecesse.
        print(
            "[annotate] tarefas sobre o corpus: "
            + ", ".join(f"{n} {tipo}" for tipo, n in pool.items())
        )
        for aviso in relatorio["avisos"]:
            print(f"[annotate] AVISO: {aviso}")
        from .stages import imprimir_funil

        imprimir_funil(
            "annotate", ("tabela", "linhas"), [[t, n] for t, n in relatorio["contagens"].items()]
        )
        return 0

    if acao == "status":
        if not banco.is_file():
            print(f"[annotate] {banco} ainda não existe")
            print("[annotate] ele nasce sozinho na primeira `pf annotate serve`")
            print("[annotate] (ou rode `pf annotate seed` para criá-lo agora)")
            return 0
        conn = dbmod.connect(banco)
        try:
            try:
                adb.init_db(conn)
            except adb.SchemaDivergente as exc:
                # O status é justamente o comando que alguém roda para descobrir
                # POR QUE a app não sobe: ele precisa dizer o conserto, não
                # falhar com um traceback.
                print(f"[annotate] {banco}: {exc}", file=sys.stderr)
                return 3
            linhas = [[t, n] for t, n in adb.contagens(conn).items()]
            versao = adb.get_meta(conn, adb.CHAVE_VERSAO, "?")
            n_anotacoes = dict(linhas).get("anotacoes", 0)
            from .annotate import projetos as projmod

            projetos_do_banco = projmod.listar(conn)
        finally:
            conn.close()
        from .stages import imprimir_funil

        print(f"[annotate] {banco} — schema {versao}")
        imprimir_funil("annotate", ("tabela", "linhas"), linhas)
        if n_anotacoes:
            print(f"[annotate] {n_anotacoes} anotação(ões) — trabalho humano, não regenerável")
        for p in projetos_do_banco:
            print(
                f"[annotate] projeto {p['nome']!r} ({p['cliente']}): "
                f"{p['n_tarefas']} tarefa(s), {p['n_abertas']} aberta(s)"
            )
        from .annotate import solo as solomod

        print(
            "[annotate] autorrevisão: "
            + (
                "PERMITIDA — " + solomod.AVISO
                if solomod.ligado()
                else "bloqueada — " + solomod.AVISO_DESLIGADO
            )
        )
        if corpus.is_file():
            conn = dbmod.connect(corpus, readonly=True)
            try:
                from .annotate import pool as poolmod

                n = int(conn.execute("SELECT count(*) AS n FROM prompts").fetchone()["n"])
                p = poolmod.resolver(conn)
                politica = poolmod.politica_licenca()
            finally:
                conn.close()
            print(f"[annotate] corpus (só leitura): {corpus} — {n} prompts")
            print(f"[annotate] pool: {p.origem} — {p.n_pool} item(ns); {p.motivo}")
            print(f"[annotate] licença: {politica['descricao']}")
            if p.excluidas_licenca:
                print(
                    f"[annotate] a política de licença exclui {p.excluidas_licenca} "
                    "linha(s) que passariam nos demais filtros"
                )
            print(f"[annotate] NSFW: {poolmod.NOTA_NSFW}")
            # A conexão read-only deixa um -shm órfão ao lado do corpus, e é ele
            # que o pré-voo do swap do `pf load-db` lê como "alguém está com
            # isto aberto". Mesma limpeza do shutdown da app (nunca o -wal).
            try:
                shm = corpus.with_name(corpus.name + "-shm")
                if shm.is_file():
                    shm.unlink()
            except OSError:
                pass
        else:
            print(f"[annotate] corpus AUSENTE em {corpus} — `pf annotate serve` vai recusar")
        return 0

    if acao == "gerar":
        return _annotate_gerar(args, banco, corpus)

    if acao == "pedidos":
        return _annotate_pedidos(args, banco)

    if acao == "export":
        return _annotate_export(args, banco, corpus)

    print(
        f"[pf] ação desconhecida: {acao!r} "
        "(use serve | seed | status | migrate | gerar | pedidos | export)",
        file=sys.stderr,
    )
    return 2


def _annotate_pedidos(args: argparse.Namespace, banco: Path) -> int:
    """``pf annotate pedidos`` — a campanha de DESTILAÇÃO (Central de Briefs).

    Mesmo desenho do ``gerar`` e da rotulagem: o agente destilador é puro
    cômputo (recebe as janelas do material dentro do arquivo do lote, devolve
    JSON como texto) e **só este fio grava**, com validação estrita antes de
    encostar no banco.

    A DIFERENÇA que vale registrar: este comando **não abre o corpus**. O insumo
    são arquivos de texto fora do repositório e o destino é o ``annotate.sqlite``
    — é a única das três campanhas que roda sem ``prompts.sqlite`` existir.
    """
    from . import db as dbmod
    from .annotate import db as adb
    from .annotate import destilacao as dmod
    from .stages import imprimir_funil

    dp = dmod.DestilacaoPaths(
        Path(args.destilacao_dir).resolve() if args.destilacao_dir else None
    )
    sub = args.subacao or "status"

    if sub == "status":
        p = dmod.painel(dp)
        estado = None
        if banco.is_file():
            conn = dbmod.connect(banco)
            try:
                estado = dmod.funil(conn)
            finally:
                conn.close()
        if args.json:
            import json as _json

            print(_json.dumps({"campanha": p, "banco": estado}, ensure_ascii=False, indent=2))
            return 0

        linhas = [
            ["contrato", p["contrato"]],
            ["lotes", p["n_lotes"]],
            [
                "por estado",
                f"{p['por_estado'][dmod.DONE]} done · {p['por_estado'][dmod.CLAIMED]} claimed · "
                f"{p['por_estado'][dmod.PENDING]} pending · {p['por_estado'][dmod.FAILED]} failed",
            ],
            ["janelas preparadas", p["janelas"]],
            ["claims órfãos (TTL)", f"{len(p['orfaos'])} (ttl {p['ttl_horas']:g} h)"],
        ]
        imprimir_funil("pedidos", ("item", "valor"), linhas)

        if p["cursores"]:
            imprimir_funil(
                "pedidos",
                ("arquivo", "cursor (caractere)"),
                [[a, o] for a, o in p["cursores"].items()],
            )
        if p["lotes"]:
            imprimir_funil(
                "pedidos",
                ("lote", "arquivo", "status", "janelas", "tent.", "motivo"),
                [
                    [
                        lid,
                        (r["arquivo"] or "")[:28],
                        r["status"],
                        r["n_janelas"],
                        r["tentativas"],
                        (r["motivo"] or "")[:40],
                    ]
                    for lid, r in p["lotes"].items()
                ],
            )
        else:
            # Campanha nova: em vez de uma tabela vazia, o que se pode destilar.
            # É a única hora em que a lista de 35 arquivos é útil e não é ruído.
            try:
                material = dmod.listar_material()
            except ValueError as exc:
                print(f"[pedidos] {exc}", file=sys.stderr)
                return 1
            imprimir_funil(
                "pedidos",
                ("arquivo", "MB", "coleção", "disciplina"),
                [
                    [m["arquivo"][:44], f"{m['bytes'] / 1e6:.1f}", m["colecao"], m["disciplina"]]
                    for m in material
                ],
            )
            print(f"[pedidos] {len(material)} arquivo(s) em {dmod.material_dir()}")

        print(f"[pedidos] fora do escopo desta campanha: {', '.join(p['excluidos'])}")
        print(
            "[pedidos] o recorte é insumo de LEITURA — ele não entra no prompt, no "
            "corpus, no git nem em export nenhum"
        )
        if estado is not None:
            imprimir_funil(
                "pedidos",
                ("dimensão", "distribuição"),
                [
                    ["total", estado["total"]],
                    ["com aviso", estado["com_aviso"]],
                    *[
                        [dim.replace("por_", ""), ", ".join(f"{k or '?'} {v}" for k, v in val.items())]
                        for dim, val in estado.items()
                        if dim.startswith("por_") and val
                    ],
                ],
            )
            disponiveis = estado["por_status"].get("disponivel", 0)
            if disponiveis:
                print(
                    f"[pedidos] {disponiveis} disponível(is) — número alto aqui é sinal de "
                    "PARAR de destilar: o gargalo é a escrita humana, não a destilação"
                )
        return 0

    banco.parent.mkdir(parents=True, exist_ok=True)
    conn = dbmod.connect(banco)
    try:
        try:
            adb.init_db(conn)
        except adb.SchemaDivergente as exc:
            print(f"[pf] {banco}: {exc}", file=sys.stderr)
            print("[pf] o conserto é `pf annotate migrate` — este banco guarda trabalho humano")
            return 3

        if sub == "preparar":
            if args.lote:
                # Retomada: o lote já existe no disco. Regerá-lo pegaria janelas
                # diferentes (o cursor já andou) e a resposta que o agente
                # produziu deixaria de casar com o arquivo.
                try:
                    rel = dmod.reemitir(args.lote, dp)
                except ValueError as exc:
                    print(f"[pedidos] {exc}", file=sys.stderr)
                    return 1
                destino = _copiar_lote(rel["arquivo"], args.out_dir)
                print(f"[pedidos] {rel['lote_id']} reivindicado de novo -> {destino}")
                return 0
            if not args.arquivo:
                print(
                    "[pf] 'pedidos preparar' exige --arquivo <nome.txt> "
                    "(ou --lote para retomar um lote existente)",
                    file=sys.stderr,
                )
                print("[pf] `pf annotate pedidos status` lista o material disponível")
                return 2
            try:
                rel = dmod.preparar(arquivo=args.arquivo, n=int(args.count or 6), dp=dp)
            except ValueError as exc:
                print(f"[pedidos] {exc}", file=sys.stderr)
                return 1
            if rel["lote_id"] is None:
                print(f"[pedidos] nada a preparar: {rel['motivo']}")
                return 0
            destino = _copiar_lote(rel["arquivo"], args.out_dir)
            print(
                f"[pedidos] {rel['lote_id']}: {rel['n']} janela(s) de {rel['fonte']} -> {destino}"
            )
            print(
                f"[pedidos] {rel['colecao'] or '(coleção não identificada pelo nome)'} · "
                f"{rel['disciplina'] or '(disciplina não identificada pelo nome)'}"
            )
            print(
                f"[pedidos] cursor deste arquivo: {rel['cursor']} de {rel['total_chars']} "
                f"caracteres ({rel['cursor'] / max(1, rel['total_chars']):.1%})"
            )
            print(
                "[pedidos] o agente LÊ este arquivo e devolve JSON; a skill "
                "`destilar-pedidos` tem o protocolo inteiro"
            )
            print(
                f"[pedidos] importe com: pf annotate pedidos importar --lote {rel['lote_id']} "
                "--file <resposta.json>"
            )
            return 0

        if sub == "importar":
            if not args.lote or not args.file:
                print("[pf] 'pedidos importar' exige --lote e --file", file=sys.stderr)
                return 2
            texto = Path(args.file).read_text(encoding="utf-8")
            try:
                rel = dmod.importar(conn, args.lote, texto, dp=dp, modelo=args.model)
            except ValueError as exc:
                print(f"[pedidos] {exc}", file=sys.stderr)
                return 1
            for aviso in rel.get("avisos", []):
                print(f"[pedidos] aviso: {aviso}")
            if not rel["ok"]:
                for erro in rel["erros"]:
                    print(f"[pedidos] {erro}", file=sys.stderr)
                print(
                    f"[pedidos] {rel['lote_id']}: tentativa {rel['tentativas']} de "
                    f"{rel['max_tentativas']}, lote em {rel['status']!r}",
                    file=sys.stderr,
                )
                if rel["status"] == dmod.CLAIMED:
                    print(
                        "[pedidos] o lote SEGUE reivindicado — corrija o arquivo e "
                        "importe de novo",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "[pedidos] tentativas esgotadas — reviva com "
                        f"`pf annotate pedidos preparar --lote {rel['lote_id']}`",
                        file=sys.stderr,
                    )
                return 1
            gravado = ", ".join(f"{n} {t}" for t, n in rel["gravado"].items() if n)
            print(
                f"[pedidos] {rel['lote_id']}: {rel['n_pedidos']} pedido(s) validado(s); "
                f"gravado: {gravado or 'nada novo'}"
            )
            return 0

        print(
            f"[pf] subação desconhecida: {sub!r} (use preparar | importar | status)",
            file=sys.stderr,
        )
        return 2
    finally:
        conn.close()


def _annotate_export(args: argparse.Namespace, banco: Path, corpus: Path) -> int:
    """``pf annotate export`` (P5b) — os artefatos de entrega da Bancada.

    O corpus é **obrigatório** aqui, e não um extra: sem ele não há texto de
    prompt, não há licença e não há atribuição — e um export de anotação sem a
    proveniência da linha não cumpre a licença de nenhuma das fontes.

    ``--perfil all`` gera os cinco perfis coletivos de uma vez. A auditoria fica
    de fora dele de propósito: ela é de UM item e precisa de ``--anotacao``.
    """
    from . import db as dbmod
    from . import paths
    from .annotate import db as adb
    from .annotate import entrega

    if not banco.is_file():
        print(f"[annotate] a plataforma ainda não tem banco em {banco}", file=sys.stderr)
        print("[annotate] rode `pf annotate seed` (ou suba a app) antes de exportar")
        return 1
    if not corpus.is_file():
        print(f"[annotate] banco de prompts não encontrado em {corpus}", file=sys.stderr)
        print("[annotate] o export precisa dele para a licença e a atribuição por linha:")
        print("[annotate]   pf load-db --allow-unlabeled-pct 100")
        return 1

    destino = Path(args.out_dir).resolve() if args.out_dir else paths.ANNOTATE_EXPORTS
    escolhido = args.perfil or "all"
    if escolhido == "all":
        # A auditoria é de UM item: entra no `all` só quando alguém disse qual.
        perfis = [k for k in entrega.PERFIS if k != "audit"]
        if args.anotacao is not None:
            perfis.append("audit")
    else:
        perfis = [escolhido]

    conn = dbmod.connect(banco)
    conn_corpus = dbmod.connect(corpus, readonly=True)
    codigo = 0
    try:
        # O export LÊ colunas que só existem a partir de certas versões
        # (`gabarito_avaliacao_json`, `payload_corrigido_json`). Num banco de
        # schema antigo ele não daria erro: daria um artefato plausível com o
        # QC pela metade, que é o desfecho pior.
        versao = adb.versao_do_banco(conn)
        if versao is not None and versao != adb.SCHEMA_VERSION_ANOTACAO:
            print(f"[annotate] {adb.SchemaDivergente(versao)}", file=sys.stderr)
            return 3
        for chave in perfis:
            pf = entrega.perfil(chave)
            try:
                res = entrega.executar(
                    conn,
                    conn_corpus,
                    chave,
                    destino_dir=destino,
                    nome=args.name if escolhido != "all" else None,
                    incluir_sinteticas=bool(args.incluir_sinteticas),
                    incluir_pendentes=bool(args.incluir_pendentes),
                    tipos=tuple(args.tipo or ()),
                    projeto=args.projeto,
                    anotacao_id=args.anotacao,
                )
            except ValueError as exc:
                # Perfil que não pôde sair (auditoria sem item, item fora do
                # recorte). Não derruba os outros: um `--perfil all` que perde a
                # auditoria ainda entregou cinco artefatos, e dizer qual faltou é
                # mais útil que abortar tudo.
                print(f"[annotate] {chave}: {exc}", file=sys.stderr)
                codigo = 1
                continue
            unidade = "linha(s)" if pf.container == "jsonl" else "documento"
            print(f"[annotate] {chave:14} {res.row_count:>6} {unidade:11} {res.arquivo}")
            if aviso := res.manifest.get("WARNING"):
                print(f"[annotate] AVISO: {aviso}")
            for chave_nota in ("NOTE_PENDING", "NOTE_MISSING_PROMPTS"):
                if nota := res.manifest.get(chave_nota):
                    print(f"[annotate] nota: {nota}")
        print(f"[annotate] manifesto de cada artefato ao lado dele, em {destino}")
    finally:
        conn.close()
        conn_corpus.close()
        _limpar_shm(corpus)
    return codigo


def _limpar_shm(corpus: Path) -> None:
    """Apaga o ``-shm`` órfão que uma conexão read-only em WAL deixa para trás.

    É exatamente o sinal que o pré-voo do swap do ``pf load-db`` lê como "alguém
    está com isto aberto": deixá-lo faria a próxima recarga do corpus ser
    recusada por causa de um processo já morto. **Nunca o ``-wal``** — ele pode
    conter transações commitadas que ainda não foram para o arquivo principal.
    """
    try:
        shm = corpus.with_name(corpus.name + "-shm")
        if shm.is_file():
            shm.unlink()
    except OSError:
        pass


def _annotate_gerar(args: argparse.Namespace, banco: Path, corpus: Path) -> int:
    """``pf annotate gerar <preparar|importar|status>`` (P4c) — a campanha.

    O MESMO desenho de ``pf labels``, e pela mesma razão: o agente gerador é
    puro cômputo (lê um arquivo de lote, devolve JSON como texto) e **só este
    fio grava**, com validação estrita antes de encostar no banco. Se o agente
    escrevesse, duas sessões paralelas se sobrescreveriam e a validação estrita
    seria contornável — que é o mesmo que não existir.

    ``geracao`` importa ``payloads``, ``catalogo`` e ``tarefas``: um import lazy,
    para um ``pf --help`` não pagar por pydantic nem pelo módulo de busca.
    """
    from . import db as dbmod
    from .annotate import db as adb
    from .annotate import geracao as gmod
    from .stages import imprimir_funil

    gp = gmod.GeracaoPaths(Path(args.geracao_dir).resolve() if args.geracao_dir else None)
    sub = args.subacao or "status"

    if sub == "status":
        p = gmod.painel(gp)
        composicao = None
        cobertura = None
        if banco.is_file():
            conn = dbmod.connect(banco)
            try:
                composicao = gmod.composicao(conn)
                cobertura = gmod.cobertura_do_pool(conn)
            finally:
                conn.close()
        if args.json:
            import json as _json

            print(
                _json.dumps(
                    {"campanha": p, "composicao": composicao, "cobertura": cobertura},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        linhas = [["contrato", p["contrato"]], ["lotes", p["n_lotes"]]]
        for campanha in gmod.CAMPANHAS:
            c = p["por_campanha"][campanha]
            linhas.append(
                [
                    f"{campanha}: lotes",
                    f"{c[gmod.DONE]} done · {c[gmod.CLAIMED]} claimed · "
                    f"{c[gmod.PENDING]} pending · {c[gmod.FAILED]} failed",
                ]
            )
            linhas.append(
                [f"{campanha}: itens", f"{p['importados'][campanha]} / {p['itens'][campanha]}"]
            )
        linhas.append(["claims órfãos (TTL)", f"{len(p['orfaos'])} (ttl {p['ttl_horas']:g} h)"])
        imprimir_funil("gerar", ("item", "valor"), linhas)

        dist = ", ".join(f"{k} {v:.0%}" for k, v in sorted(p["distribuicao"].items()))
        print(f"[gerar] distribuição-alvo das sintéticas: {dist}")
        print(f"[gerar] sintética entra como: {p['status_inicial']}")
        if p["lotes"]:
            imprimir_funil(
                "gerar",
                ("lote", "campanha", "status", "itens", "tent.", "motivo"),
                [
                    [
                        lid,
                        r["campanha"],
                        r["status"],
                        r["n_items"],
                        r["tentativas"],
                        (r["motivo"] or "")[:44],
                    ]
                    for lid, r in p["lotes"].items()
                ],
            )
        if composicao is not None:
            print(
                f"[gerar] anotações: {composicao['humanas']} humana(s) + "
                f"{composicao['sinteticas']} sintética(s) de {composicao['anotacoes']}"
            )
            # A MARCA, dita com todas as letras. É ela que o export do P5b honra
            # e o painel do P5a lê — e é a única: NULL significa trabalho humano.
            print(f"[gerar] sinal de sintética: {composicao['sinal']}")
            print(
                "[gerar] anotação sintética fica FORA dos exports de dado por padrão "
                "(o projeto não entrega anotação de IA como humana)"
            )
            if composicao["sinteticas_por_alvo"]:
                print(
                    "[gerar] alvos escondidos: "
                    + ", ".join(f"{k} {v}" for k, v in composicao["sinteticas_por_alvo"].items())
                )
            for tabela, origens in composicao["material_por_origem"].items():
                if origens:
                    print(
                        f"[gerar] {tabela} por origem: "
                        + ", ".join(f"{k} {v}" for k, v in origens.items())
                    )
        if cobertura is not None:
            print(
                f"[gerar] pool ({cobertura['pool']} prompts): "
                f"{cobertura['avaliar_rubrica']} sustentam avaliar_rubrica, "
                f"{cobertura['comparar_ab']} sustentam comparar_ab "
                f"(+{cobertura['comparar_ab_demo']} do pacote de demonstração)"
            )
        return 0

    if not corpus.is_file():
        print(f"[pf] corpus não encontrado em {corpus}", file=sys.stderr)
        print("[pf] a campanha só LÊ o corpus; quem o constrói é a pipeline: pf load-db")
        return 1
    banco.parent.mkdir(parents=True, exist_ok=True)
    conn = dbmod.connect(banco)
    conn_corpus = dbmod.connect(corpus, readonly=True)
    try:
        try:
            adb.init_db(conn)
        except adb.SchemaDivergente as exc:
            print(f"[pf] {banco}: {exc}", file=sys.stderr)
            print("[pf] o conserto é `pf annotate migrate` — este banco guarda trabalho humano")
            return 3

        if sub == "preparar":
            if args.lote:
                # Retomada: o lote já existe no disco e regerá-lo escolheria
                # prompts diferentes, fazendo a resposta que o agente já
                # produziu deixar de casar com o arquivo.
                try:
                    rel = gmod.reemitir(args.lote, gp)
                except ValueError as exc:
                    print(f"[gerar] {exc}", file=sys.stderr)
                    return 1
                destino = _copiar_lote(rel["arquivo"], args.out_dir)
                print(f"[gerar] {rel['lote_id']} reivindicado de novo -> {destino}")
                return 0
            if not args.campanha:
                print(
                    "[pf] 'gerar preparar' exige --campanha material|anotacoes "
                    "(ou --lote para retomar um lote existente)",
                    file=sys.stderr,
                )
                return 2
            try:
                rel = gmod.preparar(
                    conn,
                    conn_corpus,
                    campanha=args.campanha,
                    n=int(args.count or 8),
                    lang=args.lang or "ambos",
                    gp=gp,
                )
            except ValueError as exc:
                print(f"[gerar] {exc}", file=sys.stderr)
                return 1
            if rel["lote_id"] is None:
                print(f"[gerar] nada a preparar: {rel['motivo']}")
                return 0
            destino = _copiar_lote(rel["arquivo"], args.out_dir)
            print(
                f"[gerar] {rel['lote_id']} ({rel['campanha']}): {rel['n']} item(ns) -> {destino}"
            )
            print(
                "[gerar] o agente LÊ este arquivo e devolve JSON; a skill `gerar-material` "
                "tem o protocolo inteiro"
            )
            print(
                f"[gerar] importe com: pf annotate gerar importar --lote {rel['lote_id']} "
                "--file <resposta.json>"
            )
            return 0

        if sub == "importar":
            if not args.lote or not args.file:
                print("[pf] 'gerar importar' exige --lote e --file", file=sys.stderr)
                return 2
            texto = Path(args.file).read_text(encoding="utf-8")
            try:
                rel = gmod.importar(
                    conn, args.lote, texto, gp=gp, status=args.status, modelo=args.model
                )
            except ValueError as exc:
                print(f"[gerar] {exc}", file=sys.stderr)
                return 1
            for aviso in rel.get("avisos", []):
                print(f"[gerar] aviso: {aviso}")
            if not rel["ok"]:
                for erro in rel["erros"]:
                    print(f"[gerar] {erro}", file=sys.stderr)
                print(
                    f"[gerar] {rel['lote_id']}: tentativa {rel['tentativas']} de "
                    f"{rel['max_tentativas']}, lote em {rel['status']!r}",
                    file=sys.stderr,
                )
                if rel["status"] == gmod.CLAIMED:
                    # Falha de validação NÃO muda o estado: o lote segue
                    # reivindicado e o retry dirigido é a correção normal.
                    print(
                        "[gerar] o lote SEGUE reivindicado — corrija o arquivo e importe de novo",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[gerar] tentativas esgotadas — reviva com "
                        f"`pf annotate gerar preparar --lote {rel['lote_id']}`",
                        file=sys.stderr,
                    )
                return 1
            gravado = ", ".join(f"{n} {t}" for t, n in rel["gravado"].items() if n)
            print(
                f"[gerar] {rel['lote_id']} ({rel['campanha']}): {rel['n_itens']} item(ns) "
                f"validado(s); gravado: {gravado or 'nada novo'}"
            )
            if rel["campanha"] == "anotacoes":
                print(
                    "[gerar] cada anotação nasceu com o alvo escondido em "
                    f"anotacoes.{adb.COLUNA_GABARITO_AVALIACAO} — é a marca de sintética"
                )
            return 0

        print(
            f"[pf] subação desconhecida: {sub!r} (use preparar | importar | status)",
            file=sys.stderr,
        )
        return 2
    finally:
        conn.close()
        conn_corpus.close()
        _limpar_shm(corpus)


def _copiar_lote(origem: Path, out_dir: str | None) -> Path:
    """Entrega o arquivo do lote onde o maestro pediu.

    ``--out-dir`` existe desde o primeiro dia pela pegadinha que o M5 já
    documentou: a saída de ferramenta do harness trunca em ~30k caracteres e um
    lote de dezenas de prompts do corpus passa disso com folga. O fluxo real é
    sempre por arquivo — imprimir o lote no terminal entregaria um JSON cortado
    ao meio, que o agente aceitaria e responderia pela metade.
    """
    if not out_dir:
        return origem
    destino = Path(out_dir) / origem.name
    destino.parent.mkdir(parents=True, exist_ok=True)
    from .labeling_io import escrever_texto_atomico

    return escrever_texto_atomico(destino, origem.read_text(encoding="utf-8"))


def _export(args: argparse.Namespace) -> int:
    """``pf export`` (M9) — o mesmo export da interface, pela linha de comando.

    Divide a lógica com ``POST /api/export``: o formato do arquivo, o manifesto
    e a política de licença saem de ``prompt_factory.export``, nunca duplicados
    aqui. O que este comando faz a mais é resolver o nome da coleção para o id.
    """
    from pathlib import Path

    from . import db as dbmod
    from . import export as exportmod
    from . import paths
    from .app import presenters, queries
    from .app.models import Filtros, dump_filtros
    from .config import get

    banco = Path(args.db).resolve() if args.db else paths.DB_FILE
    if not banco.is_file():
        print(f"[pf] banco não encontrado em {banco} — rode `pf load-db`", file=sys.stderr)
        return 1

    conn = dbmod.connect(banco)
    try:
        filtro = Filtros(
            lang=[args.lang] if args.lang else None,
            commercial_only=bool(args.commercial_only),
            # A CLI exporta o recorte inteiro por padrão: quem digita um comando
            # de export quer o conjunto, não a política de exibição da tela.
            nsfw="include",
        )
        modo = "filter"
        if args.collection:
            linha = conn.execute(
                "SELECT id FROM collections WHERE name = ?", (args.collection,)
            ).fetchone()
            if linha is None:
                nomes = [str(r["name"]) for r in conn.execute("SELECT name FROM collections")]
                print(
                    f"[pf] coleção {args.collection!r} não existe "
                    f"(existem: {', '.join(nomes) or 'nenhuma'})",
                    file=sys.stderr,
                )
                return 1
            filtro = filtro.model_copy(update={"collection_id": int(linha["id"])})
            modo = "collection"

        sql, params = queries.sql_export(filtro)
        chave = f"flat.{args.format}"
        resultado = exportmod.executar(
            queries.executar_fts(conn, sql, params, None),
            destino_dir=Path(args.out).resolve() if args.out else paths.EXPORTS,
            chave_formato=chave,
            atribuicoes=presenters.atribuicoes(),
            nome=args.name,
            mode=modo,
            filtros=dump_filtros(filtro),
            include_nonredistributable=bool(args.include_nonredistributable),
            include_text_original=bool(args.include_text_original),
            max_rows=int(get("app", "export_max_rows", default=250_000)),
            meta_banco={"build_id": dbmod.get_meta(conn, "db_build_id")},
        )
        export_id = exportmod.registrar(conn, resultado, filtros=dump_filtros(filtro))
    finally:
        conn.close()

    m = resultado.manifest
    print(f"[export] #{export_id} {resultado.arquivo} — {resultado.row_count} linhas")
    print(f"[export] manifesto: {resultado.manifesto}")
    print(f"[export] sha256: {m['sha256']}")
    if m["excluded_nonredistributable_count"]:
        print(
            f"[export] {m['excluded_nonredistributable_count']} linha(s) EXCLUÍDAS por "
            "redistributable=0 (use --include-nonredistributable para uso local)"
        )
    if m["nao_comercial_count"]:
        print(f"[export] {m['nao_comercial_count']} linha(s) não comerciais (CC-BY-NC) incluídas")
    if "WARNING" in m:
        print(f"[export] ATENÇÃO: {m['WARNING']}")
    return 0


COMMANDS: tuple[_Cmd, ...] = (
    _Cmd(
        "ingest",
        "M2",
        "baixa fontes do HuggingFace e grava data/raw/<fonte>.parquet",
        [
            (
                ("source",),
                {
                    "nargs": "*",
                    "metavar": "FONTE",
                    "help": "seções de config/sources.toml (hífen vale por _), ou 'all' (padrão: todas as default_on)",
                },
            ),
            (
                ("--resume",),
                {"action": "store_true", "help": "alias explícito do padrão: retomar do checkpoint (wildchat)"},
            ),
            (
                ("--restart",),
                {"action": "store_true", "help": "apaga checkpoint e part-files e refaz o passe do zero (wildchat)"},
            ),
            (
                ("--downsample",),
                {"action": "store_true", "help": "só wildchat-en: pool -> 100.000 estratificadas, sem rede"},
            ),
            (
                ("--annotate-db",),
                {
                    "metavar": "ARQ",
                    "help": "só plataforma: outro annotate.sqlite (padrão: data/db/annotate.sqlite)",
                },
            ),
            _MAX_ROWS,
            _DATA_DIR,
            _FORCE,
        ],
        implemented=True,
        handler=_ingest,
    ),
    _Cmd(
        "run",
        "M4",
        "roda estágios da pipeline (s01 normalize .. s06 dedup próximo)",
        [
            (("stages",), {"nargs": "*", "metavar": "STAGE", "help": "ex.: s01 s02, s01-s06 ou all (padrão: all)"}),
            _MAX_ROWS,
            _DATA_DIR,
            _FORCE,
        ],
        implemented=True,
        handler=_run,
    ),
    _Cmd(
        "report",
        "M2",
        "resumo legível de um parquet/estágio (NUNCA abra parquet com cat)",
        [
            (
                ("target",),
                {
                    "nargs": "?",
                    "choices": ["raw", "universe", "dedup-sample"],
                    "help": "'raw' (M2), 'universe' ou 'dedup-sample' (M4)",
                },
            ),
            (("--head",), {"type": int, "default": 2, "metavar": "N", "help": "mostra N linhas de exemplo por fonte"}),
            (("--sources",), {"nargs": "+", "metavar": "FONTE", "help": "restringe o `report raw` a estas fontes"}),
            _DATA_DIR,
        ],
        implemented=True,
        handler=_report,
    ),
    _Cmd(
        "db-check",
        "M1",
        "sanidade do SQLite: DDL, WAL e busca FTS sem acento ('coracao' acha 'coração')",
        [
            (("--query",), {"metavar": "Q", "help": "termo de teste do FTS5"}),
            (
                ("--bench",),
                {"action": "store_true", "help": "mede as consultas da interface no banco carregado (só leitura)"},
            ),
            (("--db",), {"metavar": "PATH", "help": "banco a medir no --bench (padrão: data/db/prompts.sqlite)"}),
        ],
        implemented=True,
        handler=_db_check,
    ),
    _Cmd(
        "make-seed",
        "M5",
        "s07: amostra-semente estratificada (fonte x tamanho) + lotes de rotulagem",
        [
            (("--total",), {"type": int, "metavar": "N", "help": "sobrescreve [seed] size"}),
            _DATA_DIR,
            _LABELING_DIR,
            (
                ("--force",),
                {
                    "action": "store_true",
                    "help": "APAGA a campanha em andamento e refaz a semente do zero",
                },
            ),
        ],
        implemented=True,
        handler=_make_seed,
    ),
    _Cmd(
        "labels",
        "M5",
        "opera a campanha: reivindica lotes, valida respostas e escreve o manifest",
        [
            (
                ("action",),
                {
                    "nargs": "?",
                    "choices": ["status", "list", "next", "submit", "gold", "requeue"],
                    "help": "status (padrão) | list | next (claim+imprime) | submit | gold | requeue",
                },
            ),
            (("--batch",), {"metavar": "ID", "help": "um lote específico (ex.: batch_0007)"}),
            (("--file",), {"metavar": "PATH", "help": "JSONL da resposta (submit) ou do ouro (gold)"}),
            (
                ("--out",),
                {
                    "metavar": "PATH",
                    "help": "grava o JSON do lote em arquivo (a saída de ferramenta do agente trunca ~30k)",
                },
            ),
            (("--out-dir",), {"metavar": "DIR", "help": "um <batch_id>.json por lote reivindicado"}),
            (
                ("-n", "--count"),
                {"type": int, "default": 1, "metavar": "N", "help": "quantos lotes reivindicar (next)"},
            ),
            (("--model",), {"metavar": "NOME", "help": "modelo que rotulou (registrado no manifest)"}),
            (("--status",), {"metavar": "S", "help": "filtra o `list` por status"}),
            (("--limit",), {"type": int, "metavar": "N", "help": "corta o `list` em N linhas"}),
            (("--failed",), {"action": "store_true", "help": "requeue: revive todos os lotes failed"}),
            (("--reason",), {"metavar": "TEXTO", "help": "motivo registrado no requeue"}),
            (("--json",), {"action": "store_true", "help": "status em JSON, para script"}),
            _LABELING_DIR,
        ],
        implemented=True,
        handler=_labels,
    ),
    _Cmd(
        "merge-labels",
        "M5",
        "s08: consolida rótulos dos agentes + rótulos nativos mapeados",
        [
            (
                ("--strict",),
                {"action": "store_true", "help": "falha se um lote done estiver sem arquivo de rótulos"},
            ),
            _DATA_DIR,
            _LABELING_DIR,
        ],
        implemented=True,
        handler=_merge_labels,
    ),
    _Cmd(
        "train",
        "M7",
        "s09: treina o classificador (LogReg sobre embeddings) por eixo",
        [
            (("--axis",), {"choices": ["task_type", "domain", "quality"], "help": "treina só um eixo"}),
            (("--folds",), {"type": int, "default": 5, "metavar": "K", "help": "folds da busca de C"}),
            _DATA_DIR,
        ],
        implemented=True,
        handler=_train,
    ),
    _Cmd(
        "apply",
        "M7",
        "s10: aplica o classificador ao universo, com limiares de confiança",
        [_MAX_ROWS, _FORCE, _DATA_DIR],
        implemented=True,
        handler=_apply,
    ),
    _Cmd(
        "load-db",
        "M8",
        "s11: carga bulk no SQLite (build + rebuild do FTS + swap de arquivo)",
        [
            (
                ("--swap",),
                {"action": "store_true", "help": "alias explícito do padrão: trocar o banco no fim"},
            ),
            (
                ("--no-swap",),
                {"action": "store_true", "help": "constrói db/prompts.build.sqlite e PARA (não troca)"},
            ),
            (
                ("--swap-only",),
                {
                    "action": "store_true",
                    "help": "só a troca, sem reconstruir — é o retry de um swap recusado por lock",
                },
            ),
            (
                ("--allow-unlabeled-pct",),
                {
                    "type": float,
                    "metavar": "PCT",
                    "help": "teto de linhas sem task_type (padrão: [loaddb] allow_unlabeled_pct = 1)",
                },
            ),
            (("--backup",), {"action": "store_true", "help": "grava um .bak do banco anterior antes de trocar"}),
            (
                ("--deep",),
                {"action": "store_true", "help": "PRAGMA integrity_check (varre tudo) no lugar do quick_check"},
            ),
            _DATA_DIR,
        ],
        implemented=True,
        handler=_load_db,
    ),
    _Cmd(
        "export",
        "M9",
        "exporta JSONL/CSV com licença e atribuição por linha + manifesto",
        [
            (("--format",), {"choices": ["jsonl", "csv"], "default": "jsonl", "help": "formato de saída"}),
            (("--collection",), {"metavar": "NOME", "help": "exporta uma coleção específica"}),
            (("--lang",), {"choices": ["pt", "en"], "help": "restringe a um idioma"}),
            (
                ("--commercial-only",),
                {"action": "store_true", "help": "só linhas com commercial_ok=1 (exclui CC-BY-NC)"},
            ),
            (
                ("--include-nonredistributable",),
                {
                    "action": "store_true",
                    "help": "inclui redistributable=0 — USO LOCAL, o manifesto carimba um WARNING",
                },
            ),
            (
                ("--include-text-original",),
                {"action": "store_true", "help": "acrescenta a coluna text_original"},
            ),
            (("--name",), {"metavar": "NOME", "help": "nome do arquivo (padrão: export_<timestamp>)"}),
            (("--out",), {"metavar": "DIR", "help": "diretório de saída (padrão: data/exports)"}),
            (("--db",), {"metavar": "PATH", "help": "banco a exportar (padrão: data/db/prompts.sqlite)"}),
        ],
        implemented=True,
        handler=_export,
    ),
    _Cmd(
        "serve",
        "M9",
        "sobe a interface local (FastAPI + static/index.html)",
        [
            (("--host",), {"metavar": "HOST", "help": "padrão: [app] host do settings.toml"}),
            (("--port",), {"type": int, "metavar": "PORT", "help": "padrão: [app] port do settings.toml"}),
            (("--db",), {"metavar": "PATH", "help": "banco a servir (padrão: data/db/prompts.sqlite)"}),
            (("--reload",), {"action": "store_true", "help": "auto-reload do uvicorn (dev)"}),
        ],
        implemented=True,
        handler=_serve,
    ),
    _Cmd(
        "annotate",
        "P3a",
        "sobe a Bancada: plataforma de anotação (banco próprio; corpus só leitura)",
        [
            (
                ("action",),
                {
                    "nargs": "?",
                    "choices": [
                        "serve",
                        "seed",
                        "status",
                        "migrate",
                        "gerar",
                        "pedidos",
                        "export",
                    ],
                    "help": (
                        "serve (padrão) | seed (personas + fixtures + tarefas) | "
                        "status | migrate (sobe o schema preservando as anotações) | "
                        "gerar (campanha de material e de anotações sintéticas) | "
                        "pedidos (destila pedidos de prompt do material didático) | "
                        "export (os artefatos de entrega)"
                    ),
                },
            ),
            (
                ("subacao",),
                {
                    "nargs": "?",
                    "choices": ["preparar", "importar", "status"],
                    "help": "de `gerar` e `pedidos`: preparar | importar | status (padrão)",
                },
            ),
            (("--host",), {"metavar": "HOST", "help": "padrão: [annotate] host do settings.toml"}),
            (("--port",), {"type": int, "metavar": "PORT", "help": "padrão: [annotate] port (8766)"}),
            (
                ("--db",),
                {"metavar": "PATH", "help": "banco da plataforma (padrão: data/db/annotate.sqlite)"},
            ),
            (
                ("--corpus-db",),
                {"metavar": "PATH", "help": "corpus, SÓ LEITURA (padrão: data/db/prompts.sqlite)"},
            ),
            (
                ("--force",),
                {
                    "action": "store_true",
                    "help": "seed: APAGA fixtures e tarefas e refaz (recusa se houver anotação)",
                },
            ),
            (
                ("--campanha",),
                {
                    "choices": ["material", "anotacoes"],
                    "help": "gerar preparar: rubrica+2 respostas | anotações sintéticas",
                },
            ),
            (
                ("-n", "--count"),
                {
                    "type": int,
                    "metavar": "N",
                    "help": (
                        "gerar preparar: itens no lote (padrão 8); "
                        "pedidos preparar: janelas no lote (padrão 6)"
                    ),
                },
            ),
            (
                ("--arquivo",),
                {
                    "metavar": "NOME",
                    "help": (
                        "pedidos preparar: o .txt do material (só o NOME; a pasta vem de "
                        "[pedidos] material_dir)"
                    ),
                },
            ),
            (
                ("--lang",),
                {
                    "choices": ["pt", "en", "ambos"],
                    "help": "gerar preparar: língua dos prompts do lote (padrão: ambos)",
                },
            ),
            (
                ("--lote",),
                {
                    "metavar": "ID",
                    "help": "gerar/pedidos: o lote (mat_0001, ped_0001); em preparar, retoma",
                },
            ),
            (
                ("--file",),
                {"metavar": "PATH", "help": "gerar/pedidos importar: o JSON que o agente devolveu"},
            ),
            (
                ("--out-dir",),
                {
                    "metavar": "DIR",
                    "help": (
                        "gerar/pedidos preparar: copia o lote para cá (a saída do agente "
                        "trunca ~30k); export: onde gravar (padrão: data/exports/annotate)"
                    ),
                },
            ),
            (
                ("--status",),
                {
                    "choices": ["pendente_triagem", "pendente_avaliacao"],
                    "help": "gerar importar: onde a sintética entra (padrão: [annotate] sinteticas_status)",
                },
            ),
            (("--model",), {"metavar": "NOME", "help": "gerar importar: modelo que gerou (no manifest)"}),
            (
                ("--geracao-dir",),
                {"metavar": "DIR", "help": "raiz da campanha (padrão: geracao/)"},
            ),
            (
                ("--destilacao-dir",),
                {"metavar": "DIR", "help": "raiz da campanha de pedidos (padrão: destilacao/)"},
            ),
            (
                ("--json",),
                {"action": "store_true", "help": "gerar/pedidos status em JSON, para script"},
            ),
            (
                ("--perfil",),
                {
                    "choices": [
                        "all",
                        "annotations",
                        "sft",
                        "preference",
                        "quality-report",
                        "dataset-card",
                        "audit",
                    ],
                    "help": "export: qual artefato (padrão: all, menos a auditoria)",
                },
            ),
            (
                ("--name",),
                {"metavar": "NOME", "help": "export: nome-base do arquivo (só com um perfil)"},
            ),
            (
                ("--tipo",),
                {
                    "action": "append",
                    "metavar": "TIPO",
                    "help": "export: recorta por tipo de tarefa (repetível)",
                },
            ),
            (("--projeto",), {"metavar": "NOME", "help": "export: recorta por projeto"}),
            (
                ("--anotacao",),
                {"type": int, "metavar": "ID", "help": "export: a anotação do perfil `audit`"},
            ),
            (
                ("--incluir-sinteticas",),
                {
                    "action": "store_true",
                    "help": "export: inclui anotações sintéticas nos perfis de DADO (fora por padrão)",
                },
            ),
            (
                ("--incluir-pendentes",),
                {
                    "action": "store_true",
                    "help": "export: inclui o que passou na triagem mas não no Rate and Review",
                },
            ),
        ],
        implemented=True,
        handler=_annotate,
    ),
)


def _bootstrap_env() -> None:
    """UTF-8, HF_HOME e CA bundle antes de tudo. Idempotente e à prova de falha."""
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        from .config import hf_home

        os.environ.setdefault("HF_HOME", hf_home())
    except (OSError, KeyError, ValueError) as exc:
        # settings.toml ausente, ilegível ou malformado (TOMLDecodeError é
        # ValueError) não pode derrubar um `pf --help`. Avisa e segue.
        print(f"[pf] aviso: HF_HOME não configurado ({exc})", file=sys.stderr)
    try:
        # TLS interceptado nesta máquina: o requests (huggingface_hub) precisa
        # do armazenamento de certificados do Windows. Ver certs.py.
        from .certs import ensure_ca_bundle

        ensure_ca_bundle()
    except Exception as exc:  # pragma: no cover - bootstrap nunca derruba a CLI
        print(f"[pf] aviso: CA bundle do sistema não gerado ({exc})", file=sys.stderr)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # stream capturado (pytest) ou já fechado
            pass


def build_parser() -> argparse.ArgumentParser:
    """Monta o parser completo — usado pela CLI e pelos testes."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Prompt Factory — banco de prompts reais (pt-BR + EN). "
            "Estágios leem/escrevem Parquet imutável em data/; o banco final é SQLite."
        ),
        epilog="Detalhes de arquitetura e pegadinhas: ver CLAUDE.md.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="cmd", metavar="<comando>")
    for cmd in COMMANDS:
        nota = (
            "" if cmd.implemented else f" [implementação prevista para o {cmd.milestone}]"
        )
        sub = subparsers.add_parser(
            cmd.name,
            help=cmd.help,
            description=f"{cmd.help}{nota}",
        )
        for flags, kwargs in cmd.options:
            sub.add_argument(*flags, **kwargs)  # type: ignore[arg-type]
        sub.set_defaults(_cmd_spec=cmd)
    return parser


def stub_commands() -> tuple[_Cmd, ...]:
    """Comandos ainda não implementados — usado pelos testes."""
    return tuple(c for c in COMMANDS if not c.implemented)


def main(argv: Sequence[str] | None = None) -> int:
    """Ponto de entrada do console script ``pf``. Retorna o exit code."""
    _bootstrap_env()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd is None:
        parser.print_help()
        return 2

    spec: _Cmd = args._cmd_spec
    if not spec.implemented or spec.handler is None:
        print(f"[pf] {args.cmd!r} ainda não implementado (chega no {spec.milestone})")
        return 2

    return int(spec.handler(args))  # type: ignore[operator]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
