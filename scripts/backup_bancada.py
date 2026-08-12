"""Dump textual do annotate.sqlite para backups/bancada.sql — e prova que restaura.

O annotate.sqlite é o único banco NÃO regenerável do projeto: ele guarda
trabalho humano (anotações, revisões, criações), não artefato de pipeline.
O backup dele é um dump SQL em texto porque texto entra no git — e o git
deste repositório tem remoto, então o dump sobrevive ao disco.

O caminho é backup API → :memory: → iterdump, e não iterdump direto na
conexão do arquivo: a Bancada pode estar NO AR durante o dump, e a backup
API do sqlite dá um snapshot consistente mesmo com escritor ativo. O dump
de uma cópia congelada nunca mistura duas gerações do banco.

Todo passe termina restaurando o dump num :memory: e comparando as
contagens tabela a tabela — um backup que nunca foi restaurado é uma
esperança, não um backup.

Uso:  uv run python scripts/backup_bancada.py
"""

import sqlite3
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
BANCO = RAIZ / "data" / "db" / "annotate.sqlite"
DESTINO = RAIZ / "backups" / "bancada.sql"

# as tabelas cujo conteúdo é trabalho humano — são elas que o restore confere
TABELAS_CONFERIDAS = (
    "anotadores",
    "tarefas",
    "atribuicoes",
    "anotacoes",
    "revisoes",
    "avaliacoes",
    "criacoes",
)


def contar(conn: sqlite3.Connection) -> dict[str, int]:
    contagens = {}
    for t in TABELAS_CONFERIDAS:
        try:
            contagens[t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            # tabela de um schema futuro/passado: ausente conta como ausente,
            # não como zero — zero afirmaria uma tabela vazia que não existe
            contagens[t] = None
    return contagens


def main() -> int:
    if not BANCO.exists():
        print(f"[backup] {BANCO} nao existe — nada a fazer")
        return 1

    origem = sqlite3.connect(f"file:{BANCO}?mode=ro", uri=True)
    copia = sqlite3.connect(":memory:")
    origem.backup(copia)
    origem.close()

    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    # escrita de dentro do Python, em utf-8: redirecionar no PowerShell
    # gravaria UTF-16/BOM e quebraria o restore (pegadinha do CLAUDE.md)
    with open(DESTINO, "w", encoding="utf-8", newline="\n") as f:
        for linha in copia.iterdump():
            f.write(linha + "\n")

    antes = contar(copia)

    # a prova: o dump recém-escrito reconstrói um banco com as mesmas contagens
    restaurado = sqlite3.connect(":memory:")
    restaurado.executescript(DESTINO.read_text(encoding="utf-8"))
    depois = contar(restaurado)

    if antes != depois:
        print(f"[backup] RESTORE DIVERGIU: {antes} != {depois}")
        return 1

    kb = DESTINO.stat().st_size / 1024
    resumo = " ".join(f"{t}={n}" for t, n in antes.items() if n is not None)
    print(f"[backup] {DESTINO.name}: {kb:.0f} KB, restore conferido ({resumo})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
