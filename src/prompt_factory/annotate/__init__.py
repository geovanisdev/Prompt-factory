"""``prompt_factory.annotate`` — a plataforma de anotação ("Bancada").

Produto NOVO, separado da interface de curadoria de ``prompt_factory.app``:

* outro banco (``data/db/annotate.sqlite``, escrito por esta app e só por ela);
* outra porta (``[annotate] port`` = 8766, contra 8765 da curadoria);
* outra identidade visual (clara, uma tarefa por vez) — as duas sobem juntas na
  demonstração e precisam parecer produtos de empresas diferentes.

O ``prompts.sqlite`` é **somente leitura** aqui, sempre: a app de curadoria
assume ser a única escritora do corpus (ver ``app/cache.py``), e essa suposição
é o que permite a ela cachear agregados. Toda referência cruzada entre os dois
bancos é por ``uid`` — o ``id``/rowid do corpus muda a cada ``pf load-db``.
"""

from __future__ import annotations

__all__ = [
    "catalogo",
    "db",
    "deps",
    "main",
    "models",
    "payloads",
    "pool",
    "routes_meta",
    "routes_trabalho",
    "seed",
    "tarefas",
]
