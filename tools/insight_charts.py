"""Gerador de graficos de insight para emails de prospecçao.

Gera PNGs otimizados para email (600x400px, fundo branco, fontes grandes)
com dados personalizados por escola vs benchmarks. Tres tipos de grafico:

1. Radar ENEM — 5 areas do ENEM, escola vs municipio/estado
2. Gap indicator — card visual com gap numerico na area mais fraca
3. Trend chart — evolucao de matriculas ou docentes vs benchmark

Cada funcao retorna bytes PNG (ou None se dados insuficientes).
Upload para Supabase Storage e feito pelo caller (supabase_client.upload_chart).

Requer: pip install kaleido (export estatico do Plotly)

Usage:
    from tools.insight_charts import generate_radar_chart, generate_gap_indicator, generate_trend_chart

    png_bytes = generate_radar_chart("43238203", benchmark="municipio")
    if png_bytes:
        url = db.upload_chart("43238203/radar_20260412.png", png_bytes)
"""
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import plotly.graph_objects as go
except ImportError:
    go = None  # type: ignore

from utils.logger import logger


# ============================================================================
# PATHS DETERMINISTICOS no bucket insight-charts (overwrite-latest)
# Permitem o Streamlit Cloud REFERENCIAR a URL do chart pre-gerado fora do
# Cloud (kaleido nao roda no Cloud) sem regenerar. Ver FixB do fluxo OPR/email.
# ============================================================================
CHART_BUCKET = "insight-charts"
_CHART_FILENAMES = {"radar": "radar.png", "gap": "gap.png", "trend": "trend_mat.png"}


def charts_renderable() -> bool:
    """True se este ambiente pode RENDERIZAR graficos/OPR (kaleido funciona).

    No Streamlit Cloud kaleido falha; queremos que o Cloud apenas CONSUMA
    artefatos pre-gerados (fora do Cloud). Robusto (nao depende do secret estar
    perfeito):
      1. flag RENDER_CHARTS em os.environ OU st.secrets (aceita string E boolean);
      2. fallback: AUTO-DETECTA o Streamlit Cloud (monta o repo em /mount/src)
         -> retorna False mesmo sem o secret.
    Local/Oracle (sem flag e sem /mount/src) -> True (renderiza)."""
    _v = os.getenv("RENDER_CHARTS")
    if _v is None:
        try:
            import streamlit as st  # so existe no contexto do dashboard
            if "RENDER_CHARTS" in st.secrets:
                _v = st.secrets["RENDER_CHARTS"]  # pode ser bool TOML
        except Exception:
            _v = None
    if _v is not None:
        return str(_v).strip().lower() not in ("false", "0", "no", "off")
    # Sem flag: auto-detecta o Streamlit Community Cloud (repo em /mount/src)
    try:
        if os.path.isdir("/mount/src"):
            return False
    except Exception:
        pass
    return True


def chart_storage_path(inep: str, chart_type: str) -> str:
    """Path deterministico do PNG no bucket (ex: '22144714/radar.png')."""
    return f"{inep}/{_CHART_FILENAMES.get(chart_type, chart_type + '.png')}"


def chart_public_url(inep: str, chart_type: str, check_exists: bool = True) -> Optional[str]:
    """URL publica deterministica do chart pre-gerado. Retorna None se
    check_exists e o arquivo nao existe no Storage (evita <img> quebrada)."""
    base = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not base or not inep:
        return None
    fname = _CHART_FILENAMES.get(chart_type)
    if not fname:
        return None
    if check_exists:
        try:
            from database.supabase_client import db
            items = db.client.storage.from_(CHART_BUCKET).list(str(inep)) or []
            if fname not in {it.get("name") for it in items}:
                return None
        except Exception:
            return None
    return f"{base}/storage/v1/object/public/{CHART_BUCKET}/{chart_storage_path(inep, chart_type)}"


# ============================================================================
# CONSTANTES DE DESIGN (otimizado para email)
# ============================================================================

# Cores
COLOR_SCHOOL = "#2563EB"        # Azul forte (escola)
COLOR_SCHOOL_FILL = "rgba(37, 99, 235, 0.15)"
COLOR_BENCHMARK = "#6B7280"     # Cinza mais escuro (benchmark — mais visível)
COLOR_BENCHMARK_FILL = "rgba(107, 114, 128, 0.10)"
COLOR_GAP_NEGATIVE = "#EF4444"  # Vermelho (gap negativo)
COLOR_GAP_POSITIVE = "#10B981"  # Verde (gap positivo)
COLOR_TREND_SCHOOL = "#2563EB"
COLOR_TREND_BENCH = "#9CA3AF"

# Labels PT-BR para as 5 areas do ENEM
AREA_LABELS = {
    "enem_media_mt": "Matemática",
    "enem_media_cn": "Ciências\nda Natureza",
    "enem_media_ch": "Ciências\nHumanas",
    "enem_media_lc": "Linguagens",
    "enem_media_redacao": "Redação",
}
AREA_KEYS = list(AREA_LABELS.keys())

