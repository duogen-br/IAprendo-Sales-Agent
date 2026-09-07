"""_mec_source - Fonte de dados da base MEC com PARIDADE local/online.

A UI (Importar, Mapa) e escrita UMA vez (a versao local, correta). Esta camada
escolhe o backend conforme o ambiente:
  - LOCAL: CSV mesclado (pandas) — quando o arquivo existe.
  - ONLINE (Cloud): tabela mec_catalog no Supabase (SQL/RPC) — quando nao existe.

Mesma interface nos dois -> a pagina nao sabe (nem precisa saber) qual backend
esta ativo. Colunas internas canonicas (iguais ao rename local do CSV):
    escola, inep, uf, municipio, dep_adm, porte, niveis, latitude, longitude, fonte_dados

filters (dict) usado por count/preview/points/import:
    {ufs:[], cities:[], deps:[], portes:[], inc_fund:bool, inc_medio:bool}
"""
from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings

CSV_PATH = ROOT / settings.CSV_PATH

# 27 UFs (fallback quando o RPC de facets nao esta disponivel no Cloud).
UF_LIST = [
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
    "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
    "SP", "SE", "TO",
]

_CANON_COLS = ["escola", "inep", "uf", "municipio", "dep_adm", "porte",
               "niveis", "latitude", "longitude", "fonte_dados",
               "nivel_tecnologico", "mat_fund_af", "mat_medio"]

# Colunas do preview — UMA lista para os dois backends. Quando cada preview
# tinha a sua, a tela local e a online mostravam coisas diferentes e so dava pra
# perceber trocando de ambiente. INEP entrou porque e a chave que o operador
# copia para o fluxo "Colar INEPs"; mat_fund_af porque sem ela nao da pra
# conferir na tela se o filtro de Anos Finais trouxe o que prometeu.
PREVIEW_COLS = ["inep", "escola", "municipio", "uf", "dep_adm", "porte",
                "mat_fund_af", "mat_medio"]


