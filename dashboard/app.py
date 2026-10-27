"""IAprendo Sales Agent — Home "Hoje" (redesign v2 F2).

A pagina responde: "o que eu faco agora?" — agenda do dia + numeros que importam
+ meta. Logica em dashboard/helpers/home_v2.py (testavel); aqui so render.
"""
import os
import streamlit as st
import sys
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.theme import (
    apply_theme_no_config, metric_card, section_header, alert_banner, COLORS,
    activity_row, goal_progress, priority_badge, stage_pill, empty_state,
    ACTIVITY_CSS,
)

# set_page_config + auth + sidebar de identidade rodam no entrypoint
# (dashboard/main.py com st.navigation). Aqui: so tema e conteudo.
apply_theme_no_config()
st.markdown(ACTIVITY_CSS, unsafe_allow_html=True)

from dashboard._auth_gate import require_auth
require_auth()  # idempotente: o gate central do main.py ja autenticou

_current_user = st.session_state.get("_v2_current_user") or {
    "name": st.session_state.get("name", "?"),
    "email": "", "role": "",
}

# =========================================================================
# DADOS (helpers testaveis) + ENGINE no load (cache 5min — SPEC §0)
# =========================================================================
from database.supabase_client import db
from dashboard.helpers import home_v2 as hv
from dashboard.labels import goal_metric_label
from utils.sender_profile import is_admin as _is_admin_fn
from workflows.activity_engine import parse_ts, to_brt, now_utc, all_usernames

_username = st.session_state.get("username", "fernando")
_first_name = (_current_user.get("name") or _username).split()[0]
_admin = bool(_is_admin_fn(_username))


@st.cache_data(ttl=1800, show_spinner=False)  # era 300; reduz carga no banco (27/10/2026)
def _engine_tick() -> dict:
    """Roda o motor da agenda no load (idempotente; cobre PC local desligado)."""
    try:
        from workflows.activity_engine import run_engine
        return run_engine()
    except Exception:
        return {}


_engine_tick()
_now = now_utc()

# =========================================================================
# HEADER + BUSCA GLOBAL ACIONAVEL
# =========================================================================
_hora = to_brt(_now).hour
_saud = "Bom dia" if _hora < 12 else ("Boa tarde" if _hora < 18 else "Boa noite")
try:
    from utils.date_pt import format_pt
    _data_pt = format_pt(to_brt(_now), "%A, %d de %B")
except Exception:
    _data_pt = to_brt(_now).strftime("%d/%m/%Y")

st.markdown(
    f'<h1 style="margin-bottom:0">{_saud}, {_first_name}! ☀️</h1>'
    f'<p style="color:#757575;margin-top:4px;font-size:15px">{_data_pt} — '
    f'sua lista do dia esta pronta</p>',
    unsafe_allow_html=True,
)

_q = st.text_input("Busca", placeholder="Buscar escola, contato ou e-mail…",
                   key="global_search", label_visibility="collapsed")
if _q and len(_q.strip()) >= 2:
    _res = hv.busca_global(_q.strip())
    if not _res["escolas"] and not _res["contatos"]:
        st.caption("Nada encontrado.")
    for _e in _res["escolas"]:
        _c1, _c2, _c3 = st.columns([5, 3, 1.4])
        _c1.markdown(f"**🏫 {_e.get('name')}** — {_e.get('city') or '?'}")
        _c2.markdown(stage_pill(_e.get("status"), _e.get("commercial_stage")),
                     unsafe_allow_html=True)
        if _c3.button("Abrir →", key=f"open_{_e['id']}"):
            # Sem setar o id, a pagina Escolas abria a ULTIMA ficha vista
            # (escola_detail_id persiste entre paginas) — ou a lista generica.
            st.session_state["escola_detail_id"] = _e["id"]
            st.switch_page("pages/2_🏫_Escolas.py")
    for _ct in _res["contatos"]:
        st.markdown(f"👤 **{_ct.get('full_name')}** — {_ct.get('role') or ''} · "
                    f"{_ct.get('email') or 'sem e-mail'}")