# Labels PT-BR para as 5 competencias da redacao
COMP_LABELS = {
    "enem_redacao_comp1_media": "C1: Norma\nCulta",
    "enem_redacao_comp2_media": "C2: Compreensão\ndo Tema",
    "enem_redacao_comp3_media": "C3: Argumentação",
    "enem_redacao_comp4_media": "C4: Coesão\nTextual",
    "enem_redacao_comp5_media": "C5: Proposta\nde Intervenção",
}
COMP_KEYS = list(COMP_LABELS.keys())

# Dimensoes (px)
RADAR_WIDTH = 600
RADAR_HEIGHT = 420
GAP_WIDTH = 500
GAP_HEIGHT = 200
TREND_WIDTH = 600
TREND_HEIGHT = 320
EXPORT_SCALE = 2  # 2x para retina


# ============================================================================
# HELPERS INTERNOS
# ============================================================================

def _get_db():
    """Lazy import do database client."""
    from database.supabase_client import db
    return db


def _fetch_school_data(inep: str) -> Optional[Dict[str, Any]]:
    """Busca dados da escola em school_analytics."""
    db = _get_db()
    try:
        fields = ",".join(AREA_KEYS + COMP_KEYS + [
            "inep_code", "enem_dependencia", "enem_gap_vs_peer_2025", "enem_ano",
            "enem_area_mais_fraca", "enem_potencial_melhoria",
            "enem_amostra_confiavel", "enem_presentes",
            "peer_mun_nome", "peer_uf_sigla",
            "enem_rank_mun", "enem_rank_uf", "enem_rank_br",
            "enem_percentil_uf_dep", "enem_quartil_br",
        ])
        r = db.client.table("school_analytics").select(fields).eq(
            "inep_code", str(inep).strip()
        ).limit(1).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.warning(f"insight_charts: fetch school failed: {e}")
        return None


# Minimo de escolas no benchmark p/ a comparacao (radar/cards/gap) ser util.
# Abaixo disso, fazemos fallback de escopo (municipio -> UF -> Brasil).
_MIN_BENCH = 3


_BENCH_PAGE = 1000  # PostgREST clampa .limit() nesse teto


def _bench_query(
    municipio: str, uf: str, dependencia: str, metrics: List[str],
    excluir_inep: str = "", enem_ano: Optional[int] = None,
) -> Tuple[Dict[str, Optional[float]], int]:
    """Uma consulta de benchmark (SEM fallback): media por metrica + contagem.

    Cuidados (auditoria Ago/2026):
    - EXCLUI a propria escola do benchmark (`excluir_inep`). Numa cidade com
      3-5 privadas, a escola pesava 20-33% do "grupo de pares" e o gap vs peers
      ficava artificialmente perto de zero.
    - Filtra pela MESMA safra ENEM (nao misturar 2024 com 2025 na mesma media).
    - PAGINA os resultados: o PostgREST clampa .limit() em 1000 e havia recorte
      (ex.: SP/Estadual = 3.152 escolas) virando amostra arbitraria de 1/3.
    - Municipio com match EXATO (o ilike '%x%' fazia "Santa Maria" puxar
      "Santa Maria do Herval").
    """
    db = _get_db()
    fields = ",".join(metrics + ["inep_code"])

    def _base():
        q = db.client.table("school_analytics").select(fields).eq(
            "enem_amostra_confiavel", True
        )
        if municipio:
            q = q.eq("peer_mun_nome", municipio)
        if uf:
            q = q.eq("peer_uf_sigla", uf.upper())
        if dependencia:
            q = q.eq("enem_dependencia", dependencia)
        if enem_ano:
            q = q.eq("enem_ano", enem_ano)
        if excluir_inep:
            q = q.neq("inep_code", str(excluir_inep))
        return q

    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = (_base().range(offset, offset + _BENCH_PAGE - 1).execute().data) or []
        rows.extend(page)
        if len(page) < _BENCH_PAGE or len(rows) >= 25000:
            break
        offset += _BENCH_PAGE

    if not rows:
        return {}, 0
    result: Dict[str, Optional[float]] = {}
    for m in metrics:
        vals = [float(row[m]) for row in rows if row.get(m) is not None]
        result[m] = round(sum(vals) / len(vals), 2) if vals else None
    return result, len(rows)


def _fetch_benchmark(
    municipio: str, uf: str, dependencia: str, metrics: List[str],
    excluir_inep: str = "", enem_ano: Optional[int] = None,
) -> Tuple[Dict[str, Optional[float]], int, str]:
    """Benchmark (medias por metrica) da MESMA dependencia, com FALLBACK de
    escopo quando o nivel pedido tem poucas escolas (< _MIN_BENCH):
    municipio -> UF -> Brasil.

    Conserta OPRs/radares vazios em cidades pequenas: ex. Veranopolis tem so 2
    privadas confiaveis (< 3) -> antes o radar e os cards do OPR sumiam; agora
    cai pro RS (262 privadas) e a comparacao aparece. So aciona quando o nivel
    pedido e escasso — cidades grandes (Teresina 51 privadas) seguem por cidade.

    Returns:
        (medias, contagem, escopo) — escopo in {"municipio","estado","brasil"},
        p/ a caption refletir o nivel REALMENTE usado.
    """
    try:
        levels: List[Tuple[str, str, str]] = []
        if municipio:
            levels.append(("municipio", municipio, uf))
        if uf:
            levels.append(("estado", "", uf))
        levels.append(("brasil", "", ""))
        best: Tuple[Dict[str, Optional[float]], int, str] = ({}, 0, levels[0][0])
        for scope, mun, uff in levels:
            data, count = _bench_query(
                mun, uff, dependencia, metrics,
                excluir_inep=excluir_inep, enem_ano=enem_ano,
            )
            if count >= _MIN_BENCH:
                return data, count, scope
            if count > best[1]:
                best = (data, count, scope)
        return best
    except Exception as e:
        logger.warning(f"insight_charts: fetch benchmark failed: {e}")
        return {}, 0, "municipio"


