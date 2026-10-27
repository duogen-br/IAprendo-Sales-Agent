"""
IAlex Scheduler - Envia mensagens proativas para o Fernando via WhatsApp.
- Briefing matinal (8h)
- Alertas instantaneos (quando alguem responde)
- Resumo semanal (sexta 17h)
"""
import sys
import os
import time
import threading
import schedule
from datetime import datetime, timedelta, timezone
from typing import Dict, Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.logger import logger

# Fuso de Sao Paulo para a regra de horario comercial dos envios automaticos.
# Mesmo padrao usado em workflows/activity_engine.py.
try:
    from zoneinfo import ZoneInfo
    BRT = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover
    BRT = timezone(timedelta(hours=-3))

# Intervalos das tarefas recorrentes (minutos). Reduzidos em 27/10/2026 para o
# banco caber no plano gratuito do Supabase (o projeto estourou o limite de
# Disk IO com ~2,6 milhoes de requisicoes/mes). Cada valor pode ser ajustado
# sem mexer no codigo pela variavel de ambiente SCHED_<NOME>_MIN.
SCHEDULE_MINUTES = {
    "CHECK_REPLIES": 30,          # era 15
    "HUBSPOT_PULL": 60,           # era 15
    "AUTO_REPLIES": 30,           # era 15
    "SEND_SCHEDULED": 15,         # era 5 (envio continua so na janela comercial)
    "DYNAMIC_SCORES": 360,        # era 30 (varre todas as escolas)
    "BUYING_SIGNALS": 360,        # era 30
    "URGENCY_SCORES": 360,        # era 30 (varre todas as escolas)
    "AUTO_HEAL": 120,             # era 30
    "ACTIVITY_ENGINE": 60,        # era 30
    "OUTLOOK_POLL": 60,           # era 15
    "PRE_MEETING": 15,            # era 5 (janela de 35 min, com dedupe)
    "POST_MEETING": 60,           # era 15
}


def sched_minutes(name: str) -> int:
    """Intervalo em minutos da tarefa `name`, com override por SCHED_<NOME>_MIN."""
    default = SCHEDULE_MINUTES[name]
    try:
        value = int(os.getenv(f"SCHED_{name}_MIN", default))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default

# Janela de envio automatico (BRT): envia somente entre 8h e 18h.
BUSINESS_SEND_START, BUSINESS_SEND_END = 8, 18

# Dias em que o envio AUTOMATICO pode ocorrer (0=segunda ... 6=domingo).
# Ate 28/08/2026 nao havia regra de dia nenhuma: o job rodava a cada 5 min TODOS
# os dias, entao o que fosse aprovado numa sexta a tarde saia no sabado as 8h.
SEND_WEEKDAYS = frozenset({0, 1, 2, 3, 4})  # seg a sex

# Sexta tem janela propria de 1 hora: e-mail de prospeccao enviado na sexta a
# tarde chega quando o destinatario ja esta saindo e morre no fim de semana.
FRIDAY = 4
FRIDAY_SEND_HOUR = 8


def send_window_check(agora: datetime) -> tuple:
    """A janela do envio AUTOMATICO. Pura de proposito, para ser testavel.

    Retorna (pode_enviar, motivo). O motivo vai pro log quando bloqueia.

    Regras (horario de Porto Alegre):
      - sabado e domingo: nunca;
      - sexta: somente no horario das 8h (unica janela do dia);
      - segunda a quinta: entre 8h e 18h.

    NAO se aplica ao envio manual — o botao "Enviar agora" do painel e a tool
    `enviar_aprovados` do WhatsApp chamam send_approved_messages() direto. Se a
    pessoa clica num sabado, ela quer enviar num sabado.
    """
    dia, hora = agora.weekday(), agora.hour
    if dia not in SEND_WEEKDAYS:
        return False, f"{agora:%d/%m} e fim de semana — envio retomado na segunda as {BUSINESS_SEND_START}h"
    if dia == FRIDAY:
        if hora != FRIDAY_SEND_HOUR:
            return False, (f"sexta so envia no horario das {FRIDAY_SEND_HOUR}h "
                           f"(agora {agora:%H:%M}) — proximo envio na segunda as "
                           f"{BUSINESS_SEND_START}h")
        return True, ""
    if not (BUSINESS_SEND_START <= hora < BUSINESS_SEND_END):
        return False, (f"fora do horario comercial (agora {agora:%H:%M}; janela "
                       f"{BUSINESS_SEND_START}h-{BUSINESS_SEND_END}h)")
    return True, ""


def next_send_slot(desejado: datetime):
    """Quando uma mensagem agendada para `desejado` REALMENTE sai.

    Se o horario escolhido cai fora da janela (fim de semana, sexta a tarde,
    madrugada), o envio nao se perde: o send_approved busca
    `scheduled_send_at <= agora`, entao a mensagem sai na proxima janela valida.
    Esta funcao calcula esse instante para a TELA nao prometer um horario que
    nao vai acontecer.

    Granularidade de hora (o job roda a cada 5 min dentro da hora liberada).
    """
    if send_window_check(desejado)[0]:
        return desejado
    # anda de hora em hora ate a proxima janela (no maximo uma semana)
    cursor = desejado.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(24 * 8):
        if send_window_check(cursor)[0]:
            return cursor
        cursor += timedelta(hours=1)
    return desejado  # inalcancavel com as regras atuais; nunca mente por omissao