# =========================================================================
# 3 NUMEROS DO DIA (AO VIVO, com alerta de SLA)
# =========================================================================
_nums = hv.day_numbers(_username, _now)
_n1, _n2, _n3 = st.columns(3)
with _n1:
    _delta = (f"{_nums['atrasadas']} atrasadas" if _nums["atrasadas"] else "em dia")
    metric_card("Minha agenda (hoje)", _nums["atividades_hoje"],
                COLORS["error"] if _nums["atrasadas"] else COLORS["primary"],
                delta=_delta, icon="event_available")
with _n2:
    _delta2 = (f"{_nums['aprovacoes_aging']} paradas ha +24h"
               if _nums["aprovacoes_aging"] else "fila saudavel")
    metric_card("Aguardando aprovacao", _nums["aprovacoes_pendentes"],
                COLORS["warning"] if _nums["aprovacoes_aging"] else COLORS["secondary"],
                delta=_delta2, icon="mark_email_unread")
    st.page_link("pages/6_✉️_Comunicacao.py", label="abrir mensagens →")
with _n3:
    _sla_ok = _nums["resposta_mais_antiga_h"] <= 4
    _delta3 = ("nenhuma esperando" if not _nums["respostas_novas"] else
               (f"esperando ha {_nums['resposta_mais_antiga_h']:.0f}h ⚠️"
                if not _sla_ok else "dentro do SLA (4h)"))
    metric_card("Respostas a tratar", _nums["respostas_novas"],
                COLORS["error"] if (_nums["respostas_novas"] and not _sla_ok)
                else COLORS["success"], delta=_delta3, icon="forum")

_conv = hv.em_conversa(_username)
try:
    from integrations.agenda_config import agenda_config
    _teto = int(agenda_config.get_config().get("teto_em_conversa", 15))
except Exception:
    _teto = 15
_conv_msg = f"💬 Em conversa: **{_conv}/{_teto}** leads ativos"
if _conv > _teto:
    _conv_msg += " — acima do teto de foco: feche antes de abrir novos"
st.caption(_conv_msg)
if _nums.get("sobrecarga"):
    alert_banner("Dia cheio (12+ atividades) — ataque so as de prioridade maxima 🔴.",
                 "warning")

# =========================================================================
# TOGGLE MINHA AGENDA / EQUIPE (gestor)
# =========================================================================
_view = "Minha agenda"
if _admin:
    try:
        _view = st.segmented_control(
            "Visao", ["Minha agenda", "Equipe"], default="Minha agenda",
            label_visibility="collapsed") or "Minha agenda"
    except Exception:  # fallback p/ versoes sem segmented_control
        _view = st.radio("Visao", ["Minha agenda", "Equipe"], horizontal=True,
                         label_visibility="collapsed")

st.markdown('<hr class="divider">', unsafe_allow_html=True)


# =========================================================================
# DIALOG: + Nova atividade
# =========================================================================
@st.dialog("Nova atividade")
def _dlg_nova_atividade():
    _t = st.text_input("O que fazer?", placeholder="ex: Ligar pro Colegio Alfa")
    _dc1, _dc2 = st.columns(2)
    _d = _dc1.date_input("Quando", value=to_brt(_now).date() + timedelta(days=1))
    _h = _dc2.time_input("Hora", value=datetime.strptime("09:00", "%H:%M").time())
    _p = st.selectbox("Prioridade", [2, 1, 3],
                      format_func=lambda x: {1: "1 — alta", 2: "2 — normal",
                                             3: "3 — baixa"}[x])
    _esc = st.text_input("Escola (opcional)", placeholder="nome da escola no CRM")
    if st.button("Salvar", type="primary"):
        if not _t.strip():
            st.error("Informe o titulo.")
            return
        _cid = None
        if _esc.strip():
            try:
                _rows = db.client.table("companies").select("id").ilike(
                    "name", f"%{_esc.strip()}%").limit(1).execute().data or []
                _cid = _rows[0]["id"] if _rows else None
            except Exception:
                pass
        _due = datetime.combine(_d, _h).replace(tzinfo=to_brt(_now).tzinfo)
        db.create_activity({
            "owner_username": _username, "type": "tarefa",
            "title": _t.strip()[:300], "due_at": _due.isoformat(),
            "priority": int(_p), "source": "manual",
            "created_by": _username, "company_id": _cid,
        })
        st.rerun()