def _resolve_school_name(inep: str) -> str:
    """Resolve nome da escola via companies ou school_censo_yearly."""
    try:
        from agent.tools.enem_tools import _resolve_school_names
        names = _resolve_school_names([inep])
        return names.get(inep, {}).get("name") or f"INEP {inep}"
    except Exception:
        return f"INEP {inep}"


# kaleido (engine do fig.to_image) NAO e thread-safe: usa um subprocesso Chromium
# unico e compartilhado. Renders concorrentes (ex: as 4 variantes do OPR geradas
# em ThreadPoolExecutor em report_generator) disputam o stdio do subprocesso e
# so um sobrevive — os outros voltam None. Este lock SERIALIZA todos os renders
# (os fetches de dados continuam paralelos; so a conversao p/ PNG e serial).
_RENDER_LOCK = threading.Lock()


def _to_png(fig: "go.Figure", width: int, height: int, scale: Optional[float] = None) -> Optional[bytes]:
    """Exporta Plotly figure para PNG bytes.

    Args:
        scale: Override do EXPORT_SCALE global. Use 1 para PNGs menores
            (~3-4x mais leves), util quando embute multiplos PNGs no mesmo HTML.
    """
    if go is None:
        return None
    try:
        with _RENDER_LOCK:  # kaleido nao e thread-safe — serializa o render
            return fig.to_image(
                format="png", width=width, height=height,
                scale=scale if scale is not None else EXPORT_SCALE,
            )
    except Exception as e:
        logger.warning(f"insight_charts: to_image failed: {e}")
        return None


# ============================================================================
# GERADOR 1: RADAR ENEM (5 areas)
# ============================================================================

def generate_radar_chart(
    inep: str,
    benchmark: str = "municipio",
    include_comps: bool = False,
    benchmark_dep: Optional[str] = None,
    scale: Optional[float] = None,
) -> Optional[bytes]:
    """Gera radar ENEM 5 areas (+ opcionalmente 5 competencias redacao).

    Args:
        inep: Codigo INEP da escola.
        benchmark: "municipio" (default), "estado" ou "brasil".
        include_comps: Se True, gera segundo radar com competencias de redacao.
        benchmark_dep: Dependencia forçada para benchmark (ex: "Privada").
            Se None, usa a mesma dependencia da escola.

    Returns:
        PNG bytes ou None se dados insuficientes.
    """
    if go is None:
        return None

    school = _fetch_school_data(inep)
    if not school or school.get("enem_amostra_confiavel") is not True:
        return None

    nome = _resolve_school_name(inep)
    municipio = school.get("peer_mun_nome") or ""
    uf = school.get("peer_uf_sigla") or ""
    dep = school.get("enem_dependencia") or ""

    # Metricas da escola
    metrics = AREA_KEYS
    labels_map = AREA_LABELS
    school_vals = [school.get(m) for m in metrics]
    if not any(v is not None for v in school_vals):
        return None

    # Benchmark (permite forçar dependência diferente).
    # excluir_inep: a propria escola NAO entra no seu grupo de pares.
    # enem_ano: comparar so com a MESMA safra do ENEM.
    bench_dep = benchmark_dep if benchmark_dep else dep
    bench_data, bench_count, bench_scope = _fetch_benchmark(
        municipio if benchmark == "municipio" else "",
        uf if benchmark in ("municipio", "estado") else "",
        bench_dep,
        metrics,
        excluir_inep=str(inep),
        enem_ano=school.get("enem_ano"),
    )
    bench_vals = [bench_data.get(m) for m in metrics]

    # Labels
    labels = [labels_map[m] for m in metrics]

    # Fechar poligono
    s_vals = [float(v) if v is not None else 0 for v in school_vals]
    b_vals = [float(v) if v is not None else 0 for v in bench_vals]
    s_closed = s_vals + [s_vals[0]]
    b_closed = b_vals + [b_vals[0]]
    l_closed = labels + [labels[0]]

    # Benchmark label (sem jargao "peer", com contagem de escolas)
    # Pluralizar: Federal→federais, Privada→privadas, etc.
    _dep_plural_map = {"Federal": "federais", "Estadual": "estaduais", "Municipal": "municipais", "Privada": "privadas"}
    bench_dep_plural = _dep_plural_map.get(bench_dep, bench_dep.lower() + "s" if bench_dep else "")
    # Escopo REAL usado (pode ter caido de municipio p/ UF/Brasil no fallback)
    if bench_scope == "municipio":
        bench_label = f"Média de {bench_count} escolas {bench_dep_plural} de {municipio}"
    elif bench_scope == "estado":
        bench_label = f"Média de {bench_count} escolas {bench_dep_plural} do {uf}"
    else:
        bench_label = f"Média de {bench_count} escolas {bench_dep_plural} no Brasil"

    # Construir figura
    fig = go.Figure()

    # Escola trace PRIMEIRO (azul, fica atras quando menor)
    fig.add_trace(go.Scatterpolar(
        r=s_closed,
        theta=l_closed,
        fill="toself",
        name=nome[:40],
        line=dict(color=COLOR_SCHOOL, width=3),
        fillcolor=COLOR_SCHOOL_FILL,
        opacity=0.85,
    ))

    # Benchmark trace POR CIMA (cinza pontilhado, mais visivel)
    if any(v > 0 for v in b_closed):
        fig.add_trace(go.Scatterpolar(
            r=b_closed,
            theta=l_closed,
            fill="none",
            name=bench_label[:50],
            line=dict(color=COLOR_BENCHMARK, width=3, dash="dot"),
            opacity=0.9,
        ))

    # Range
    all_vals = [v for v in s_vals + b_vals if v > 0]
    max_val = max(all_vals) * 1.1 if all_vals else 800

    fig.update_layout(
        polar=dict(
            radialaxis=dict(
                visible=True,
                range=[0, max_val],
                tickfont=dict(size=10, color="#999"),
                gridcolor="#E5E7EB",
            ),
            angularaxis=dict(
                tickfont=dict(size=12, color="#333"),
                gridcolor="#E5E7EB",
            ),
            bgcolor="white",
        ),
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.15,
            xanchor="center",
            x=0.5,
            font=dict(size=10),
        ),
        title=dict(
            text=f"Performance ENEM {int(school.get('enem_ano') or 2025)}<br><span style='font-size:11px;color:#666'>"
                 f"{nome[:45]} vs {bench_label[:55]}</span>",
            font=dict(size=14, color="#333"),
            x=0.5,
            xanchor="center",
        ),
        paper_bgcolor="white",
        plot_bgcolor="white",
        margin=dict(l=70, r=70, t=75, b=65),
        height=RADAR_HEIGHT,
        width=RADAR_WIDTH,
    )

    return _to_png(fig, RADAR_WIDTH, RADAR_HEIGHT, scale=scale)