class IALexScheduler:
    """Agenda e envia mensagens proativas."""

    # Tag usada nos jobs do pipeline automatico (facilita reload dinamico)
    PIPELINE_TAG = "automated_pipeline"
    FOLLOWUP_TAG = "automated_followup"

    def __init__(self):
        self._running = False
        self._thread = None

    def start(self):
        """Inicia o scheduler em background thread."""
        if self._running:
            return

        # Agendar tarefas
        # Briefing matinal UNICO as 08:00 (inclui urgency digest integrado)
        # Nao separar em 08:00 + 08:15 para evitar mensagens duplicadas
        schedule.every().day.at("08:00").do(self._morning_briefing)
        # 13:00 a pedido do dono (Ago/2026): lembrete do dia apos o almoco.
        # ATENCAO: a lib `schedule` usa o horario LOCAL da maquina — a VM
        # precisa estar em America/Sao_Paulo (setup_vm.sh faz isso; em UTC
        # tudo dispararia 3h mais cedo no fuso de Porto Alegre).
        schedule.every().day.at("13:00").do(self._midday_check)
        schedule.every().day.at("17:00").do(self._end_of_day)
        schedule.every().friday.at("17:30").do(self._weekly_report)
        schedule.every(sched_minutes("CHECK_REPLIES")).minutes.do(self._check_replies)
        schedule.every(sched_minutes("HUBSPOT_PULL")).minutes.do(self._hubspot_pull)
        # Auto-resposta a replies — a cada 15 min
        schedule.every(sched_minutes("AUTO_REPLIES")).minutes.do(self._process_auto_replies)
        # Envio de mensagens agendadas — a cada 5 min
        schedule.every(sched_minutes("SEND_SCHEDULED")).minutes.do(self._send_scheduled_messages)
        # Lead scoring dinamico — a cada 30 min
        schedule.every(sched_minutes("DYNAMIC_SCORES")).minutes.do(self._update_dynamic_scores)
        # Detector de sinais de compra (intent alerts) — a cada 30 min
        schedule.every(sched_minutes("BUYING_SIGNALS")).minutes.do(self._check_buying_signals)
        # Score de urgencia unificado (F2) — a cada 30 min, DEPOIS dos sub-scores
        schedule.every(sched_minutes("URGENCY_SCORES")).minutes.do(self._update_urgency_scores)
        # NOTA: digest de urgencia NAO e mais agendado separadamente —
        # foi integrado ao _morning_briefing() para enviar UMA so mensagem
        # Auto-healing (F6 Fase 3A) — a cada 30 min, apos urgency
        # Detecta problemas e tenta remediar (restart bridge, notifica Fernando)
        schedule.every(sched_minutes("AUTO_HEAL")).minutes.do(self._auto_heal_system)
        # Activity engine (redesign v2 F1) — agenda do time: varredor de
        # auto-resolucao + criacao pelas regras (docs/SPEC_AGENDA_METAS.md).
        # Idempotente por dedupe_key; tambem rodara no load da Home (F2).
        schedule.every(sched_minutes("ACTIVITY_ENGINE")).minutes.do(self._run_activity_engine)
        # Retreino semanal do modelo preditivo (domingo 03:00)
        schedule.every().sunday.at("03:00").do(self._retrain_predictive_model)

        # Pre-geracao de OPR + graficos de insight (kaleido) FORA do Cloud.
        # O Streamlit Cloud nao roda kaleido — ele apenas CONSOME os artefatos
        # prontos no Supabase. Este job (rodando onde o IAlex roda: PC/Oracle)
        # mantem o Cloud sempre fresco => equivalencia entre online e local.
        # Noite (04:00): so as escolas SEM artefato (novas importadas) — barato.
        # Domingo (04:30): refresh COMPLETO (dado anual/ENEM/matriculas muda).
        schedule.every().day.at("04:00").do(self._pregenerate_artifacts)
        schedule.every().sunday.at("04:30").do(
            self._pregenerate_artifacts, full_refresh=True
        )

        # Outlook Calendar — poll + briefings + pós-reunião
        schedule.every(sched_minutes("OUTLOOK_POLL")).minutes.do(self._poll_outlook_calendar)
        schedule.every(sched_minutes("PRE_MEETING")).minutes.do(self._check_pre_meeting_briefings)
        schedule.every(sched_minutes("POST_MEETING")).minutes.do(self._check_post_meeting_followup)

        # Pipeline automatico dinamico (carregado da config do banco)
        self._register_automated_pipeline()

        # Follow-ups comportamentais automaticos (Item 6)
        self._register_automated_followup()

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("IAlex scheduler iniciado")

    def stop(self):
        """Para o scheduler."""
        self._running = False
        schedule.clear()
        logger.info("IAlex scheduler parado")

    def _run_loop(self):
        """Loop principal do scheduler."""
        while self._running:
            try:
                schedule.run_pending()
            except Exception as e:
                logger.error(f"Erro no scheduler: {e}")
            time.sleep(30)

    def _send_to_owner(self, text: str):
        """Envia mensagem para o Fernando via WhatsApp."""
        try:
            from agent.whatsapp_bridge import WhatsAppBridge
            bridge = WhatsAppBridge()
            owner = os.getenv("IALEX_OWNER_NUMBER", "")
            if owner:
                bridge.send_message(owner, text)
                logger.info("Mensagem proativa enviada", extra={"len": len(text)})
            else:
                logger.warning("IALEX_OWNER_NUMBER nao configurado")
        except Exception as e:
            logger.error(f"Erro ao enviar mensagem proativa: {e}")

    def _run_background_with_feedback(
        self,
        task_name: str,
        task_fn,
        timeout_minutes: int = 10,
    ):
        """Executa uma funcao em background thread com GARANTIA de feedback.
        Se a task terminar (sucesso ou erro), envia resultado via WhatsApp.
        Se exceder timeout, envia aviso de timeout.

        REGRA: toda task background DEVE ter feedback. Fernando nunca fica sem resposta.

        Args:
            task_name: nome amigavel (ex: "Follow-ups", "Pipeline")
            task_fn: callable que recebe None e retorna None (usa _send_to_owner internamente)
            timeout_minutes: timeout em minutos (default 10)
        """
        import time as _time

        def _wrapped():
            started = _time.time()
            feedback_sent = False
            try:
                task_fn()
                feedback_sent = True  # task_fn deve ter chamado _send_to_owner
            except Exception as e:
                logger.error(f"Task background '{task_name}' falhou: {e}", exc_info=True)
                if not feedback_sent:
                    try:
                        self._send_to_owner(
                            f"❌ *{task_name} — ERRO*\n\n"
                            f"A tarefa falhou apos {int((_time.time() - started) / 60)} min.\n"
                            f"Erro: `{str(e)[:300]}`\n\n"
                            f"_Verifique os logs no dashboard._"
                        )
                    except Exception:
                        pass

        def _with_timeout():
            import time as _t
            task_thread = threading.Thread(target=_wrapped, daemon=True)
            task_thread.start()
            task_thread.join(timeout=timeout_minutes * 60)
            if task_thread.is_alive():
                logger.error(f"Task background '{task_name}' excedeu timeout de {timeout_minutes} min")
                try:
                    self._send_to_owner(
                        f"⏰ *{task_name} — TIMEOUT*\n\n"
                        f"A tarefa nao terminou em {timeout_minutes} minutos.\n"
                        f"Pode estar travada. Verifique os logs."
                    )
                except Exception:
                    pass

        threading.Thread(target=_with_timeout, daemon=True).start()

    # ============================================================
    # OUTLOOK CALENDAR (poll + briefing + pós-reunião)
    # ============================================================

    def _poll_outlook_calendar(self):
        """Poll Outlook Calendar a cada 15 min — detectar reuniões com escolas."""
        try:
            from integrations.outlook_client import outlook_client
            if not outlook_client.is_available():
                return

            events = outlook_client.get_upcoming_events(hours=72)
            if not events:
                return

            from database.supabase_client import db as _db

            for event in events:
                subject = event.get("subject", "")
                event_id = event.get("id", "")
                start_dt = outlook_client.parse_event_time(event)
                if not start_dt or not subject:
                    continue

                # Checar se já processamos este evento (por subject+start como chave)
                start_iso = start_dt.isoformat()
                try:
                    existing = _db.client.table("meetings").select("id").eq(
                        "notes", f"outlook_event:{event_id}"
                    ).limit(1).execute()
                    if existing.data:
                        continue  # Já registrado
                except Exception:
                    pass

                # Tentar match com escola do banco
                school = outlook_client.match_event_to_school(event)
                if not school:
                    continue  # Não é reunião com escola

                company_id = school["id"]
                school_name = school.get("name", "?")

                # Registrar meeting no banco
                try:
                    end_dt = outlook_client.parse_event_end(event)
                    _db.client.table("meetings").insert({
                        "company_id": company_id,
                        "meeting_type": "online",
                        "status": "scheduled",
                        "scheduled_at": start_iso,
                        "notes": f"outlook_event:{event_id}",
                    }).execute()

                    # Registrar interaction
                    _db.client.table("interactions").insert({
                        "company_id": company_id,
                        "type": "meeting_scheduled",
                        "channel": "outlook",
                    }).execute()

                    logger.info(f"Outlook: reuniao detectada com {school_name}", extra={
                        "event_subject": subject, "start": start_iso,
                    })

                    # Notificar Fernando
                    self._send_to_owner(
                        f"📅 *Reuniao detectada no Outlook!*\n\n"
                        f"🏫 *{school_name}*\n"
                        f"📋 {subject}\n"
                        f"🕐 {start_dt.strftime('%d/%m/%Y %H:%M')}\n\n"
                        f"_Enviarei um briefing 30 min antes._"
                    )
                except Exception as e:
                    logger.debug(f"Outlook poll insert: {e}")

        except Exception as e:
            logger.debug(f"Outlook poll: {e}")

    def _check_pre_meeting_briefings(self):
        """Envia briefing no WhatsApp 30 min antes de reuniões agendadas."""
        try:
            from database.supabase_client import db as _db
            now = datetime.now(timezone.utc)
            window_start = now
            window_end = now + timedelta(minutes=35)

            # Buscar meetings scheduled nos próximos 30-35 min
            meetings = _db.client.table("meetings").select(
                "id,company_id,scheduled_at,status,notes"
            ).eq("status", "scheduled").gte(
                "scheduled_at", window_start.isoformat()
            ).lte(
                "scheduled_at", window_end.isoformat()
            ).execute().data or []

            for meeting in meetings:
                company_id = meeting.get("company_id")
                if not company_id:
                    continue

                # Checar se já enviamos briefing (evitar duplicata)
                notes = meeting.get("notes", "") or ""
                if "briefing_sent" in notes:
                    continue

                # Buscar dados da escola
                try:
                    school = _db.get_company_detail(company_id)
                except Exception:
                    school = {}
                if not school:
                    continue

                school_name = school.get("name", "?")
                city = school.get("city", "")
                score = school.get("qualification_score", "?")
                porte = school.get("school_size", "?")
                tipo = school.get("admin_category", "?")

                # Contatos
                contatos_text = ""
                try:
                    contacts = _db.client.table("contacts").select(
                        "full_name,role,email"
                    ).eq("company_id", company_id).limit(5).execute().data or []
                    if contacts:
                        contatos_text = "\n".join(
                            f"• {c.get('full_name', '?')} — {c.get('role', '?')} ({c.get('email', '')})"
                            for c in contacts
                        )
                except Exception:
                    pass

                # Histórico de emails
                historico_text = ""
                try:
                    emails = _db.client.table("approval_queue").select(
                        "subject,status,sent_at,opened_at,clicked_at,replied_at,follow_up_number"
                    ).eq("company_id", company_id).eq("status", "sent").order(
                        "sent_at", desc=False
                    ).limit(5).execute().data or []
                    if emails:
                        lines = []
                        for e in emails:
                            fu = e.get("follow_up_number", 0) or 0
                            fu_tag = f" (FU#{fu})" if fu > 0 else ""
                            tracking = []
                            if e.get("replied_at"):
                                tracking.append("respondeu")
                            elif e.get("clicked_at"):
                                tracking.append("clicou")
                            elif e.get("opened_at"):
                                tracking.append("abriu")
                            else:
                                tracking.append("enviado")
                            sent_date = (e.get("sent_at") or "")[:10]
                            lines.append(f"• {sent_date}{fu_tag}: {e.get('subject', '')[:40]} — {', '.join(tracking)}")
                        historico_text = "\n".join(lines)
                except Exception:
                    pass

                # Memórias
                memorias_text = ""
                try:
                    from integrations.memory import memory
                    mems = memory.get_for("company", company_id, limit=5)
                    if mems:
                        memorias_text = memory.format_for_context(mems)
                except Exception:
                    pass

                # Montar briefing
                sched_time = (meeting.get("scheduled_at") or "")[:16]
                briefing = (
                    f"📅 *Reuniao em 30 minutos!*\n\n"
                    f"🏫 *{school_name}*\n"
                    f"📍 {city} | 🎯 Score: {score} | 📊 {porte}\n"
                    f"📋 {tipo}\n"
                )
                if contatos_text:
                    briefing += f"\n👤 *Contatos:*\n{contatos_text}\n"
                if historico_text:
                    briefing += f"\n📧 *Historico de emails:*\n{historico_text}\n"
                if memorias_text:
                    briefing += f"\n💡 *Insights/memorias:*\n{memorias_text}\n"
                briefing += f"\n⚡ *Boa reuniao!*"

                self._send_to_owner(briefing)

                # Marcar briefing como enviado
                try:
                    new_notes = f"{notes}|briefing_sent"
                    _db.client.table("meetings").update({"notes": new_notes}).eq(
                        "id", meeting["id"]
                    ).execute()
                except Exception:
                    pass

                logger.info(f"Briefing pre-reuniao enviado: {school_name}")

        except Exception as e:
            logger.debug(f"Pre-meeting briefing: {e}")

    def _check_post_meeting_followup(self):
        """Pede resumo no WhatsApp após reuniões que já terminaram."""
        try:
            from database.supabase_client import db as _db
            now = datetime.now(timezone.utc)

            # Buscar meetings scheduled que já passaram (> 1h atrás, para dar margem)
            cutoff = (now - timedelta(hours=1)).isoformat()
            meetings = _db.client.table("meetings").select(
                "id,company_id,scheduled_at,status,notes"
            ).eq("status", "scheduled").lt(
                "scheduled_at", cutoff
            ).limit(5).execute().data or []

            for meeting in meetings:
                notes = meeting.get("notes", "") or ""
                if "post_followup_sent" in notes:
                    continue

                company_id = meeting.get("company_id")
                school_name = "?"
                if company_id:
                    try:
                        c = _db.client.table("companies").select("name").eq(
                            "id", company_id
                        ).single().execute()
                        school_name = (c.data or {}).get("name", "?")
                    except Exception:
                        pass

                self._send_to_owner(
                    f"✅ *Reuniao com {school_name} encerrou!*\n\n"
                    f"Como foi? Me conte em poucas palavras:\n"
                    f"1️⃣ Interessado (quer piloto/proposta)\n"
                    f"2️⃣ Precisa pensar (follow-up em X dias)\n"
                    f"3️⃣ Nao interessado\n"
                    f"4️⃣ Fechou negocio!\n\n"
                    f"_Ou descreva livremente o resultado._"
                )

                # Marcar como post_followup_sent
                try:
                    new_notes = f"{notes}|post_followup_sent"
                    _db.client.table("meetings").update({"notes": new_notes}).eq(
                        "id", meeting["id"]
                    ).execute()
                except Exception:
                    pass

                logger.info(f"Post-meeting followup enviado: {school_name}")

        except Exception as e:
            logger.debug(f"Post-meeting followup: {e}")

    def _update_dynamic_scores(self):
        """Recalcula scores dinamicos de todas as escolas com interacoes."""
        try:
            from tools.dynamic_score import dynamic_scorer
            result = dynamic_scorer.update_all_scores()
            updated = result.get("updated", 0)
            if updated > 0:
                logger.info(f"Scores dinamicos: {updated} escola(s) atualizada(s)")
        except Exception as e:
            logger.debug(f"Dynamic scores skip: {e}")

    def _process_auto_replies(self):
        """Processa replies de escolas e gera auto-respostas na fila."""
        try:
            from tools.reply_handler import reply_handler
            result = reply_handler.process_new_replies(limit=5)
            generated = result.get("generated", 0)
            if generated > 0:
                details = result.get("details", [])
                lines = [f"📩 *{generated} auto-resposta(s) gerada(s)*\n"]
                for d in details[:5]:
                    emoji = d.get("intent_emoji", "📧")
                    lines.append(
                        f"{emoji} *{d.get('escola', '?')}* — {d.get('intent_label', '?')}\n"
                        f"   _\"{d.get('reply_preview', '')[:80]}\"_"
                    )
                lines.append(f"\n📋 Revise na fila de aprovacao antes de enviar.")
                self._send_to_owner("\n".join(lines))
                logger.info(f"Auto-replies: {generated} geradas")
        except Exception as e:
            logger.debug(f"Auto-replies skip: {e}")

    def _send_scheduled_messages(self):
        """Verifica e envia mensagens agendadas cujo horario ja passou.
        Roda a cada 5 minutos. Se scheduled_send_at <= NOW() ou NULL, envia.
        Notifica Fernando sobre envios E bloqueios (sem email).

        Janela: ver send_window_check() — dias uteis, 8h-18h, e na sexta apenas
        o horario das 8h. Fora dela as aprovadas ficam represadas (nada e
        perdido: sao reavaliadas na proxima execucao). O botao "Enviar agora" do
        dashboard e a tool `enviar_aprovados` do WhatsApp chamam
        send_approved_messages() diretamente e NAO passam por este check.
        """
        now_brt = datetime.now(BRT)
        _pode, _motivo = send_window_check(now_brt)
        if not _pode:
            logger.info(f"Envio agendado pulado: {_motivo}")
            return

        try:
            from workflows.send_approved import send_approved_messages
            result = send_approved_messages(limit=20)
            n_sent = result.get("sent", 0)
            n_skipped = result.get("skipped", 0)

            if n_sent > 0:
                logger.info(f"Envio agendado: {n_sent} mensagem(ns) enviada(s)")
                self._send_to_owner(
                    f"📤 *Envio concluido*\n\n"
                    f"✅ {n_sent} mensagem(ns) enviada(s) agora."
                )

            if n_skipped > 0:
                blocked_details = [d for d in result.get("details", []) if d.get("status") == "blocked"]
                logger.warning(f"Envio: {n_skipped} mensagem(ns) bloqueada(s) por falta de email")
                self._send_to_owner(
                    f"⚠️ *{n_skipped} mensagem(ns) BLOQUEADA(S)*\n\n"
                    f"Motivo: contato sem email cadastrado.\n"
                    f"Adicione o email do contato no dashboard e reprocesse.\n"
                    f"_As mensagens foram movidas para status 'blocked'._"
                )
        except Exception as e:
            logger.error(f"Erro no envio agendado: {e}")

    def _morning_briefing(self):
        """Envia briefing matinal UNICO as 8h.

        Combina: briefing geral + urgency highlights + urgency digest + trends.
        Tudo em UMA so mensagem para nao sobrecarregar Fernando.
        Respeita autonomia: em modo MANUAL, nao envia.
        """
        try:
            from integrations.pipeline_config import pipeline_config
            if pipeline_config.get_autonomy_level() == "manual":
                logger.debug("Briefing matinal suprimido (modo manual)")
                return

            from agent.brain import Brain
            brain = Brain()
            briefing = brain.get_morning_briefing()

            # v2 F1: o digest ABRE com a agenda do dia (SPEC §2/§6).
            agenda_section = ""
            try:
                agenda_section = self._agenda_digest_section()
            except Exception:
                pass

            # F2: adicionar contagem de urgencia ao briefing
            urgency_line = ""
            try:
                from tools.urgency_scorer import urgency_scorer
                critical = urgency_scorer.get_by_tier("CRITICAL", limit=50)
                hot = urgency_scorer.get_by_tier("HOT", limit=50)
                if critical or hot:
                    parts = []
                    if critical:
                        parts.append(f"\U0001f534 {len(critical)} CRITICO(S)")
                    if hot:
                        parts.append(f"\U0001f7e0 {len(hot)} QUENTE(S)")
                    urgency_line = f"\n\n\U0001f525 *Urgencia:* {' | '.join(parts)}"
            except Exception:
                pass

            # F2: integrar urgency digest (antes era enviado separado as 08:15)
            digest_section = ""
            try:
                from tools.proactive_actions import proactive_engine
                digest = proactive_engine.generate_daily_digest()
                if digest:
                    digest_section = f"\n\n---\n\n{digest}"
                # Trends de tier
                trends = proactive_engine.detect_and_format_trends()
                if trends:
                    digest_section += f"\n\n{trends}"
                # Inatividade
                from config.settings import settings as _s
                inactive = proactive_engine.detect_inactivity(days=_s.URGENCY_INACTIVITY_DAYS)
                inactivity_msg = proactive_engine.format_inactivity_for_whatsapp(inactive)
                if inactivity_msg:
                    digest_section += f"\n\n{inactivity_msg}"
            except Exception:
                pass

            if briefing or agenda_section:
                full_msg = (f"\u2600\ufe0f *Bom dia, Fernando!*\n\n{agenda_section}"
                            f"{briefing}{urgency_line}{digest_section}")
                self._send_to_owner(full_msg)
        except Exception as e:
            logger.error(f"Erro no briefing matinal: {e}")

    def _agenda_digest_section(self) -> str:
        """Bloco de agenda que abre o digest (SPEC \u00a72): atividades de hoje,
        atrasadas e prioridade 1 + aviso de sobrecarga (>12)."""
        try:
            from workflows.activity_engine import now_utc, parse_ts, to_brt
            from database.supabase_client import db
            import os as _os
            owner = _os.getenv("IALEX_OWNER_USERNAME", "fernando")
            now = now_utc()
            today = to_brt(now).date()
            acts = db.list_activities(owner=owner, status=["open"], limit=100)
            late, today_n, prio1 = 0, 0, 0
            for a in acts:
                due = parse_ts(a.get("due_at"))
                if not due:
                    continue
                if due < now:
                    late += 1
                if to_brt(due).date() == today:
                    today_n += 1
                if a.get("priority") == 1 and (due < now or to_brt(due).date() == today):
                    prio1 += 1
            if not acts:
                return ""
            line = (f"\U0001f4cb *Sua agenda:* {today_n} atividades hoje"
                    + (f" ({late} atrasadas" + (f", {prio1} prioridade maxima)" if prio1 else ")") if late or prio1 else ""))
            if today_n > 12:
                line += "\n\u26a0\ufe0f Dia cheio \u2014 ataque so as de prioridade 1."
            return line + "\n\n"
        except Exception:
            return ""

    def _run_activity_engine(self):
        """Roda o motor da agenda (v2 F1) \u2014 varredor + regras. Idempotente."""
        try:
            from workflows.activity_engine import run_engine
            summary = run_engine()
            if summary.get("created") or summary.get("swept"):
                logger.info("activity_engine via scheduler", extra=summary)
        except Exception as e:
            logger.error(f"Erro no activity engine: {e}")

    def _midday_check(self):
        """Check rapido ao meio-dia."""
        try:
            from database.supabase_client import db
            from approval_queue import queue_manager

            stats = queue_manager.get_stats()
            pending = stats.get("pending", 0)

            if pending > 0:
                self._send_to_owner(
                    f"🔔 Lembrete: voce tem *{pending} email(s)* aguardando aprovacao.\n"
                    f"Quer que eu aprove todos? Responda 'aprova tudo'."
                )
        except Exception as e:
            logger.error(f"Erro no check meio-dia: {e}")

    def _end_of_day(self):
        """Resumo do dia as 17h."""
        try:
            from database.supabase_client import db

            today = datetime.now().strftime("%Y-%m-%d")
            sent_today = db.client.table("approval_queue").select(
                "id", count="exact"
            ).eq("status", "sent").gte("sent_at", f"{today}T00:00:00").execute()

            count = sent_today.count or 0
            if count > 0:
                self._send_to_owner(
                    f"📊 *Resumo do dia:*\n"
                    f"📤 {count} email(s) enviado(s) hoje.\n"
                    f"Bom trabalho! 💪"
                )
        except Exception as e:
            logger.error(f"Erro no resumo do dia: {e}")

    def _weekly_report(self):
        """Resumo semanal na sexta as 17:30."""
        try:
            from agent.brain import Brain
            brain = Brain()
            report = brain.get_weekly_report()
            if report:
                self._send_to_owner(f"📋 *Resumo Semanal*\n\n{report}")
        except Exception as e:
            logger.error(f"Erro no relatorio semanal: {e}")

    def _check_replies(self):
        """Verifica a cada 15 min se houve novas respostas."""
        try:
            from database.supabase_client import db

            # Buscar emails com replied_at nos ultimos 15 minutos
            fifteen_min_ago = (datetime.now() - timedelta(minutes=16)).isoformat()
            recent_replies = db.client.table("approval_queue").select(
                "id,subject,replied_at,companies(name),contacts(full_name)"
            ).eq("status", "sent").gte("replied_at", fifteen_min_ago).execute().data or []

            for reply in recent_replies:
                comp = reply.get("companies") or {}
                ct = reply.get("contacts") or {}
                self._send_to_owner(
                    f"🎉 *RESPOSTA RECEBIDA!*\n\n"
                    f"🏫 {comp.get('name', '?')}\n"
                    f"👤 {ct.get('full_name', '?')}\n"
                    f"📧 Re: {reply.get('subject', '')[:40]}\n\n"
                    f"Verifique seu email! Quer que eu sugira proximos passos?"
                )
        except Exception as e:
            # Nao logar se for erro normal (tabela sem coluna, etc)
            if "column" not in str(e).lower():
                logger.error(f"Erro ao verificar replies: {e}")
        # v2 F1: resposta de lead e o gatilho mais valioso — roda o engine ja
        # (latencia de 15min pra atividade "Responder" nascer; SPEC §1.3 R1).
        try:
            self._run_activity_engine()
        except Exception:
            pass

    def _hubspot_pull(self):
        """Puxa mudancas do HubSpot a cada 15 min (sincronizacao reversa)."""
        try:
            from integrations.hubspot_pull import hubspot_pull
            result = hubspot_pull.pull_changes()
            total = result.get("companies", 0) + result.get("contacts", 0) + result.get("deals", 0)
            if total > 0:
                logger.info(f"HubSpot pull: {total} registros atualizados", extra=result)
        except ImportError:
            pass  # HubSpot nao configurado
        except Exception as e:
            logger.error(f"Erro no HubSpot pull agendado: {e}")

    def _check_buying_signals(self):
        """Detecta sinais de compra (intent alerts) e notifica Fernando na hora.
        Roda a cada 30 minutos. Usa o modulo intent_detector com dedup via memoria.
        """
        try:
            from tools.intent_detector import intent_detector
            new_alerts = intent_detector.get_new_alerts(days=7, min_score=50)
            if not new_alerts:
                return
            logger.info(f"Intent detector: {len(new_alerts)} novo(s) alerta(s) de compra")
            for signal in new_alerts:
                message = intent_detector.format_for_whatsapp(signal)
                self._send_to_owner(message)
                intent_detector.mark_alerted(signal)
        except Exception as e:
            logger.error(f"Erro no check buying signals: {e}")

    # ============================================================
    # URGENCY SCORE (F2)
    # ============================================================

    def _update_urgency_scores(self):
        """Recalcula scores de urgencia unificados (F2).
        Roda a cada 30 min, DEPOIS de dynamic_scores e buying_signals.
        Se detectar novo lead CRITICAL, envia alerta WhatsApp imediato.

        Respeita autonomia: em modo MANUAL, recalcula scores (database) mas
        NAO envia alertas WhatsApp proativos.
        """
        try:
            from tools.urgency_scorer import urgency_scorer
            result = urgency_scorer.compute_all()
            updated = result.get("updated", 0)
            by_tier = result.get("by_tier", {})

            if updated > 0:
                logger.info(f"Urgency scores: {updated} atualizado(s)", extra=by_tier)

            # Alertar IMEDIATAMENTE sobre novos CRITICALs (tier changes)
            # Gate de autonomia: em modo MANUAL, nao envia alertas proativos
            from integrations.pipeline_config import pipeline_config
            is_manual = pipeline_config.get_autonomy_level() == "manual"

            tier_changes = result.get("tier_changes", [])
            new_critical = [
                c for c in tier_changes
                if c.get("new_tier") == "CRITICAL" and c.get("old_tier") != "CRITICAL"
            ]
            for c in new_critical:
                name = "?"
                try:
                    from database.supabase_client import db as _db
                    comp = _db.client.table("companies").select("name").eq(
                        "id", c["company_id"]
                    ).limit(1).execute()
                    if comp.data:
                        name = comp.data[0].get("name", "?")
                except Exception:
                    pass

                if not is_manual:
                    self._send_to_owner(
                        f"\U0001f534 *ALERTA URGENCIA CRITICA!*\n\n"
                        f"\U0001f3eb *{name}*\n"
                        f"\U0001f4c8 Score: {c.get('urgency_score', '?')}/100\n"
                        f"\u2b06\ufe0f Era {c.get('old_tier', '?')} → agora CRITICAL\n\n"
                        f"_Acao imediata recomendada. Pergunte 'proximas acoes' para ver sugestoes._"
                    )
                else:
                    logger.info(f"Urgency alert SUPRIMIDO (modo manual): {name} -> CRITICAL")

            # Notificar via dashboard
            if new_critical:
                try:
                    from tools.notification_manager import notification_manager
                    notification_manager.add_notification(
                        title=f"{len(new_critical)} lead(s) CRITICO(S)",
                        message=f"Novos leads atingiram urgencia CRITICAL. Verifique agora.",
                        notification_type="warning",
                        link="/Pipeline",
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.debug(f"Urgency scores skip: {e}")

    # ============================================================
    # AUTO-HEALING (F6 Fase 3A)
    # ============================================================

    def _auto_heal_system(self):
        """Roda health check e tenta remediar problemas automaticamente.

        Acoes seguras: restart da instancia Baileys, notificacoes ao Fernando.
        Acoes ambiguas ou perigosas: apenas notifica (sem agir).
        """
        try:
            from tools.health_check import run_health_check, auto_heal
            result = run_health_check()

            # So executa auto-heal se houver problemas
            if result.get("overall") not in ("degraded", "critical"):
                logger.debug("Auto-heal: sistema saudavel, nada a fazer")
                return

            heal_result = auto_heal(result)
            healed = heal_result.get("healed", [])
            notified = heal_result.get("notified", [])

            if healed or notified:
                logger.info(
                    f"Auto-heal: {len(healed)} remediado(s), {len(notified)} notificacoes",
                    extra={"healed": healed, "notified": notified},
                )
        except Exception as e:
            logger.debug(f"Auto-heal skip: {e}")

    def _send_urgency_digest(self):
        """Envia digest de urgencia diario as 08:15 (apos briefing matinal).
        Inclui leads por tier, tendencias e alertas de inatividade.

        Respeita autonomia: em modo MANUAL, nao envia digest proativo.
        """
        try:
            from integrations.pipeline_config import pipeline_config
            if pipeline_config.get_autonomy_level() == "manual":
                logger.debug("Urgency digest suprimido (modo manual)")
                return

            from tools.proactive_actions import proactive_engine

            parts: list = []

            # Digest principal
            digest = proactive_engine.generate_daily_digest()
            if digest:
                parts.append(digest)

            # Tendencias de tier
            trends = proactive_engine.detect_and_format_trends()
            if trends:
                parts.append(trends)

            # Inatividade
            from config.settings import settings as _s
            inactive = proactive_engine.detect_inactivity(days=_s.URGENCY_INACTIVITY_DAYS)
            inactivity_msg = proactive_engine.format_inactivity_for_whatsapp(inactive)
            if inactivity_msg:
                parts.append(inactivity_msg)

            if parts:
                full_message = "\n\n---\n\n".join(parts)
                self._send_to_owner(full_message)
                logger.info("Urgency digest enviado", extra={"parts": len(parts)})

        except Exception as e:
            logger.error(f"Erro no urgency digest: {e}")

    def _retrain_predictive_model(self):
        """Retreina o modelo preditivo semanalmente (domingo 03:00)."""
        try:
            from tools.predictive_scorer import predictive_scorer
            result = predictive_scorer.train()
            if result.get("trained"):
                logger.info(
                    f"Modelo preditivo retreinado: {result['samples']} amostras, "
                    f"{result['positives']} positivos, accuracy {result['accuracy']}"
                )
                # Notificar Fernando se houve melhoria significativa
                self._send_to_owner(
                    f"🤖 *Modelo preditivo retreinado*\n\n"
                    f"📊 {result['samples']} escolas analisadas\n"
                    f"🎯 {result['positives']} positivos (respostas/reunioes)\n"
                    f"✅ Accuracy: {result['accuracy']}\n\n"
                    f"O IAlex esta mais inteligente! Pergunte 'top oportunidades' para ver o novo ranking."
                )
            else:
                logger.info(f"Retreino nao realizado: {result.get('reason')}")
        except Exception as e:
            logger.error(f"Erro no retreino do modelo preditivo: {e}")

    # ============================================================
    # PRE-GERACAO DE ARTEFATOS (OPR + graficos) FORA DO CLOUD
    # ============================================================

    def _pregenerate_artifacts(self, full_refresh: bool = False):
        """Dispara a pre-geracao em THREAD (nao bloqueia o loop do scheduler).

        O lote pode levar ~1h no refresh completo; rodar inline travaria os
        outros jobs agendados. Por isso roda em daemon thread.
        """
        threading.Thread(
            target=self._pregenerate_artifacts_inner,
            args=(full_refresh,), daemon=True,
        ).start()

    def _pregenerate_artifacts_inner(self, full_refresh: bool):
        """Pre-gera OPR + graficos de insight (kaleido) FORA do Cloud.

        Roda onde o IAlex roda (PC/Oracle). O Streamlit Cloud nao tem kaleido,
        entao apenas CONSOME os artefatos prontos do Supabase. Manter este job
        rodando mantem o Cloud sempre fresco => equivalencia online/local.

        - full_refresh=False (noite, 04:00): so as escolas SEM OPR no Storage
          (novas importadas) — barato, cobre o dia-a-dia.
        - full_refresh=True (domingo, 04:30): refresh COMPLETO (dado anual muda).

        Gate por charts_renderable(): no-op em ambiente sem render (ex: Cloud).
        Silencioso (so logs) — manutencao, nao notifica o dono.
        """
        try:
            from tools.insight_charts import charts_renderable
            if not charts_renderable():
                logger.debug("Pregeneracao pulada (ambiente sem render de graficos)")
                return
            from scripts.pregenerate_artifacts import pregenerate_batch, _crm_ineps
            ineps = _crm_ineps()
            if not full_refresh:
                # so as escolas que ainda nao tem OPR no Storage (novas)
                try:
                    from database.supabase_client import db as _db
                    existing = _db.client.storage.from_("insight-charts").list(
                        "reports", {"limit": 2000}
                    ) or []
                    have = {
                        (f.get("name") or "").replace(".html", "")
                        for f in existing
                        if (f.get("name") or "").endswith(".html")
                    }
                    ineps = [i for i in ineps if i not in have]
                except Exception as e:
                    logger.debug(
                        f"Pregeneracao: list reports falhou ({e}); processando todas"
                    )
            if not ineps:
                logger.debug("Pregeneracao: nada a fazer (todas com artefato)")
                return
            # resume=False: o filtro "sem artefato" (noite) ou o refresh completo
            # (domingo) ja definem o conjunto exato a processar. Cada escola roda
            # isolada com timeout dentro do pregenerate_batch.
            stats = pregenerate_batch(ineps, resume=False)
            logger.info(
                "Pregeneracao de artefatos concluida",
                extra={**stats, "full_refresh": full_refresh},
            )
        except Exception as e:
            logger.error(f"Erro na pregeneracao de artefatos: {e}")

    # ============================================================
    # PIPELINE AUTOMATICO (Item 5)
    # ============================================================

    def _register_automated_pipeline(self):
        """Registra o job do pipeline automatico com base na config salva.
        Le a config via integrations.pipeline_config e agenda 1 job por dia
        habilitado no horario configurado. Silencioso se desabilitado.
        Gate de autonomia: nao registra se autonomy_level='manual'.
        """
        try:
            from integrations.pipeline_config import pipeline_config
            cfg = pipeline_config.get_config()
            if cfg.get("autonomy_level") == "manual":
                logger.info("Pipeline automatico BLOQUEADO (modo manual)")
                return
            if not cfg.get("enabled"):
                logger.info("Pipeline automatico desabilitado (nenhum job registrado)")
                return
            time_str = cfg.get("schedule_time", "08:00")
            days = cfg.get("days", [])
            day_map = {
                "mon": schedule.every().monday,
                "tue": schedule.every().tuesday,
                "wed": schedule.every().wednesday,
                "thu": schedule.every().thursday,
                "fri": schedule.every().friday,
                "sat": schedule.every().saturday,
                "sun": schedule.every().sunday,
            }
            registered = []
            for d in days:
                j = day_map.get(d)
                if not j:
                    continue
                j.at(time_str).do(self._run_automated_pipeline).tag(self.PIPELINE_TAG)
                registered.append(d)
            logger.info(
                "Pipeline automatico registrado",
                extra={"time": time_str, "days": registered, "steps": cfg.get("steps")},
            )
        except Exception as e:
            logger.error(f"Erro ao registrar pipeline automatico: {e}")

    def reload_pipeline_schedule(self):
        """Limpa jobs antigos do pipeline automatico e re-registra com config atual.
        Chamado pela UI quando Fernando salva nova configuracao.
        """
        try:
            schedule.clear(self.PIPELINE_TAG)
            logger.info("Jobs antigos do pipeline automatico removidos")
        except Exception as e:
            logger.debug(f"reload_pipeline_schedule clear: {e}")
        self._register_automated_pipeline()

    def _run_automated_pipeline(self, triggered_by: str = "schedule"):
        """Executa o pipeline automatico conforme config e envia resumo WhatsApp.
        Usa _run_background_with_feedback para garantir que Fernando sempre recebe retorno.
        """
        def _task():
            self.__run_pipeline_inner(triggered_by)

        self._run_background_with_feedback(
            task_name="Pipeline automatico",
            task_fn=_task,
            timeout_minutes=15,
        )

    def __run_pipeline_inner(self, triggered_by: str):
        """Logica interna do pipeline (chamada pelo wrapper com feedback)."""
        started = datetime.now()
        from integrations.pipeline_config import pipeline_config
        from workflows.daily_pipeline import run_pipeline

        cfg = pipeline_config.get_config()

        # Se foi disparado por schedule, validar dia atual
        if triggered_by == "schedule":
            today = pipeline_config.weekday_short_from_date(started)
            if today not in cfg.get("days", []):
                self._send_to_owner(
                    f"🤖 *Pipeline automatico*: hoje ({today}) nao esta nos dias configurados. Nada a fazer."
                )
                return

        # Gate de autonomia em runtime (defense in depth)
        autonomy = cfg.get("autonomy_level", "semi_auto")
        effective_send = cfg.get("send_approved", False) and autonomy == "full_auto"
        effective_steps = list(cfg.get("steps") or [])
        if autonomy != "full_auto" and "send" in effective_steps:
            effective_steps = [s for s in effective_steps if s != "send"]

        logger.info("Pipeline automatico INICIANDO", extra={
            "triggered_by": triggered_by,
            "autonomy_level": autonomy,
            "steps": effective_steps,
        })

        limits = cfg.get("limits", {})
        report = run_pipeline(
            qualify_limit=limits.get("qualify_limit", 20),
            enrich_limit=limits.get("enrich_limit", 10),
            write_limit=limits.get("write_limit", 10),
            send_approved=effective_send,
            dry_run=cfg.get("dry_run", False),
            write_mode=cfg.get("write_mode", "ai"),
            steps=effective_steps,
        )

        finished = datetime.now()
        duration_min = int((finished - started).total_seconds() // 60)

        message = self._format_pipeline_summary(
            cfg=cfg,
            report=report,
            started=started,
            finished=finished,
            duration_min=duration_min,
            triggered_by=triggered_by,
        )
        self._send_to_owner(message)

        pipeline_config.update_last_run(
            status="success",
            summary=report.get("summary", {}),
        )

    def _format_pipeline_summary(
        self,
        cfg: dict,
        report: dict,
        started: datetime,
        finished: datetime,
        duration_min: int,
        triggered_by: str,
    ) -> str:
        """Formata o resumo do pipeline para enviar no WhatsApp."""
        from integrations.pipeline_config import pipeline_config as pc

        steps_data = report.get("steps", {}) or {}
        summary = report.get("summary", {}) or {}
        qualified = steps_data.get("qualify", {}).get("output", 0) if "qualify" in steps_data else None
        enriched = steps_data.get("enrich", {}).get("output", 0) if "enrich" in steps_data else None
        contacts_found = steps_data.get("contacts", {}).get("output", 0) if "contacts" in steps_data else None
        written = steps_data.get("write", {}).get("output", 0) if "write" in steps_data else None
        sent = steps_data.get("send", {}).get("sent", 0) if "send" in steps_data else None

        # Fila pendente
        try:
            from database.supabase_client import db as _db
            q = _db.client.table("approval_queue").select("id", count="exact").eq(
                "status", "pending"
            ).execute()
            pending = q.count or 0
        except Exception:
            pending = None

        day_name = pc.day_label(pc.weekday_short_from_date(started))
        date_str = started.strftime("%d/%m")
        time_range = f"{started.strftime('%H:%M')}-{finished.strftime('%H:%M')}"

        header = (
            "🤖 *Pipeline automatico executado*"
            if triggered_by == "schedule"
            else "🤖 *Pipeline executado manualmente*"
        )

        lines = [
            header,
            f"📅 {day_name}, {date_str} — {time_range} ({duration_min} min)",
            "",
            "📊 *Resultado:*",
        ]
        if qualified is not None:
            lines.append(f"   ✅ Qualificadas: {qualified}")
        if enriched is not None:
            lines.append(f"   ✅ Enriquecidas: {enriched}")
        if contacts_found is not None:
            lines.append(f"   ✅ Contatos encontrados: {contacts_found}")
        if written is not None:
            lines.append(f"   ✅ Emails gerados: {written}")
        if sent is not None:
            lines.append(f"   ✅ Emails enviados: {sent}")

        if not any(v is not None and v > 0 for v in [qualified, enriched, contacts_found, written, sent]):
            lines.append("   _Nenhuma acao necessaria — tudo em dia._")

        lines.append("")
        if pending is not None:
            lines.append(f"📋 *Fila de aprovacao:* {pending} email(s) aguardando")
        lines.append("")
        lines.append("⚡ *Proximo passo:* revise a fila no dashboard")

        return "\n".join(lines)

    def run_pipeline_now(self) -> Dict[str, Any]:
        """Executa o pipeline automatico imediatamente (manual/teste).
        Respeita o autonomy_level: em modo manual, apenas GERA (nao envia),
        mesmo que o usuario clique no botao de teste.
        """
        try:
            from integrations.pipeline_config import pipeline_config
            lvl = pipeline_config.get_autonomy_level()
            if lvl == "manual":
                return {
                    "ok": False,
                    "reason": "autonomy_manual",
                    "message": (
                        "Modo MANUAL: execucao automatica bloqueada. "
                        "Mude para SEMI-AUTO ou FULL-AUTO em Configuracoes para rodar."
                    ),
                }
        except Exception:
            pass
        self._run_automated_pipeline(triggered_by="manual")
        return {"ok": True}

    # ============================================================
    # FOLLOW-UPS COMPORTAMENTAIS (Item 6)
    # ============================================================

    def _register_automated_followup(self):
        """Registra job diario de follow-ups comportamentais com base na config.
        Gate de autonomia: nao registra se autonomy_level='manual'.
        """
        try:
            from integrations.pipeline_config import pipeline_config
            cfg = pipeline_config.get_config()
            if cfg.get("autonomy_level") == "manual":
                logger.info("Follow-ups automaticos BLOQUEADOS (modo manual)")
                return
            if not cfg.get("followup_enabled", False):
                logger.info("Follow-ups automaticos desabilitados")
                return
            time_str = cfg.get("followup_time", "09:30")
            schedule.every().day.at(time_str).do(
                self._run_follow_up_generation
            ).tag(self.FOLLOWUP_TAG)
            logger.info("Follow-ups automaticos registrados", extra={
                "time": time_str,
                "limit": cfg.get("followup_limit", 20),
                "types": cfg.get("followup_types"),
            })
        except Exception as e:
            logger.error(f"Erro ao registrar follow-ups automaticos: {e}")

    def reload_followup_schedule(self):
        """Limpa jobs antigos de follow-up e re-registra."""
        try:
            schedule.clear(self.FOLLOWUP_TAG)
        except Exception:
            pass
        self._register_automated_followup()

    def _run_follow_up_generation(self, triggered_by: str = "schedule"):
        """Executa geracao de follow-ups comportamentais com feedback garantido."""
        def _task():
            self.__run_followup_inner(triggered_by)

        self._run_background_with_feedback(
            task_name="Follow-ups",
            task_fn=_task,
            timeout_minutes=10,
        )

    def __run_followup_inner(self, triggered_by: str):
        """Logica interna de follow-ups (chamada pelo wrapper com feedback)."""
        from workflows.follow_up_manager import run_follow_up_check
        from integrations.pipeline_config import pipeline_config

        cfg = pipeline_config.get_config()
        limit = cfg.get("followup_limit", 20)
        allowed = cfg.get("followup_types") or None

        logger.info("Follow-ups automaticos: INICIANDO", extra={
            "triggered_by": triggered_by,
            "limit": limit,
            "allowed_types": allowed,
        })

        result = run_follow_up_check(limit=limit, allowed_types=allowed)
        message = self._format_followup_summary(result, triggered_by)
        self._send_to_owner(message)

    def _format_followup_summary(self, result: dict, triggered_by: str) -> str:
        """Formata o resumo de follow-ups para WhatsApp."""
        generated = result.get("generated", 0)
        errors = result.get("errors", 0)
        due = result.get("due_found", 0)
        by_type = result.get("by_type", {}) or {}

        emoji_map = {
            "hot_click": "🔥",
            "curious_open": "👀",
            "silent_open": "📬",
            "revival": "🧊",
        }
        label_map = {
            "hot_click": "hot click (alta prioridade)",
            "curious_open": "curious open",
            "silent_open": "silent open",
            "revival": "revival",
        }

        header = (
            "🔄 *Follow-ups automaticos*"
            if triggered_by == "schedule"
            else "🔄 *Follow-ups (manual)*"
        )

        lines = [header]
        lines.append(f"📅 {datetime.now().strftime('%d/%m %H:%M')}")
        lines.append("")
        lines.append(f"📊 *Resumo:* {generated} gerados | {errors} erros | {due} detectados")

        if generated > 0:
            lines.append("")
            lines.append("*Por tipo comportamental:*")
            for t, n in by_type.items():
                if n > 0:
                    emoji = emoji_map.get(t, "•")
                    label = label_map.get(t, t)
                    lines.append(f"   {emoji} {label}: {n}")

        # Fila pendente total
        try:
            from database.supabase_client import db as _db
            q = _db.client.table("approval_queue").select("id", count="exact").eq(
                "status", "pending"
            ).execute()
            pending = q.count or 0
            lines.append("")
            lines.append(f"📋 *Fila de aprovacao:* {pending} email(s) aguardando")
        except Exception:
            pass

        if generated == 0 and due == 0:
            lines.append("")
            lines.append("_Nenhum lead esta pronto para follow-up agora._")
        elif generated > 0:
            lines.append("")
            lines.append("⚡ *Proximo passo:* revise na fila de aprovacao")

        return "\n".join(lines)

    def run_followup_now(self) -> Dict[str, Any]:
        """Executa geracao de follow-ups imediatamente (manual/teste).
        Follow-ups SEMPRE geram em status=pending, entao sao seguros em
        qualquer nivel de autonomia. Apenas bloqueia em modo 'manual' total.
        """
        try:
            from integrations.pipeline_config import pipeline_config
            if pipeline_config.get_autonomy_level() == "manual":
                return {
                    "ok": False,
                    "reason": "autonomy_manual",
                    "message": (
                        "Modo MANUAL: geracao automatica de follow-ups bloqueada. "
                        "Mude para SEMI-AUTO ou FULL-AUTO em Configuracoes."
                    ),
                }
        except Exception:
            pass
        self._run_follow_up_generation(triggered_by="manual")
        return {"ok": True}

    # === Metodos para executar manualmente ===

    def send_briefing_now(self):
        """Envia briefing agora (para testes)."""
        self._morning_briefing()

    def send_weekly_now(self):
        """Envia relatorio semanal agora (para testes)."""
        self._weekly_report()


# Singleton
ialex_scheduler = IALexScheduler()


if __name__ == "__main__":
    print("IAlex Scheduler - Modo teste")
    print("Enviando briefing matinal...")
    ialex_scheduler.send_briefing_now()
