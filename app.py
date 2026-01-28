import os
import json
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SHEET_ID = os.getenv("SHEET_ID", "").strip()
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "SOLICITACOES").strip()

GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON", "").strip()

PORT = int(os.getenv("PORT", "10000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()  # Render geralmente injeta isso

if not BOT_TOKEN:
    raise RuntimeError("Faltou BOT_TOKEN nas variáveis de ambiente.")
if not SHEET_ID:
    raise RuntimeError("Faltou SHEET_ID nas variáveis de ambiente.")
if not GOOGLE_CREDS_JSON:
    raise RuntimeError("Faltou GOOGLE_CREDS_JSON nas variáveis de ambiente.")


# =========================
# GOOGLE SHEETS CLIENT
# =========================
def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(WORKSHEET_NAME)
    return ws


def append_row_to_sheet(row: dict):
    ws = get_sheet()
    ordered = [
        row.get("timestamp", ""),
        row.get("caminho", ""),
        row.get("elegivel", ""),
        row.get("criterio_positivo", ""),
        row.get("nome_paciente", ""),
        row.get("prontuario", ""),
        row.get("nome_cirurgiao", ""),
        row.get("cirurgia_proposta", ""),
        row.get("data_cirurgia_prevista", ""),
        row.get("prioridade", ""),
        row.get("observacoes", ""),
        row.get("telegram_user_id", ""),
        row.get("telegram_username", ""),
    ]
    ws.append_row(ordered, value_input_option="USER_ENTERED")


# =========================
# STATES (simples via context.user_data)
# =========================
def reset_flow(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["mode"] = None
    context.user_data["elig_step"] = None
    context.user_data["eligible"] = None
    context.user_data["criterion"] = None
    context.user_data["sched_step"] = None
    context.user_data["sched"] = {}


# =========================
# KEYBOARDS
# =========================
def main_menu_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("AVALIAR ELEGIBILIDADE", callback_data="MENU_ELIG"),
            InlineKeyboardButton("FAZER AGENDAMENTO", callback_data="MENU_SCHED"),
        ]
    ])


def yes_no_kb(prefix: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Sim", callback_data=f"{prefix}:SIM"),
            InlineKeyboardButton("Não", callback_data=f"{prefix}:NAO"),
        ]
    ])


def priority_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Baixa (6–12 semanas)", callback_data="PRIO:BAIXA")],
        [InlineKeyboardButton("Média (4–6 semanas)", callback_data="PRIO:MEDIA")],
        [InlineKeyboardButton("Alta (2–4 semanas)", callback_data="PRIO:ALTA")],
        [InlineKeyboardButton("Muito alta (≤2 semanas)", callback_data="PRIO:MUITO_ALTA")],
    ])


def confirm_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("CONFIRMAR", callback_data="CONFIRM:SIM"),
            InlineKeyboardButton("CANCELAR", callback_data="CONFIRM:NAO"),
        ]
    ])


# =========================
# MESSAGES (fluxo da imagem)
# =========================
ELIG_QUESTIONS = [
    ("idade80", "Paciente ≥ 80 anos?"),
    ("memoria", "Paciente tem problemas de memória?\n"
               "- incapacidade para atividades do dia a dia por questões de memória\n"
               "- não reconhece familiares\n"
               "- não sabe dizer qual dia/mês/ano está"),
    ("humor", "Paciente tem transtornos de humor?\n"
             "- uso de antidepressivos\n"
             "- labilidade emocional importante\n"
             "- insônia ou alterações de comportamento"),
    ("multimorbidade", "Paciente possui 5 ou mais doenças sistêmicas?\n"
                       "Ex: HAS, DM, insuficiência cardíaca, DAC, DRC, doença hepática crônica, AVE"),
    ("polifarmacia", "Paciente faz uso de 5 ou mais medicamentos regularmente?"),
    ("fragilidade", "Paciente com fragilidade (CFS ≥ 4) OU baixa tolerância a esforço?\n"
                    "Ex: cansa ao andar 1 quadra ou subir 1 lance de escadas (10 degraus), mobilidade reduzida/lentificada"),
]