# ============================================================================
# GERADOR 2: GAP INDICATOR
# ============================================================================

def generate_gap_indicator(inep: str) -> Optional[bytes]:
    """Gera card visual com o gap numerico na area mais fraca.

    Returns:
        PNG bytes ou None se sem gap significativo.
    """
    if go is None:
        return None

    school = _fetch_school_data(inep)
    if not school:
        return None

    gap = school.get("enem_gap_vs_peer_2025")
    area_fraca = school.get("enem_area_mais_fraca")
    if gap is None or area_fraca is None:
        return None

    gap_val = float(gap)
    if abs(gap_val) < 3:
        return None  # Gap insignificante

    nome = _resolve_school_name(inep)
    color = COLOR_GAP_NEGATIVE if gap_val < 0 else COLOR_GAP_POSITIVE
    icon = "▼" if gap_val < 0 else "▲"
    label = "abaixo" if gap_val < 0 else "acima"

    fig = go.Figure()

    # Card como anotacao (Plotly nao tem cards nativos)
    fig.add_annotation(
        text=f"<b style='font-size:48px;color:{color}'>{icon} {abs(gap_val):.0f} pts</b>",
        xref="paper", yref="paper",
        x=0.5, y=0.65,
        showarrow=False,
        font=dict(size=48, color=color),
    )
    fig.add_annotation(
        text=f"<b>{label} das escolas similares</b> em <b>{area_fraca}</b>",
        xref="paper", yref="paper",
        x=0.5, y=0.3,
        showarrow=False,
        font=dict(size=16, color="#555"),
    )
    fig.add_annotation(
        text=f"{nome[:45]}",
        xref="paper", yref="paper",
        x=0.5, y=0.08,
        showarrow=False,
        font=dict(size=11, color="#999"),
    )

    fig.update_layout(
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        paper_bgcolor="white",
        plot_bgcolor="white",
        margin=dict(l=20, r=20, t=20, b=20),
        height=GAP_HEIGHT,
        width=GAP_WIDTH,
    )

    return _to_png(fig, GAP_WIDTH, GAP_HEIGHT)


# ============================================================================
# GERADOR 3: TREND CHART (serie temporal)
# ============================================================================

# Memo do benchmark por (cidade, metrica). O valor e o mesmo para TODA escola
# da cidade — sem isto, um refresh de 131 escolas em 11 cidades recalcula 786
# vezes o que sao 66 numeros distintos.
#
# Escopo: vale dentro de um processo. O job noturno isola cada escola num
# subprocesso, entao la quem paga a conta e o indice coberto da migration 025;
# aqui o ganho e no dashboard e no IAlex, que vivem por horas.
_BENCH_CACHE: dict = {}


