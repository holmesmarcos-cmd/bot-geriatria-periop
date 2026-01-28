import os
import json
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
PORT = int(os.getenv("PORT", "10000"))

AGENDA_WORKSHEET_NAME = os.getenv("AGENDA_WORKSHEET_NAME", "AGENDA").strip()
AGENDA_MAX_OPTIONS = int(os.getenv("AGENDA_MAX_OPTIONS", "8"))

if not BOT_TOKEN or not SHEET_ID or not GOOGLE_CREDS_JSON:
    raise RuntimeError("Variáveis de ambiente obrigatórias ausentes.")

# =========================
# GOOGLE SHEETS
# =========================
def get_client():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS_JSON),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(creds)


def get_sheet(name):
    return get_client().open_by_key(SHEET_ID).worksheet(name)


def append_log(row: dict):
    ws = get_sheet(WORKSHEET_NAME)
    ws.append_row([
        row.get("timestamp", ""),
        row.get("caminho", ""),
        row.get("elegivel", ""),
        row.get("criterio", ""),
        row.get("nome", ""),
        row.get("prontuario", ""),
        row.get("cirurgiao", ""),
        row.get("cirurgia", ""),
        row.get("data_prevista", ""),
        row.get("observacoes", ""),
        row.get("telegram_id", ""),
        row.get("telegram_user", ""),
    ], value_input_option="USER_ENTERED")


# =========================
# AGENDA
# =========================
def find_slots():
    ws = get_sheet(AGENDA_WORKSHEET_NAME)
    values = ws.get_all_values()
    slots = []

    for r in range(1, len(values)):
        date = values[r][0] if values[r] else ""
        if not date:
            continue
        for c in range(1, 7):  # B–G
            if len(values[r]) <= c or values[r][c].strip() == "":
                slots.append({
                    "row": r + 1,
                    "col": c + 1,
                    "label": f"{date} – V{c}"
                })
                if len(slots) >= AGENDA_MAX_OPTIONS:
                    return slots
    return slots


def book_slot(row, col, text):
    ws = get_sheet(AGENDA_WORKSHEET_NAME)
    if ws.cell(row, col).value:
        return False
    ws.update_cell(row, col, text)
    return True


# =========================
# STATE
# =========================
def reset(ctx):
    ctx.user_data.clear()
    ctx.user_data["step"] = 0
    ctx.user_data["data"] = {}


# =========================
# UI
# =========================
def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("AVALIAR ELEGIBILIDADE", callback_data="ELIG")],
        [InlineKeyboardButton("FAZER AGENDAMENTO", callback_data="SCHED")],
    ])


def confirm_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("CONFIRMAR", callback_data="CONFIRM")],
        [InlineKeyboardButton("CANCELAR", callback_data="CANCEL")],
    ])


# =========================
# QUESTIONS
# =========================
FIELDS = [
    ("nome", "Nome do paciente:"),
    ("prontuario", "Prontuário:"),
    ("cirurgiao", "Nome do cirurgião:"),
    ("cirurgia", "Cirurgia proposta:"),
    ("data_prevista", "Data prevista da cirurgia:"),
    ("observacoes", "Observações / Recomendações (ou - ):"),
]


# =========================
# HANDLERS
# =========================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reset(ctx)
    await update.message.reply_text("Escolha uma opção:", reply_markup=menu())


async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["mode"] = q.data
    ctx.user_data["step"] = 0
    ctx.user_data["data"] = {}
    await q.edit_message_text(FIELDS[0][1])


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("mode") not in ["ELIG", "SCHED"]:
        await update.message.reply_text("Use /start.")
        return

    step = ctx.user_data.get("step", 0)
    if step >= len(FIELDS):
        return

    key, _ = FIELDS[step]
    ctx.user_data["data"][key] = update.message.text.strip()
    ctx.user_data["step"] += 1

    if ctx.user_data["step"] >= len(FIELDS):
        d = ctx.user_data["data"]
        resumo = (
            "📝 *CONFIRMAR SOLICITAÇÃO*\n\n"
            f"Paciente: {d['nome']}\n"
            f"Prontuário: {d['prontuario']}\n"
            f"Cirurgião: {d['cirurgiao']}\n"
            f"Cirurgia: {d['cirurgia']}\n"
            f"Data prevista: {d['data_prevista']}\n"
            f"Observações: {d['observacoes']}\n"
        )
        await update.message.reply_text(resumo, parse_mode="Markdown", reply_markup=confirm_kb())
        return

    await update.message.reply_text(FIELDS[ctx.user_data["step"]][1])


async def on_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "CANCEL":
        reset(ctx)
        await q.edit_message_text("Cancelado.")
        await q.message.reply_text("Menu:", reply_markup=menu())
        return

    data = ctx.user_data["data"]
    texto = (
        "📝 CONFIRMAR SOLICITAÇÃO\n"
        f"Paciente: {data['nome']}\n"
        f"Prontuário: {data['prontuario']}\n"
        f"Cirurgião: {data['cirurgiao']}\n"
        f"Cirurgia proposta: {data['cirurgia']}\n"
        f"Data prevista: {data['data_prevista']\n}"
        f"Observações: {data['observacoes']}"
    )

    ctx.user_data["booking_text"] = texto
    slots = find_slots()

    if not slots:
        await q.edit_message_text("Sem vagas disponíveis.")
        await q.message.reply_text("Menu:", reply_markup=menu())
        reset(ctx)
        return

    kb = [[InlineKeyboardButton(s["label"], callback_data=f"SLOT:{s['row']}:{s['col']}")] for s in slots]
    kb.append([InlineKeyboardButton("CANCELAR", callback_data="CANCEL")])
    await q.edit_message_text("Escolha uma vaga:", reply_markup=InlineKeyboardMarkup(kb))


async def on_slot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "CANCEL":
        reset(ctx)
        await q.edit_message_text("Cancelado.")
        await q.message.reply_text("Menu:", reply_markup=menu())
        return

    _, r, c = q.data.split(":")
    ok = book_slot(int(r), int(c), ctx.user_data["booking_text"])

    if not ok:
        await q.edit_message_text("Vaga ocupada. Escolha outra.")
        return

    u = q.from_user
    d = ctx.user_data["data"]
    append_log({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "caminho": ctx.user_data.get("mode"),
        "elegivel": "SIM",
        "criterio": "",
        "nome": d["nome"],
        "prontuario": d["prontuario"],
        "cirurgiao": d["cirurgiao"],
        "cirurgia": d["cirurgia"],
        "data_prevista": d["data_prevista"],
        "observacoes": d["observacoes"],
        "telegram_id": u.id,
        "telegram_user": u.username or "",
    })

    await q.edit_message_text("✅ Agendamento realizado.")
    await q.message.reply_text("Menu:", reply_markup=menu())
    reset(ctx)


def build_app():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_menu, pattern="^(ELIG|SCHED)$"))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern="^(CONFIRM|CANCEL)$"))
    app.add_handler(CallbackQueryHandler(on_slot, pattern="^SLOT:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


if __name__ == "__main__":
    app = build_app()
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{RENDER_EXTERNAL_URL.rstrip('/')}/{BOT_TOKEN}",
        drop_pending_updates=True,
    )