SCHED_FIELDS = [
    ("nome_paciente", "Nome do paciente:"),
    ("prontuario", "Prontuário:"),
    ("nome_cirurgiao", "Nome do cirurgião:"),
    ("cirurgia_proposta", "Cirurgia proposta:"),
    ("data_cirurgia_prevista", "Qual a data da cirurgia (ou expectativa aproximada)?"),
    ("prioridade", "Prioridade para marcação no ambulatório de Geriatria Periop:"),
    ("observacoes", "Observações / Recomendações (se não houver, digite: - )"),
]


# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_flow(context)
    await update.message.reply_text(
        "Olá! Escolha uma opção:",
        reply_markup=main_menu_kb()
    )


async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "MENU_ELIG":
        context.user_data["mode"] = "elig"
        context.user_data["elig_step"] = 0
        context.user_data["eligible"] = None
        context.user_data["criterion"] = None

        key, question = ELIG_QUESTIONS[0]
        await query.edit_message_text(
            f"AVALIAR ELEGIBILIDADE\n\n{question}",
            reply_markup=yes_no_kb(f"ELIG:{key}")
        )
        return

    if data == "MENU_SCHED":
        # agendamento direto (sem passar por elegibilidade)
        context.user_data["mode"] = "sched"
        context.user_data["eligible"] = "SIM"
        context.user_data["criterion"] = "agendamento_direto"
        context.user_data["sched_step"] = 0
        context.user_data["sched"] = {}

        _, prompt = SCHED_FIELDS[0]
        await query.edit_message_text(
            f"FAZER AGENDAMENTO\n\n{prompt}"
        )
        return


async def on_elig_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # callback_data: ELIG:<key>:SIM|NAO
    _, key, ans = query.data.split(":")

    if ans == "SIM":
        context.user_data["eligible"] = "SIM"
        context.user_data["criterion"] = key

        # entra em agendamento
        context.user_data["mode"] = "sched"
        context.user_data["sched_step"] = 0
        context.user_data["sched"] = {}

        _, prompt = SCHED_FIELDS[0]
        await query.edit_message_text(
            "✅ Paciente ELEGÍVEL para avaliação geriátrica perioperatória.\n\n"
            f"Critério positivo: {key}\n\n"
            f"FAZER AGENDAMENTO\n\n{prompt}"
        )
        return

    # ans == NAO -> próxima pergunta ou não elegível
    step = context.user_data.get("elig_step", 0)
    step += 1
    context.user_data["elig_step"] = step

    if step >= len(ELIG_QUESTIONS):
        context.user_data["eligible"] = "NAO"
        context.user_data["criterion"] = "nenhum"

        await query.edit_message_text(
            "❌ PACIENTE NÃO ELEGÍVEL pelos critérios do bot.\n\n"
            "Se ainda houver dúvida clínica, considere discutir o caso com a equipe de geriatria."
        )
        await query.message.reply_text("Menu:", reply_markup=main_menu_kb())
        return

    next_key, next_q = ELIG_QUESTIONS[step]
    await query.edit_message_text(
        f"AVALIAR ELEGIBILIDADE\n\n{next_q}",
        reply_markup=yes_no_kb(f"ELIG:{next_key}")
    )