def _benchmark_municipio(db, city: str, years: list, metric: str) -> dict:
    """Media da metrica por ano no municipio: {ano: (media, n_escolas)}.

    Era um loop de uma consulta POR ANO, cada uma lendo todas as escolas da
    cidade daquela safra. Medido no pg_stat_statements: 14.810 chamadas,
    876 blocos de disco cada, ~99 GB — a consulta que mais consumia I/O do
    projeto inteiro. Agora e uma consulta so, com os anos num IN, e memoizada.
    """
    chave = (str(city or ""), str(metric), tuple(sorted(years)))
    if chave in _BENCH_CACHE:
        return _BENCH_CACHE[chave]

    out: dict = {}
    if city and years:
        try:
            r = (db.client.table("school_censo_yearly")
                 .select(f"vintage_censo,{metric}")
                 .in_("vintage_censo", list(years))
                 .eq("city", city)
                 .execute())
            por_ano: dict = {}
            for row in (r.data or []):
                v = row.get(metric)
                if v is None:
                    continue
                por_ano.setdefault(row.get("vintage_censo"), []).append(float(v))
            for ano, vals in por_ano.items():
                out[ano] = (round(sum(vals) / len(vals), 1), len(vals)) if vals else (None, 0)
        except Exception:
            out = {}
    _BENCH_CACHE[chave] = out
    return out


def generate_trend_chart(
    inep: str,
    metric: str = "qt_mat_bas",
    metric_label: str = "Matrículas Totais",
) -> Optional[bytes]:
    """Gera grafico de linhas: escola vs media do municipio ao longo dos anos.

    Args:
        inep: Codigo INEP.
        metric: Campo do school_censo_yearly (ex: qt_mat_bas, qt_doc_bas).
        metric_label: Label em PT-BR para o eixo Y.

    Returns:
        PNG bytes ou None se dados insuficientes.
    """
    if go is None:
        return None

    db = _get_db()
    nome = _resolve_school_name(inep)

    # Buscar serie da escola
    try:
        r = db.client.table("school_censo_yearly").select(
            f"vintage_censo,{metric},city,state"
        ).eq("inep_code", str(inep).strip()).order("vintage_censo").execute()
        school_series = r.data or []
    except Exception:
        return None

    # Filtrar anos com dados NULL (ex: Censo 2025 ainda nao publicado)
    school_series = [row for row in school_series if row.get(metric) is not None]

    if len(school_series) < 2:
        return None

    years = [row["vintage_censo"] for row in school_series]
    school_vals = [row.get(metric) for row in school_series]
    city = school_series[0].get("city") or ""
    state = school_series[0].get("state") or ""

    # Buscar benchmark (media do municipio por ano) — UMA consulta pros N anos.
    _bench = _benchmark_municipio(db, city, years, metric)
    bench_vals = [_bench.get(y, (None, 0))[0] for y in years]
    bench_counts = [_bench.get(y, (None, 0))[1] for y in years]

    # Guard: omit benchmark years with unrepresentative sample
    if bench_counts:
        max_count = max(bench_counts) if bench_counts else 0
        for i, count in enumerate(bench_counts):
            if max_count > 0 and count < max_count * 0.3:
                bench_vals[i] = None  # omit this year's benchmark

    fig = go.Figure()

    # Converter para variacao % relativa ao primeiro ano
    # Escola: pegar primeiro valor valido como base (100%)
    s_valid_raw = [(y, v) for y, v in zip(years, school_vals) if v is not None]
    b_valid_raw = [(y, v) for y, v in zip(years, bench_vals) if v is not None]

    # Guard (FixC): re-basear no 1o ano com amostra razoavel (>=20 alunos) e
    # descartar anos anteriores minusculos — senao escola nova/reestruturada (ex:
    # 12 -> 1211 alunos) gera variacao absurda (+9000%). Alinha benchmark ao
    # mesmo ano-base.
    _MIN_BASE = 20
    _bi = next((i for i, (_, v) in enumerate(s_valid_raw) if v and v >= _MIN_BASE), 0)
    if s_valid_raw:
        _base_year = s_valid_raw[_bi][0]
        s_valid_raw = [(y, v) for (y, v) in s_valid_raw if y >= _base_year]
        b_valid_raw = [(y, v) for (y, v) in b_valid_raw if y >= _base_year]

    s_base = s_valid_raw[0][1] if s_valid_raw else 1
    b_base = b_valid_raw[0][1] if b_valid_raw else 1
    s_base = max(s_base, 1)  # evita divisao por zero
    b_base = max(b_base, 1)

    s_valid = [(y, round((v / s_base - 1) * 100, 1)) for y, v in s_valid_raw]
    b_valid = [(y, round((v / b_base - 1) * 100, 1)) for y, v in b_valid_raw]

    # Linha de referencia em 0% (sem mudanca)
    all_years = sorted(set([p[0] for p in s_valid] + [p[0] for p in b_valid]))
    fig.add_trace(go.Scatter(
        x=all_years, y=[0] * len(all_years),
        mode="lines",
        line=dict(color="#E0E0E0", width=1, dash="dash"),
        showlegend=False,
        hoverinfo="skip",
    ))

    # Benchmark (variacao %)
    if b_valid:
        fig.add_trace(go.Scatter(
            x=[p[0] for p in b_valid],
            y=[p[1] for p in b_valid],
            mode="lines+markers+text",
            name=f"Média de {city}",
            line=dict(color=COLOR_TREND_BENCH, width=2, dash="dot"),
            marker=dict(size=6, color=COLOR_TREND_BENCH),
            text=[f"{p[1]:+.0f}%" for p in b_valid],
            textposition="bottom center",
            textfont=dict(size=9, color=COLOR_TREND_BENCH),
        ))

    # Escola (variacao %)
    if s_valid:
        fig.add_trace(go.Scatter(
            x=[p[0] for p in s_valid],
            y=[p[1] for p in s_valid],
            mode="lines+markers+text",
            name=nome[:35],
            line=dict(color=COLOR_TREND_SCHOOL, width=3),
            marker=dict(size=8, color=COLOR_TREND_SCHOOL),
            text=[f"{p[1]:+.0f}%" for p in s_valid],
            textposition="top center",
            textfont=dict(size=10, color=COLOR_TREND_SCHOOL),
        ))

    # Range Y com padding
    all_pct = [p[1] for p in s_valid] + [p[1] for p in b_valid]
    if all_pct:
        y_min_val = min(all_pct)
        y_max_val = max(all_pct)
        y_pad = max((y_max_val - y_min_val) * 0.20, 8)
        y_range = [y_min_val - y_pad, y_max_val + y_pad]
    else:
        y_range = None

    # Subtitulo: ABSOLUTO real (serie completa) p/ concretude/confianca + % desde
    # o ano-base. O EIXO segue em % (compara escolas de portes diferentes de forma
    # justa); o numero absoluto vive so no texto.
    def _fmt_int(n):
        try:
            return f"{int(n):,}".replace(",", ".")
        except Exception:
            return str(n)

    _abs_first = _fmt_int(school_vals[0])
    _abs_last = _fmt_int(school_vals[-1])
    _base_year = s_valid_raw[0][0] if s_valid_raw else years[0]
    _growth = s_valid[-1][1] if s_valid else 0

    fig.update_layout(
        title=dict(
            text=f"Crescimento de {metric_label} (%)<br>"
                 f"<span style='font-size:11px;color:#666'>"
                 f"{nome[:35]}: {_abs_first}\u2192{_abs_last} alunos · "
                 f"{_growth:+.0f}% desde {_base_year} · vs média de {city}</span>",
            font=dict(size=14, color="#333"),
            x=0.5, xanchor="center",
        ),
        xaxis=dict(
            dtick=1,
            gridcolor="#F3F4F6",
            tickfont=dict(size=11),
        ),
        yaxis=dict(
            title="Crescimento (%)",
            gridcolor="#F3F4F6",
            tickfont=dict(size=11),
            ticksuffix="%",
            range=y_range,
            zeroline=True,
            zerolinecolor="#E0E0E0",
        ),
        legend=dict(
            orientation="h", yanchor="top", y=-0.25,
            xanchor="center", x=0.5, font=dict(size=10),
        ),
        paper_bgcolor="white",
        plot_bgcolor="white",
        margin=dict(l=60, r=30, t=70, b=75),
        height=TREND_HEIGHT,
        width=TREND_WIDTH,
    )

    return _to_png(fig, TREND_WIDTH, TREND_HEIGHT)


