# -*- coding: utf-8 -*-
"""Guardas das 3 consultas que consumiam o Disk IO do projeto.

Contexto (05/09/2026). Alerta "Disk IO Budget depleting" no Supabase. A suspeita
era mec_catalog (185k) e school_enem_yearly (345k). O pg_stat_statements diz
outra coisa — quem sangra e school_censo_yearly (1,28M linhas / 546 MB):

    #1  SELECT qt_mat_bas FROM school_censo_yearly
        WHERE vintage_censo=$1 AND city=$2
        14.810 chamadas · 12.970.089 blocos de disco (~99 GB) · 876/chamada
        -> tools/insight_charts.generate_trend_chart: uma consulta POR ANO,
           cada uma lendo todas as escolas da cidade.

    #2  SELECT state FROM school_censo_yearly WHERE vintage_censo=$1
        2.204 chamadas · 2.143.089 blocos (~16 GB) · 972/chamada
        -> dashboard/helpers/school_lookup.get_ufs(): paginava o Brasil inteiro
           pra descobrir 27 siglas de UF, a cada 5 min.

    #3  school_analytics ... peer_mun_nome ILIKE $2
        2.456 chamadas · 868.366 blocos (~7 GB) · 731 ms de media
        -> agent/tools/enem_tools:713. O ilike descarta idx_sa_mun_dep. Medido:
           4.090 buffers / 2.918 ms com ilike contra 51 buffers / 5,9 ms com "=".

Estes testes travam as tres correcoes. Sao guardas de FORMA (AST/estatica)
porque o custo so aparece em producao, com a tabela real — nao ha como um teste
unitario "sentir" 99 GB de I/O.
"""
import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INSIGHT = ROOT / "tools" / "insight_charts.py"
LOOKUP = ROOT / "dashboard" / "helpers" / "school_lookup.py"
ENEM = ROOT / "agent" / "tools" / "enem_tools.py"
MIGRATION = ROOT / "database" / "migrations" / "APLICAR-025-INDICE-BENCHMARK-CENSO.sql"


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _func(path, nome):
    for n in ast.walk(_tree(path)):
        if isinstance(n, ast.FunctionDef) and n.name == nome:
            return n
    raise AssertionError(f"{nome} sumiu de {path.name}")


# ---------------------------------------------------------------------------
# #1 — benchmark do trend: uma consulta, nao uma por ano
# ---------------------------------------------------------------------------
def test_benchmark_nao_consulta_dentro_de_loop_de_anos():
    """Um `.execute()` dentro do `for year` e o N+1 que gerou os 99 GB."""
    fn = _func(INSIGHT, "generate_trend_chart")
    for laco in [n for n in ast.walk(fn) if isinstance(n, ast.For)]:
        alvo = getattr(laco.target, "id", "")
        if alvo not in ("year", "ano"):
            continue
        tem_query = any(
            isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr == "execute"
            for c in ast.walk(laco)
        )
        assert not tem_query, (
            f"insight_charts:{laco.lineno} voltou a consultar o banco dentro do "
            f"loop de anos — e o N+1 que consumia ~99 GB de I/O")


def test_benchmark_usa_um_in_com_todos_os_anos():
    fn = _func(INSIGHT, "_benchmark_municipio")
    usa_in = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "in_"
        for n in ast.walk(fn)
    )
    assert usa_in, "_benchmark_municipio deixou de agrupar os anos num IN"


def test_benchmark_e_memoizado():
    """O mesmo numero serve toda escola da cidade: 786 consultas viram 66."""
    fn = _func(INSIGHT, "_benchmark_municipio")
    src = ast.get_source_segment(INSIGHT.read_text(encoding="utf-8"), fn) or ""
    assert "_BENCH_CACHE" in src, "o memo do benchmark por cidade sumiu"