async def on_priority(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, prio = query.data.split(":")

    # salva prioridade
    context.user_data["sched"]["prioridade"] = prio

    # avançar para observações (texto)
    idx = [i for i, (f, _) in enumerate(SCHED_FIELDS) if f == "prioridade"][0]
    context.user_data["sched_step"] = idx + 1

    _, next_prompt = SCHED_FIELDS[idx + 1]
    await query.edit_message_text(next_prompt)


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, ans = query.data.split(":")
    if ans == "NAO":
        await query.edit_message_text("Solicitação cancelada.")
        await query.message.reply_text("Menu:", reply_markup=main_menu_kb())
        reset_flow(context)
        return

    sched = context.user_data.get("sched", {})
    user = query.from_user

    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    caminho = "avaliacao_eligibilidade"
    criterio = context.user_data.get("criterion") or "nenhum"
    if criterio == "agendamento_direto":
        caminho = "agendamento_direto"

    row = {
        "timestamp": now,
        "caminho": caminho,
        "elegivel": context.user_data.get("eligible") or "SIM",
        "criterio_positivo": criterio,
        "nome_paciente": sched.get("nome_paciente", ""),
        "prontuario": sched.get("prontuario", ""),
        "nome_cirurgiao": sched.get("nome_cirurgiao", ""),
        "cirurgia_proposta": sched.get("cirurgia_proposta", ""),
        "data_cirurgia_prevista": sched.get("data_cirurgia_prevista", ""),
        "prioridade": sched.get("prioridade", ""),
        "observacoes": sched.get("observacoes", ""),
        "telegram_user_id": str(user.id),
        "telegram_username": user.username or "",
    }

    try:
        append_row_to_sheet(row)
        await query.edit_message_text(
            "✅ Solicitação enviada.\n\n"
            "Orientar o paciente a procurar o setor de marcação."
        )
    except Exception as e:
        await query.edit_message_text(
            "⚠️ Erro ao enviar para o Google Sheets.\n"
            "Verifique as credenciais e o compartilhamento da planilha.\n\n"
            f"Detalhe: {type(e).__name__}"
        )

    await query.message.reply_text("Menu:", reply_markup=main_menu_kb())
    reset_flow(context)


async def on_text_message_with_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("mode") != "sched":
        await update.message.reply_text("Digite /start para abrir o menu.")
        return

    step = context.user_data.get("sched_step", 0)

    # Guard: evita IndexError se o usuário digitar depois de finalizar os campos
    if step is None:
        context.user_data["sched_step"] = 0
        step = 0

    if step >= len(SCHED_FIELDS):
        await update.message.reply_text(
            "Você já chegou na confirmação. Use os botões abaixo para confirmar ou cancelar.",
            reply_markup=confirm_kb()
        )
        return

    field, prompt = SCHED_FIELDS[step]

    # PRIORIDADE é tratada por botão
    if field == "prioridade":
        await update.message.reply_text(
            "Escolha a prioridade pelos botões abaixo:",
            reply_markup=priority_kb()
        )
        return

    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("Não entendi. Tente novamente.")
        return

    # Salva valor
    context.user_data["sched"][field] = value

    # Avança etapa
    step += 1
    context.user_data["sched_step"] = step

    # Terminou todos os campos → resumo + confirmação
    if step >= len(SCHED_FIELDS):
        sched = context.user_data.get("sched", {})

        resumo = (
            "📝 *CONFIRMAR SOLICITAÇÃO*\n\n"
            f"*Paciente:* {sched.get('nome_paciente','')}\n"
            f"*Prontuário:* {sched.get('prontuario','')}\n"
            f"*Cirurgião:* {sched.get('nome_cirurgiao','')}\n"
            f"*Cirurgia proposta:* {sched.get('cirurgia_proposta','')}\n"
            f"*Data prevista:* {sched.get('data_cirurgia_prevista','')}\n"
            f"*Prioridade:* {sched.get('prioridade','')}\n"
            f"*Observações:* {sched.get('observacoes','')}\n\n"
            "Deseja confirmar o envio para o ambulatório de Geriatria Perioperatória?"
        )

        await update.message.reply_text(
            resumo,
            parse_mode="Markdown",
            reply_markup=confirm_kb()
        )
        return

    next_field, next_prompt = SCHED_FIELDS[step]

    if next_field == "prioridade":
        await update.message.reply_text(
            next_prompt,
            reply_markup=priority_kb()
        )
        return

    await update.message.reply_text(next_prompt)


def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(CallbackQueryHandler(on_menu_click, pattern=r"^MENU_"))
    app.add_handler(CallbackQueryHandler(on_elig_answer, pattern=r"^ELIG:"))
    app.add_handler(CallbackQueryHandler(on_priority, pattern=r"^PRIO:"))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^CONFIRM:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message_with_finish))

    return app


if __name__ == "__main__":
    application = build_app()

    if not RENDER_EXTERNAL_URL:
        raise RuntimeError("Faltou RENDER_EXTERNAL_URL nas variáveis de ambiente do Render.")

    webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/{BOT_TOKEN}"

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=webhook_url,
        drop_pending_updates=True,
    )