# ============================================================================
# HELPER: Gerar todos os charts relevantes para uma escola
# ============================================================================

def generate_all_relevant_charts(
    inep: str,
    benchmark: str = "municipio",
) -> List[Dict[str, Any]]:
    """Gera todos os graficos relevantes para uma escola.

    Decide automaticamente quais charts gerar baseado nos dados disponiveis.

    Returns:
        Lista de dicts: [{"type": "radar", "bytes": PNG, "filename": "..."}, ...]
    """
    # Paths DETERMINISTICOS (overwrite-latest): permitem o Cloud referenciar a
    # URL sem regenerar (geracao roda fora do Cloud). Ver CHART_FILENAMES.
    charts: List[Dict[str, Any]] = []

    # 1. Radar ENEM (sempre, se tiver amostra confiavel)
    radar = generate_radar_chart(inep, benchmark=benchmark)
    if radar:
        charts.append({
            "type": "radar",
            "bytes": radar,
            "filename": chart_storage_path(inep, "radar"),
            "alt": "Performance ENEM 2025 por area",
        })

    # 2. Gap indicator (se gap significativo)
    gap = generate_gap_indicator(inep)
    if gap:
        charts.append({
            "type": "gap",
            "bytes": gap,
            "filename": chart_storage_path(inep, "gap"),
            "alt": "Diferenca vs escolas similares",
        })

    # 3. Trend de matriculas (se tiver serie historica)
    trend = generate_trend_chart(inep, "qt_mat_bas", "Matrículas Totais")
    if trend:
        charts.append({
            "type": "trend",
            "bytes": trend,
            "filename": chart_storage_path(inep, "trend"),
            "alt": "Evolucao de matriculas",
        })

    return charts