def _when_txt(a: dict, overdue: bool) -> str:
    """'vence 14h30' / 'venceu ha 2d' (visual do mockup)."""
    _due = parse_ts(a.get("due_at"))
    if not _due:
        return ""
    if overdue:
        _delta = _now - _due
        if _delta.days >= 1:
            return f"venceu ha {_delta.days}d"
        _h = int(_delta.total_seconds() // 3600)
        return f"venceu ha {_h}h" if _h else "venceu agora"
    _local = to_brt(_due)
    if _local.date() == to_brt(_now).date():
        return f"vence {_local.strftime('%Hh%M')}"
    return f"vence {_local.strftime('%d/%m %Hh%M')}"


def _render_activity(a: dict, overdue: bool):
    """Linha da agenda com ✓ / ⏰ / → (concluir-adiar em <=2 cliques)."""
    _c0, _c1, _c2, _c3 = st.columns([8, 0.8, 0.8, 0.8])
    with _c0:
        st.markdown(activity_row(a, overdue=overdue,
                                 when_txt=_when_txt(a, overdue)),
                    unsafe_allow_html=True)
    if _c1.button("✓", key=f"done_{a['id']}", help="Concluir"):
        db.complete_activity(a["id"], _username, "manual")
        st.rerun()
    with _c2.popover("⏰", help="Adiar"):
        if st.button("+2 horas", key=f"snz2h_{a['id']}"):
            _r = db.snooze_activity(a["id"], (_now + timedelta(hours=2)).isoformat())
            st.error(_r.get("erro")) if not _r.get("ok") else st.rerun()
        if st.button("Amanha 9h", key=f"snzam_{a['id']}"):
            _b = to_brt(_now).date() + timedelta(days=1)
            _until = datetime.combine(_b, datetime.min.time().replace(hour=9),
                                      tzinfo=to_brt(_now).tzinfo)
            _r = db.snooze_activity(a["id"], _until.isoformat())
            st.error(_r.get("erro")) if not _r.get("ok") else st.rerun()
        if st.button("Segunda 9h", key=f"snzseg_{a['id']}"):
            _b = to_brt(_now).date() + timedelta(days=1)
            while _b.weekday() != 0:
                _b += timedelta(days=1)
            _until = datetime.combine(_b, datetime.min.time().replace(hour=9),
                                      tzinfo=to_brt(_now).tzinfo)
            _r = db.snooze_activity(a["id"], _until.isoformat())
            st.error(_r.get("erro")) if not _r.get("ok") else st.rerun()
    if a.get("company_id"):
        if _c3.button("→", key=f"go_{a['id']}", help="Abrir escola"):
            # O company_id ja estava disponivel aqui e era descartado
            st.session_state["escola_detail_id"] = a["company_id"]
            st.switch_page("pages/2_🏫_Escolas.py")


# =========================================================================
# CONTEUDO: PAINEL EQUIPE (gestor) ou MINHA AGENDA + LATERAL
# =========================================================================
if _view == "Equipe" and _admin:
    section_header("Equipe — semana e gargalos", "groups")
    _panel = hv.team_panel(all_usernames(), _now)
    _vend_cols = st.columns(max(1, len(_panel["por_vendedor"])))
    for _i, (_u, _d) in enumerate(_panel["por_vendedor"].items()):
        with _vend_cols[_i % len(_vend_cols)]:
            _alert = (f'<span style="color:#C62828;font-weight:700">'
                      f'{_d["respostas_atrasadas"]} resposta(s) atrasada(s)!</span><br/>'
                      if _d["respostas_atrasadas"] else "")
            st.markdown(
                f'<div class="data-card"><strong>{_u.capitalize()}</strong><br/>'
                f'{_alert}{_d["atrasadas"]} atrasadas · {_d["hoje"]} hoje</div>',
                unsafe_allow_html=True)

    _cl, _cr = st.columns(2)
    with _cl:
        section_header("Leads sem dono", "person_off")
        if not _panel["sem_dono"]:
            st.caption("Nenhum — todo lead tem dono. ✓")
        for _l in _panel["sem_dono"][:8]:
            _x1, _x2, _x3 = st.columns([4, 2, 1.6])
            _x1.markdown(f"**{_l.get('name')}** — {_l.get('city') or ''}")
            _novo = _x2.selectbox("dono", all_usernames(), key=f"own_{_l['id']}",
                                  label_visibility="collapsed")
            if _x3.button("Atribuir", key=f"att_{_l['id']}"):
                db.client.table("companies").update(
                    {"owner_username": _novo}).eq("id", _l["id"]).execute()
                st.rerun()
    with _cr:
        section_header("Parados ha 7+ dias", "hourglass_bottom")
        if not _panel["parados_7d"]:
            st.caption("Nenhum lead parado. ✓")
        for _l in _panel["parados_7d"][:8]:
            _lc = parse_ts(_l.get("last_contacted_at"))
            _dias = (_now - _lc).days if _lc else "?"
            _y1, _y2, _y3 = st.columns([4, 2, 1.8])
            _y1.markdown(f"**{_l.get('name')}** — {_dias}d "
                         f"({_l.get('owner_username') or 'sem dono'})")
            _novo = _y2.selectbox("p/", all_usernames(), key=f"re_{_l['id']}",
                                  label_visibility="collapsed")
            if _y3.button("Reatribuir", key=f"reb_{_l['id']}"):
                db.client.table("companies").update(
                    {"owner_username": _novo}).eq("id", _l["id"]).execute()
                db.reassign_company_activities(
                    _l["id"], _novo, note=f"reatribuida por {_username}")
                st.rerun()

    if _panel["fila_aging"]:
        section_header("Fila de aprovacao envelhecendo (+24h)", "schedule")
        for _o, _n in _panel["fila_aging"].items():
            st.markdown(f"- **{_o}**: {_n} mensagens paradas")

else:
    _col_main, _col_side = st.columns([2.1, 1])

    with _col_main:
        _hl, _hr = st.columns([3, 1.4])
        with _hl:
            section_header("Minha Agenda", "event_note")
        with _hr:
            if st.button("+ Nova atividade"):
                _dlg_nova_atividade()

        _g = hv.agenda_groups(_username, _now)
        _total_aberta = sum(len(v) for v in _g.values())

        if _total_aberta == 0:
            try:
                _n_escolas = int(db.client.table("companies").select(
                    "id", count="exact").limit(1).execute().count or 0)
            except Exception:
                _n_escolas = 0
            if _n_escolas < 5:
                empty_state("🧭", "Primeiro dia? Siga o roteiro",
                            "1) Conheca as escolas da base · 2) Pergunte algo ao "
                            "IAlex · 3) Aprove sua primeira mensagem · 4) Registre "
                            "seu primeiro contato")
                st.page_link("pages/2_🏫_Escolas.py", label="1 · Conhecer as escolas →")
                st.page_link("pages/0_💬_Chat_IAlex.py", label="2 · Perguntar ao IAlex →")
                st.page_link("pages/6_✉️_Comunicacao.py", label="3 · Aprovar mensagens →")
            else:
                empty_state("🎉", "Tudo em dia!",
                            "Nenhuma atividade pendente. Que tal buscar escolas novas?")
                st.page_link("pages/5_📊_Pipeline.py",
                             label="Prospectar escolas novas →")
        else:
            if _g["atrasadas"]:
                st.markdown(f"**⚠️ Atrasadas ({len(_g['atrasadas'])})**")
                for _a in _g["atrasadas"][:10]:
                    _render_activity(_a, overdue=True)
            st.markdown(f"**Hoje ({len(_g['hoje'])})**")
            if not _g["hoje"]:
                st.caption("Nada mais para hoje.")
            for _a in _g["hoje"][:10]:
                _render_activity(_a, overdue=False)
            if _g["amanha"]:
                # aberto quando ha poucas — atividade criada "pra amanha" deve
                # ser VISIVEL na hora (aprendizado do teste do dono)
                with st.expander(f"Amanha ({len(_g['amanha'])})",
                                 expanded=len(_g["amanha"]) <= 3):
                    for _a in _g["amanha"][:10]:
                        st.markdown(activity_row(_a, when_txt=_when_txt(_a, False)),
                                    unsafe_allow_html=True)
            if _g["proximas"]:
                with st.expander(f"Proximas ({len(_g['proximas'])})"):
                    for _a in _g["proximas"][:10]:
                        st.markdown(activity_row(_a, when_txt=_when_txt(_a, False)),
                                    unsafe_allow_html=True)

        # contador de concluidas hoje (reforco positivo barato — SPEC §1.8)
        try:
            _start_day = to_brt(_now).replace(hour=0, minute=0, second=0,
                                              microsecond=0)
            _done_today = int(db.client.table("activities").select(
                "id", count="exact").eq("owner_username", _username)
                .eq("status", "done")
                .gte("completed_at", _start_day.isoformat())
                .execute().count or 0)
            if _done_today:
                st.caption(f"✓ {_done_today} concluida(s) hoje")
        except Exception:
            pass

    with _col_side:
        section_header("Agir agora", "local_fire_department")
        _hot = hv.hot_leads(3)
        if not _hot:
            st.caption("Nenhum lead quente no momento.")
        for _l in _hot:
            _lc = parse_ts(_l.get("last_contacted_at"))
            _motivo = (f"sem contato ha {(_now - _lc).days}d" if _lc
                       else "ainda sem contato")
            st.markdown(
                f'<div class="v2-side">{priority_badge(_l.get("urgency_tier"))}<br/>'
                f'<strong style="font-size:13.5px">{_l.get("name")}</strong><br/>'
                f'<span style="font-size:11.5px;color:#94A3B8">score '
                f'{int(_l.get("urgency_score") or 0)} · {_motivo}</span></div>',
                unsafe_allow_html=True)

        section_header("Proximas 24h", "calendar_month")
        _meets = hv.reunioes_24h(None if _admin else _username, _now)
        if not _meets:
            st.caption("Nenhuma reuniao nas proximas 24h.")
        for _m in _meets:
            _dt = parse_ts(_m.get("scheduled_at"))
            _quando = to_brt(_dt).strftime("%d/%m %Hh%M") if _dt else "?"
            st.markdown(f"📅 **{_quando}** — {_m.get('title') or 'Reuniao'}")

        try:
            from utils.date_pt import format_pt as _fmt_pt
            _mes_nome = _fmt_pt(to_brt(_now), "%B").capitalize()
        except Exception:
            _mes_nome = to_brt(_now).strftime("%m/%Y")
        section_header(f"Minha meta — {_mes_nome}", "flag")
        _metas = hv.minhas_metas(_username, _now)
        if not _metas:
            st.caption("Sem metas definidas para o mes." +
                       (" Defina em Resultados →" if _admin else
                        " Fale com o gestor."))
        for _mt in _metas[:3]:
            st.markdown(goal_progress(goal_metric_label(_mt["metric"]),
                                      _mt["realized"], _mt["target"]),
                        unsafe_allow_html=True)
        st.page_link("pages/8_📈_Analytics.py", label="ver Resultados →")