@st.cache_data(show_spinner="Carregando base mesclada (185k escolas)...")
def load_csv() -> Optional[pd.DataFrame]:
    """Carrega o CSV mesclado (Censo 2025 + Catalogo INEP) com colunas canonicas.
    Retorna None se o arquivo nao existe (ex: Streamlit Cloud)."""
    if not CSV_PATH.exists():
        return None
    df = pd.read_csv(str(CSV_PATH), encoding=settings.CSV_ENCODING, low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    rename_map = {
        "NOME_ESCOLA": "escola", "CODIGO_INEP": "inep", "UF": "uf",
        "MUNICIPIO": "municipio", "DEPENDENCIA": "dep_adm", "PORTE_ESCOLA": "porte",
        "PERFIL_ENSINO": "niveis", "LATITUDE": "latitude", "LONGITUDE": "longitude",
        "FONTE_DADOS": "fonte_dados", "NIVEL_TECNOLOGICO": "nivel_tecnologico",
        "MATRICULAS_FUND_AF": "mat_fund_af", "MATRICULAS_MEDIO": "mat_medio",
    }
    df = df.rename(columns=rename_map)
    for col in _CANON_COLS:
        if col not in df.columns:
            df[col] = ""
    return df


# ---------------------------------------------------------------------------
# Cache dos facets do catalogo (distinct estaveis — evita re-query a cada rerun)
# ---------------------------------------------------------------------------
# TTL de 24h, nao de 10 minutos. A RPC mec_catalog_facets() faz TRES DISTINCT
# de tabela cheia sobre mec_catalog (185k linhas / 90 MB cada) pra devolver ~37
# strings: 27 UFs, ~4 dependencias, ~6 portes. Medido em 07/09/2026:
# 10.536 blocos de disco (~82 MB) e 2,17 s POR CHAMADA — a consulta com o pior
# custo marginal do projeto.
#
# Esses valores so mudam quando o catalogo inteiro e recarregado
# (scripts/load_mec_catalog.py), o que acontece uma vez por ano. Reexpirar a
# cada 10 min era pagar 82 MB de I/O por um dado anual.
#
# Efeito colateral aceito: depois de recarregar o catalogo, os filtros da tela
# levam ate 24h (ou um restart do painel) pra refletir valores novos.
_TTL_FACETS = 86400  # 24h


@st.cache_data(ttl=_TTL_FACETS, show_spinner=False)
def _cat_facets() -> Dict[str, list]:
    from database.supabase_client import db
    return db.catalog_facets()


@st.cache_data(ttl=_TTL_FACETS, show_spinner=False)
def _cat_total() -> int:
    from database.supabase_client import db
    return db.count_mec_catalog({})


@st.cache_data(ttl=300, show_spinner=False)
def _cat_cities(ufs_tuple: tuple) -> list:
    from database.supabase_client import db
    return db.catalog_cities(list(ufs_tuple))


# ===========================================================================
# Interface
# ===========================================================================
class MecSource:
    backend = "?"

    def total(self) -> int: ...
    def ufs(self) -> List[str]: ...
    def cities(self, ufs: List[str]) -> List[str]: ...
    def deps(self) -> List[str]: ...
    def portes_raw(self) -> List[str]: ...
    def count(self, filters: Dict[str, Any]) -> int: ...
    def preview(self, filters: Dict[str, Any], n: int = 15) -> pd.DataFrame: ...
    def points(self, filters: Dict[str, Any], limit: int = 10000) -> pd.DataFrame: ...
    def import_filtered(self, filters: Dict[str, Any], limit: int = 0) -> Dict[str, Any]: ...


def _excl_sem(values: List[str]) -> List[str]:
    """Remove portes tipo 'Escola sem matriculas...' (igual a UI local)."""
    return [v for v in values if v and "Escola sem" not in v]


# ---------------------------------------------------------------------------
# Backend LOCAL (CSV / pandas)
# ---------------------------------------------------------------------------
class CsvMecSource(MecSource):
    backend = "csv"

    def __init__(self, df: pd.DataFrame):
        self.df = df

    def total(self) -> int:
        return len(self.df)

    def ufs(self) -> List[str]:
        return sorted(self.df["uf"].dropna().unique().tolist())

    def cities(self, ufs: List[str]) -> List[str]:
        d = self.df[self.df["uf"].isin(ufs)] if ufs else self.df
        return sorted(d["municipio"].dropna().unique().tolist())

    def deps(self) -> List[str]:
        return sorted(self.df["dep_adm"].dropna().unique().tolist())

    def portes_raw(self) -> List[str]:
        return _excl_sem([str(p).strip() for p in self.df["porte"].dropna().unique().tolist()])

    def _mask(self, filters: Dict[str, Any]):
        f = filters or {}
        df = self.df
        m = pd.Series(True, index=df.index)
        if f.get("ufs"):
            m &= df["uf"].isin(f["ufs"])
        if f.get("cities"):
            m &= df["municipio"].isin(f["cities"])
        if f.get("deps"):
            m &= df["dep_adm"].isin(f["deps"])
        if f.get("portes"):
            m &= df["porte"].str.strip().isin(f["portes"])
        inc_fund, inc_medio = bool(f.get("inc_fund")), bool(f.get("inc_medio"))
        if inc_fund or inc_medio:
            # Mesma regra do backend online (utils/nivel_ensino): anos finais
            # sai da matricula real, nao do texto "Fundamental" — que cobre do
            # 1o ao 9o e trazia metade de escola de anos iniciais.
            from utils.nivel_ensino import mask_fund_af, mask_medio
            nm = pd.Series(False, index=df.index)
            if inc_fund:
                nm = nm | mask_fund_af(df, "mat_fund_af", "niveis")
            if inc_medio:
                nm = nm | mask_medio(df, "niveis")
            m &= nm

        # Busca livre: INEP exato (so digitos) ou pedaco do nome.
        termo = str(f.get("q") or "").strip()
        if termo:
            if termo.isdigit():
                m &= df["inep"].astype(str).str.strip() == termo
            else:
                import unicodedata

                def _n(s):
                    return (unicodedata.normalize("NFKD", str(s))
                            .encode("ASCII", "ignore").decode("ASCII").lower())
                m &= df["escola"].astype(str).map(_n).str.contains(_n(termo), na=False)
        return m

    def count(self, filters: Dict[str, Any]) -> int:
        return int(self._mask(filters).sum())

    def preview(self, filters: Dict[str, Any], n: int = 15) -> pd.DataFrame:
        return self.df[self._mask(filters)][PREVIEW_COLS].head(n).copy()

    def points(self, filters: Dict[str, Any], limit: int = 10000) -> pd.DataFrame:
        return self.df[self._mask(filters)].head(limit).copy()

    def import_filtered(self, filters: Dict[str, Any], limit: int = 0) -> Dict[str, Any]:
        """Roda o import_schools.py (subprocess) com os filtros via env — igual ao
        comportamento local atual."""
        f = filters or {}
        env = os.environ.copy()
        env["TARGET_STATE"] = ",".join(f.get("ufs") or [])
        env["TARGET_CITY"] = ",".join(f.get("cities") or [])
        if f.get("deps"):
            env["TARGET_SCHOOL_TYPES"] = ",".join(d.lower() for d in f["deps"])
        else:
            env.pop("TARGET_SCHOOL_TYPES", None)
        env["PYTHONIOENCODING"] = "utf-8"
        script = str(ROOT / "database" / "migrations" / "import_schools.py")
        py = str(ROOT / "venv" / "Scripts" / "python.exe")
        if not Path(py).exists():
            py = sys.executable
        cmd = [py, script]
        if limit and int(limit) > 0:
            cmd += ["--sample", str(int(limit))]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", env=env, timeout=300)
            import re as _re
            out = res.stdout or ""
            ins = _re.search(r"Inseridas?[:\s]+(\d+)", out, _re.IGNORECASE)
            dup = _re.search(r"Duplicatas?[:\s]+(\d+)", out, _re.IGNORECASE)
            return {
                "ok": res.returncode == 0,
                "inseridas": int(ins.group(1)) if ins else None,
                "duplicatas": int(dup.group(1)) if dup else 0,
                "no_match": "Nenhuma escola passa nos filtros" in out,
                "log": out, "stderr": res.stderr or "", "returncode": res.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "inseridas": 0, "duplicatas": 0,
                    "error": "Importacao excedeu 5 minutos. Use um limite menor."}
        except Exception as e:
            return {"ok": False, "inseridas": 0, "duplicatas": 0, "error": str(e)}


# ---------------------------------------------------------------------------
# Backend ONLINE (mec_catalog / Supabase)
# ---------------------------------------------------------------------------
class CatalogMecSource(MecSource):
    backend = "catalog"

    def total(self) -> int:
        return _cat_total()

    def ufs(self) -> List[str]:
        st_ = _cat_facets().get("states") or []
        return st_ if st_ else list(UF_LIST)

    def cities(self, ufs: List[str]) -> List[str]:
        if not ufs:
            return []  # cascata: sem UF, nao lista (185k cidades inviavel)
        return _cat_cities(tuple(sorted(ufs)))

    def deps(self) -> List[str]:
        return _cat_facets().get("deps") or []

    def portes_raw(self) -> List[str]:
        return _excl_sem(_cat_facets().get("portes") or [])

    def count(self, filters: Dict[str, Any]) -> int:
        from database.supabase_client import db
        return db.count_mec_catalog(filters)

    def _rows_to_df(self, rows: list) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=_CANON_COLS)
        return pd.DataFrame([{
            "escola": r.get("name"), "inep": r.get("inep_code"), "uf": r.get("state"),
            "municipio": r.get("city"), "dep_adm": r.get("admin_dependency"),
            "porte": r.get("school_size"), "niveis": r.get("education_levels"),
            "latitude": r.get("latitude"), "longitude": r.get("longitude"),
            "fonte_dados": r.get("fonte_dados"),
            "nivel_tecnologico": r.get("nivel_tecnologico"),
            "mat_fund_af": r.get("matriculas_fund_af"),
            "mat_medio": r.get("matriculas_medio"),
        } for r in rows])

    def preview(self, filters: Dict[str, Any], n: int = 15) -> pd.DataFrame:
        from database.supabase_client import db
        rows = db.query_mec_catalog(filters, limit=n, columns="*")
        return self._rows_to_df(rows)[PREVIEW_COLS]

    def points(self, filters: Dict[str, Any], limit: int = 10000) -> pd.DataFrame:
        from database.supabase_client import db
        rows = db.query_mec_catalog(filters, limit=limit, columns="*")
        return self._rows_to_df(rows)

    def import_filtered(self, filters: Dict[str, Any], limit: int = 0) -> Dict[str, Any]:
        from database.supabase_client import db
        return db.import_mec_filtered(filters, limit=limit, source="dashboard_online")


def get_mec_source() -> Optional[MecSource]:
    """CSV local se existir; senao o catalogo Supabase; None se nenhum disponivel."""
    df = load_csv()
    if df is not None:
        return CsvMecSource(df)
    try:
        from database.supabase_client import db
        if db.catalog_available():
            return CatalogMecSource()
    except Exception:
        pass
    return None