# ============================================================================
# COMPARATIVO: Radar com 2 escolas + benchmark
# ============================================================================

def generate_comparison_radar(
    inep1: str,
    inep2: str,
    benchmark: str = "municipio",
) -> Optional[bytes]:
    """Gera radar ENEM com 2 escolas + benchmark (3 traces).

    Args:
        inep1: INEP da escola-alvo (azul).
        inep2: INEP da escola de referencia (laranja).
        benchmark: "municipio", "estado" ou "brasil".

    Returns:
        PNG bytes ou None.
    """
    if go is None:
        return None

    s1 = _fetch_school_data(inep1)
    s2 = _fetch_school_data(inep2)
    if not s1 or not s2:
        return None
    if s1.get("enem_amostra_confiavel") is not True or s2.get("enem_amostra_confiavel") is not True:
        return None

    nome1 = _resolve_school_name(inep1)
    nome2 = _resolve_school_name(inep2)
    mun = s1.get("peer_mun_nome") or ""
    uf = s1.get("peer_uf_sigla") or ""
    dep = s1.get("enem_dependencia") or ""

    metrics = AREA_KEYS
    labels = [AREA_LABELS[m] for m in metrics]

    v1 = [float(s1.get(m) or 0) for m in metrics]
    v2 = [float(s2.get(m) or 0) for m in metrics]

    # Benchmark
    bench_data, bench_count, _bench_scope = _fetch_benchmark(
        mun if benchmark == "municipio" else "",
        uf if benchmark in ("municipio", "estado") else "",
        dep, metrics,
    )
    vb = [float(bench_data.get(m) or 0) for m in metrics]

    # Fechar poligonos
    v1c = v1 + [v1[0]]
    v2c = v2 + [v2[0]]
    vbc = vb + [vb[0]]
    lc = labels + [labels[0]]

    fig = go.Figure()

    # Escola 1 (azul — alvo)
    fig.add_trace(go.Scatterpolar(
        r=v1c, theta=lc, fill="toself",
        name=nome1[:30], line=dict(color=COLOR_SCHOOL, width=3),
        fillcolor=COLOR_SCHOOL_FILL, opacity=0.85,
    ))

    # Escola 2 (laranja — referencia)
    COLOR_SCHOOL2 = "#F97316"
    COLOR_SCHOOL2_FILL = "rgba(249, 115, 22, 0.12)"
    fig.add_trace(go.Scatterpolar(
        r=v2c, theta=lc, fill="toself",
        name=nome2[:30], line=dict(color=COLOR_SCHOOL2, width=3),
        fillcolor=COLOR_SCHOOL2_FILL, opacity=0.75,
    ))

    # Benchmark (cinza tracejado)
    if any(v > 0 for v in vbc):
        bench_label = f"Media {dep} {mun}" if benchmark == "municipio" else f"Media {dep} {uf}"
        fig.add_trace(go.Scatterpolar(
            r=vbc, theta=lc, fill="none",
            name=bench_label[:40], line=dict(color=COLOR_BENCHMARK, width=2, dash="dot"),
            opacity=0.7,
        ))

    all_vals = [v for v in v1 + v2 + vb if v > 0]
    max_val = max(all_vals) * 1.1 if all_vals else 800

    fig.update_layout(
        polar=dict(
            radialaxis=dict(visible=True, range=[0, max_val],
                            tickfont=dict(size=9, color="#999"), gridcolor="#E5E7EB"),
            angularaxis=dict(tickfont=dict(size=11, color="#333"), gridcolor="#E5E7EB"),
            bgcolor="white",
        ),
        showlegend=True,
        legend=dict(orientation="h", yanchor="top", y=-0.15, xanchor="center", x=0.5, font=dict(size=9)),
        title=dict(
            text=f"Comparativo ENEM {int(s1.get('enem_ano') or 2025)}<br><span style='font-size:11px;color:#666'>"
                 f"{nome1[:25]} vs {nome2[:25]}</span>",
            font=dict(size=14, color="#333"), x=0.5, xanchor="center",
        ),
        paper_bgcolor="white", plot_bgcolor="white",
        margin=dict(l=70, r=70, t=75, b=70),
        height=RADAR_HEIGHT, width=RADAR_WIDTH,
    )

    return _to_png(fig, RADAR_WIDTH, RADAR_HEIGHT)


# ============================================================================
# COMPARATIVO: Trend com 2 escolas
# ============================================================================