def test_memo_devolve_o_mesmo_objeto_sem_reconsultar():
    """Segunda chamada nao pode tocar o banco."""
    import tools.insight_charts as ic

    class _DbQueQuebraNaSegunda:
        def __init__(self):
            self.chamadas = 0

        def table(self, _n):
            self.chamadas += 1
            return self

        def select(self, *a, **k):
            return self

        def in_(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def execute(self):
            return type("R", (), {"data": [
                {"vintage_censo": 2024, "qt_mat_bas": 100},
                {"vintage_censo": 2024, "qt_mat_bas": 300},
            ]})()

    ic._BENCH_CACHE.clear()
    fake = _DbQueQuebraNaSegunda()
    db = type("D", (), {"client": fake})()
    a = ic._benchmark_municipio(db, "Cidade X", [2024], "qt_mat_bas")
    b = ic._benchmark_municipio(db, "Cidade X", [2024], "qt_mat_bas")
    assert a == b == {2024: (200.0, 2)}
    assert fake.chamadas == 1, "o memo nao segurou a segunda chamada"
    ic._BENCH_CACHE.clear()


def test_benchmark_calcula_media_e_contagem_por_ano():
    import tools.insight_charts as ic

    class _Fake:
        def table(self, _n): return self
        def select(self, *a, **k): return self
        def in_(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def execute(self):
            return type("R", (), {"data": [
                {"vintage_censo": 2023, "qt_mat_bas": 10},
                {"vintage_censo": 2023, "qt_mat_bas": 20},
                {"vintage_censo": 2024, "qt_mat_bas": 50},
                {"vintage_censo": 2024, "qt_mat_bas": None},  # NULL nao entra
            ]})()

    ic._BENCH_CACHE.clear()
    out = ic._benchmark_municipio(type("D", (), {"client": _Fake()})(),
                                  "C", [2023, 2024], "qt_mat_bas")
    assert out == {2023: (15.0, 2), 2024: (50.0, 1)}
    ic._BENCH_CACHE.clear()


def test_falha_do_banco_nao_derruba_o_grafico():
    import tools.insight_charts as ic

    class _Explode:
        def table(self, _n): raise RuntimeError("banco fora")

    ic._BENCH_CACHE.clear()
    out = ic._benchmark_municipio(type("D", (), {"client": _Explode()})(),
                                  "C", [2024], "qt_mat_bas")
    assert out == {}
    ic._BENCH_CACHE.clear()


# ---------------------------------------------------------------------------
# #2 — get_ufs: 27 siglas nao se descobrem varrendo 1,28M linhas
# ---------------------------------------------------------------------------
def test_get_ufs_nao_consulta_o_banco():
    fn = _func(LOOKUP, "get_ufs")
    tem_query = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in ("execute", "range")
        for n in ast.walk(fn)
    )
    assert not tem_query, (
        "get_ufs voltou a varrer school_censo_yearly pra achar 27 siglas "
        "(~16 GB de I/O medidos)")


def test_get_ufs_devolve_as_27_ordenadas():
    """Conferido no banco em 05/09/2026: o DISTINCT do censo dava estas 27."""
    from dashboard.helpers.school_lookup import get_ufs
    ufs = get_ufs.__wrapped__() if hasattr(get_ufs, "__wrapped__") else get_ufs()
    assert len(ufs) == 27
    assert ufs == sorted(ufs)
    assert {"RS", "SP", "SC", "MG", "DF"} <= set(ufs)


# ---------------------------------------------------------------------------
# #3 — benchmark do radar: "=" e nao ilike
# ---------------------------------------------------------------------------
def test_radar_compara_municipio_por_igualdade():
    """ilike aqui descarta o indice E casa municipio errado.

    O valor vem do proprio school_analytics (peer_mun_nome da escola), entao
    nao ha nada a casar parcialmente. "%Sao Jose%" pegava 58 municipios.
    """
    fonte = ENEM.read_text(encoding="utf-8")
    tree = ast.parse(fonte)
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
            continue
        if n.func.attr != "ilike" or not n.args:
            continue
        col = n.args[0]
        if not (isinstance(col, ast.Constant) and col.value == "peer_mun_nome"):
            continue
        # ilike com valor digitado pelo usuario (busca livre no IAlex/painel) e
        # intencional. O proibido e usar ilike sobre o valor lido do banco.
        trecho = ast.get_source_segment(fonte, n) or ""
        assert "{mun}" not in trecho, (
            f"enem_tools:{n.lineno} voltou a usar ilike no benchmark do radar "
            f"(4.090 buffers / 2.918 ms contra 51 / 5,9 ms com '=')")


def test_busca_livre_do_usuario_continua_parcial():
    """Nao quebrei a busca por digitacao: 'porto' tem que achar Porto Alegre."""
    fonte = ENEM.read_text(encoding="utf-8")
    parciais = [n.lineno for n in ast.walk(ast.parse(fonte))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "ilike" and n.args
                and isinstance(n.args[0], ast.Constant)
                and n.args[0].value == "peer_mun_nome"]
    assert len(parciais) >= 2, (
        "a busca parcial por municipio digitado pelo usuario foi removida junto")


# ---------------------------------------------------------------------------
# Migration do indice coberto
# ---------------------------------------------------------------------------
def test_migration_do_indice_existe_e_e_aditiva():
    assert MIGRATION.exists(), "migration 025 sumiu"
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "create index" in sql and "if not exists" in sql
    assert "include (qt_mat_bas" in sql, "o indice deixou de ser coberto"
    assert "concurrently" in sql, "sem CONCURRENTLY o create trava escrita"
    for proibido in ("drop ", "delete ", "truncate", "alter table"):
        assert proibido not in sql, f"migration deixou de ser aditiva: {proibido}"


# ---------------------------------------------------------------------------
# #4 — facets do catalogo: 3 DISTINCT de tabela cheia por ~37 strings
# ---------------------------------------------------------------------------
MEC_SOURCE = ROOT / "dashboard" / "_mec_source.py"


def test_facets_do_catalogo_tem_ttl_longo():
    """A RPC mec_catalog_facets() custa ~82 MB de disco e 2,17 s por chamada.

    Ela faz TRES DISTINCT de tabela cheia sobre mec_catalog (185k linhas) pra
    devolver 27 UFs + ~4 dependencias + ~6 portes — valores que so mudam na
    recarga anual do catalogo. Com TTL de 10 min, o painel pagava isso a cada
    10 minutos de uso.
    """
    import dashboard._mec_source as ms
    assert ms._TTL_FACETS >= 3600, (
        "o TTL dos facets do catalogo voltou a ser curto — cada expiracao "
        "custa ~82 MB de I/O por um dado que muda uma vez por ano")


def test_facets_e_total_usam_o_mesmo_ttl():
    """Os dois batem na mesma tabela pesada; TTLs diferentes so criam surpresa."""
    fonte = MEC_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(fonte)
    alvos = {"_cat_facets", "_cat_total"}
    vistos = {}
    for n in ast.walk(tree):
        if not (isinstance(n, ast.FunctionDef) and n.name in alvos):
            continue
        for dec in n.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            for kw in dec.keywords:
                if kw.arg == "ttl":
                    vistos[n.name] = ast.dump(kw.value)
    assert len(vistos) == 2, f"nao achei o cache das duas funcoes: {vistos}"
    assert len(set(vistos.values())) == 1, (
        f"_cat_facets e _cat_total divergiram de TTL: {vistos}")