def generate_comparison_trend(
    inep1: str,
    inep2: str,
    metric: str = "qt_mat_bas",
    metric_label: str = "Matrículas Totais",
) -> Optional[bytes]:
    """Gera grafico de evolucao comparando 2 escolas (variacao %).

    Args:
        inep1: INEP escola-alvo.
        inep2: INEP escola referencia.
        metric: Coluna de school_censo_yearly.
        metric_label: Label em PT-BR.

    Returns:
        PNG bytes ou None.
    """
    if go is None:
        return None

    db = _get_db()
    nome1 = _resolve_school_name(inep1)
    nome2 = _resolve_school_name(inep2)

    def _fetch_series(inep: str):
        try:
            r = db.client.table("school_censo_yearly").select(
                f"vintage_censo,{metric}"
            ).eq("inep_code", str(inep).strip()).order("vintage_censo").execute()
            rows = [row for row in (r.data or []) if row.get(metric) is not None]
            return [(row["vintage_censo"], float(row[metric])) for row in rows]
        except Exception:
            return []

    s1 = _fetch_series(inep1)
    s2 = _fetch_series(inep2)
    if len(s1) < 2 and len(s2) < 2:
        return None

    fig = go.Figure()

    # Converter para variacao %
    def _to_pct(series):
        if not series:
            return []
        base = max(series[0][1], 1)
        return [(y, round((v / base - 1) * 100, 1)) for y, v in series]

    s1_pct = _to_pct(s1)
    s2_pct = _to_pct(s2)

    # Linha de referencia 0%
    all_years = sorted(set([p[0] for p in s1_pct] + [p[0] for p in s2_pct]))
    fig.add_trace(go.Scatter(
        x=all_years, y=[0] * len(all_years),
        mode="lines", line=dict(color="#E0E0E0", width=1, dash="dash"),
        showlegend=False, hoverinfo="skip",
    ))

    # Escola 1 (azul)
    if s1_pct:
        fig.add_trace(go.Scatter(
            x=[p[0] for p in s1_pct], y=[p[1] for p in s1_pct],
            mode="lines+markers+text", name=nome1[:30],
            line=dict(color=COLOR_TREND_SCHOOL, width=3),
            marker=dict(size=8, color=COLOR_TREND_SCHOOL),
            text=[f"{p[1]:+.0f}%" for p in s1_pct],
            textposition="top center", textfont=dict(size=9, color=COLOR_TREND_SCHOOL),
        ))

    # Escola 2 (laranja)
    COLOR_TREND2 = "#F97316"
    if s2_pct:
        fig.add_trace(go.Scatter(
            x=[p[0] for p in s2_pct], y=[p[1] for p in s2_pct],
            mode="lines+markers+text", name=nome2[:30],
            line=dict(color=COLOR_TREND2, width=3),
            marker=dict(size=8, color=COLOR_TREND2),
            text=[f"{p[1]:+.0f}%" for p in s2_pct],
            textposition="bottom center", textfont=dict(size=9, color=COLOR_TREND2),
        ))

    # Range Y com padding
    all_pct = [p[1] for p in s1_pct] + [p[1] for p in s2_pct]
    if all_pct:
        y_pad = max((max(all_pct) - min(all_pct)) * 0.20, 8)
        y_range = [min(all_pct) - y_pad, max(all_pct) + y_pad]
    else:
        y_range = None

    # Subtitulo com valores absolutos
    sub_parts = []
    if s1:
        sub_parts.append(f"{nome1[:20]}: {int(s1[0][1])}\u2192{int(s1[-1][1])}")
    if s2:
        sub_parts.append(f"{nome2[:20]}: {int(s2[0][1])}\u2192{int(s2[-1][1])}")

    fig.update_layout(
        title=dict(
            text=f"Variação de {metric_label}<br>"
                 f"<span style='font-size:10px;color:#666'>{' | '.join(sub_parts)}</span>",
            font=dict(size=13, color="#333"), x=0.5, xanchor="center",
        ),
        xaxis=dict(dtick=1, gridcolor="#F3F4F6", tickfont=dict(size=11)),
        yaxis=dict(title="Variação %", gridcolor="#F3F4F6", tickfont=dict(size=11),
                   ticksuffix="%", range=y_range, zeroline=True, zerolinecolor="#E0E0E0"),
        legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="center", x=0.5, font=dict(size=10)),
        paper_bgcolor="white", plot_bgcolor="white",
        margin=dict(l=60, r=30, t=70, b=75),
        height=TREND_HEIGHT, width=TREND_WIDTH,
    )

    return _to_png(fig, TREND_WIDTH, TREND_HEIGHT)


# ============================================================================
# HELPER: Benchmark por lista de INEPs (para grupo customizado)
# ============================================================================

def fetch_benchmark_by_ineps(
    inep_list: List[str],
    metrics: Optional[List[str]] = None,
) -> Tuple[Dict[str, Optional[float]], int]:
    """Calcula media de metricas para um grupo customizado de escolas.

    Args:
        inep_list: Lista de codigos INEP.
        metrics: Metricas a calcular (default: AREA_KEYS).

    Returns:
        Tuple de (dict de medias, contagem de escolas com dados).
    """
    if metrics is None:
        metrics = list(AREA_KEYS)
    db = _get_db()
    try:
        fields = ",".join(metrics + ["inep_code"])
        r = db.client.table("school_analytics").select(fields).eq(
            "enem_amostra_confiavel", True
        ).in_("inep_code", [str(i).strip() for i in inep_list]).execute()
        rows = r.data or []
        if not rows:
            return {}, 0
        result = {}
        for m in metrics:
            vals = [float(row[m]) for row in rows if row.get(m) is not None]
            result[m] = round(sum(vals) / len(vals), 2) if vals else None
        return result, len(rows)
    except Exception as e:
        logger.warning(f"fetch_benchmark_by_ineps failed: {e}")
        return {}, 0
