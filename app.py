import os
import json
from datetime import datetime, timezone, date

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

# Webhook fixo
WEBHOOK_PATH = "webhook"

if not BOT_TOKEN or not SHEET_ID or not GOOGLE_CREDS_JSON:
    raise RuntimeError(
        "Variáveis de ambiente obrigatórias ausentes (BOT_TOKEN, SHEET_ID, GOOGLE_CREDS_JSON)."
    )

# =========================
# HELPERS (DATA)
# =========================
_PT_MONTHS = {
    "janeiro": 1, "jan": 1,
    "fevereiro": 2, "fev": 2,
    "março": 3, "marco": 3, "mar": 3,
    "abril": 4, "abr": 4,
    "maio": 5, "mai": 5,
    "junho": 6, "jun": 6,
    "julho": 7, "jul": 7,
    "agosto": 8, "ago": 8,
    "setembro": 9, "set": 9,
    "outubro": 10, "out": 10,
    "novembro": 11, "nov": 11,
    "dezembro": 12, "dez": 12,
}

# Mapeia coluna B..G (V1..V6) para horários
SLOT_TIMES = {
    1: "08:00",
    2: "08:45",
    3: "09:30",
    4: "10:15",
    5: "11:00",
    6: "11:45",
}

def _today_local() -> date:
    return datetime.now(timezone.utc).astimezone().date()

def parse_data_prevista(s: str):
    if not s:
        return None

    raw = s.strip().lower()

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except Exception:
            pass

    # tenta "mm/YYYY" interpretando como 01/mm/YYYY
    try:
        return datetime.strptime("01/" + raw, "%d/%m/%Y").date()
    except Exception:
        pass

    cleaned = raw.replace("-", "/").replace("  ", " ")
    if "/" in cleaned:
        parts = [p.strip() for p in cleaned.split("/") if p.strip()]
        if len(parts) == 2:
            m_txt, y_txt = parts
            if m_txt in _PT_MONTHS:
                try:
                    return date(int(y_txt), _PT_MONTHS[m_txt], 1)
                except Exception:
                    return None

    parts = cleaned.split()
    if len(parts) == 2:
        m_txt, y_txt = parts[0].strip(), parts[1].strip()
        if m_txt in _PT_MONTHS:
            try:
                return date(int(y_txt), _PT_MONTHS[m_txt], 1)
            except Exception:
                return None

    return None

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
    ws.append_row(
        [
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
            row.get("slot_escolhido", ""),
            row.get("telegram_id", ""),
            row.get("telegram_user", ""),
        ],
        value_input_option="USER_ENTERED",
    )

# =========================
# AGENDA (A=Data, B-G=6 vagas)
# =========================
def find_slots():
    ws = get_sheet(AGENDA_WORKSHEET_NAME)
    values = ws.get_all_values()
    slots = []

    for r in range(1, len(values)):  # pula cabeçalho
        row_vals = values[r] if values[r] else []
        date_str = row_vals[0] if len(row_vals) > 0 else ""
        if not date_str:
            continue

        # B–G => índices 1..6 (0-based)
        for c in range(1, 7):
            cell_val = row_vals[c] if len(row_vals) > c else ""
            if (cell_val or "").strip() == "":
                hora = SLOT_TIMES.get(c, f"V{c}")
                slots.append(
                    {
                        "row": r + 1,  # 1-based
                        "col": c + 1,  # 1-based
                        "label": f"{date_str} – {hora}",
                        "date": date_str,
                        "slot": f"V{c}",
                        "time": hora,
                    }
                )
                if len(slots) >= AGENDA_MAX_OPTIONS:
                    return slots

    return slots

def book_slot(row, col, text):
    ws = get_sheet(AGENDA_WORKSHEET_NAME)
    current = ws.cell(row, col).value
    if current and str(current).strip() != "":
        return False
    ws.update_cell(row, col, text)
    return True

# =========================
# STATE
# =========================
def reset(ctx):
    ctx.user_data.clear()
    ctx.user_data["mode"] = None
    ctx.user_data["elig_step"] = 0
    ctx.user_data["eligible"] = None
    ctx.user_data["criterio"] = ""
    ctx.user_data["step"] = 0
    ctx.user_data["data"] = {}
    ctx.user_data["booking_text"] = ""
    ctx.user_data["slots_cache"] = []

# =========================
# UI
# =========================
def menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("AVALIAR ELEGIBILIDADE", callback_data="MENU:ELIG"),
                InlineKeyboardButton("FAZER AGENDAMENTO", callback_data="MENU:SCHED"),
            ]
        ]
    )

def yesno(prefix: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Sim", callback_data=f"{prefix}:SIM"),
                InlineKeyboardButton("Não", callback_data=f"{prefix}:NAO"),
            ]
        ]
    )

def confirm_kb():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("CONFIRMAR", callback_data="CONFIRM:SIM"),
                InlineKeyboardButton("CANCELAR", callback_data="CONFIRM:NAO"),
            ]
        ]
    )

def build_slots_kb(slots):
    kb = []
    for s in slots:
        kb.append([InlineKeyboardButton(s["label"], callback_data=f"SLOT:{s['row']}:{s['col']}")])
    kb.append([InlineKeyboardButton("CANCELAR", callback_data="SLOT:CANCEL")])
    return InlineKeyboardMarkup(kb)

# =========================
# FLUXO (imagem)
# =========================
ELIG_QUESTIONS = [
    ("idade80", "Paciente ≥ 80 anos?"),
    (
        "memoria",
        "Paciente tem problemas de memória?\n"
        "- incapacidade para atividades do dia a dia por questões de memória\n"
        "- não reconhece familiares\n"
        "- não sabe dizer qual dia/mês/ano está",
    ),
    (
        "humor",
        "Paciente tem transtornos de humor?\n"
        "- uso de antidepressivos\n"
        "- labilidade emocional importante\n"
        "- insônia ou alterações de comportamento",
    ),
    (
        "multimorbidade",
        "Paciente possui 5 ou mais doenças sistêmicas?\n"
        "Ex: HAS, DM, insuficiência cardíaca, DAC, DRC, doença hepática crônica, AVE",
    ),
    ("polifarmacia", "Paciente faz uso de 5 ou mais medicamentos regularmente?"),
    (
        "fragilidade",
        "Paciente com fragilidade (CFS ≥ 4) OU baixa tolerância a esforço?\n"
        "Ex: cansa ao andar 1 quadra ou subir 1 lance de escadas (10 degraus), mobilidade reduzida/lentificada",
    ),
]

FIELDS = [
    ("nome", "Nome do paciente:"),
    ("prontuario", "Prontuário:"),
    ("cirurgiao", "Nome do cirurgião:"),
    ("cirurgia", "Cirurgia proposta:"),
    ("data_prevista", "Qual a data da cirurgia (ou expectativa aproximada)?\nEx: 03/03/2026 ou Abril/2026"),
    ("observacoes", "Observações / Recomendações (se não houver, digite: - )"),
]

# =========================
# HANDLERS
# =========================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reset(ctx)
    await update.message.reply_text(
        "Olá, sou um bot para agendamento de consulta no Ambulatório de Geriatria PeriOp "
        "(uso exclusivo de cirurgiões).\n\n"
        "Clique abaixo no que deseja fazer agora:",
        reply_markup=menu(),
    )

async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "MENU:ELIG":
        ctx.user_data["mode"] = "elig"
        ctx.user_data["elig_step"] = 0
        key, question = ELIG_QUESTIONS[0]
        await q.edit_message_text(
            f"AVALIAR ELEGIBILIDADE\n\n{question}",
            reply_markup=yesno(f"ELIG:{key}"),
        )
        return

    if data == "MENU:SCHED":
        ctx.user_data["mode"] = "sched"
        ctx.user_data["eligible"] = "SIM"
        ctx.user_data["criterio"] = "agendamento_direto"
        ctx.user_data["step"] = 0
        ctx.user_data["data"] = {}
        await q.edit_message_text(f"FAZER AGENDAMENTO\n\n{FIELDS[0][1]}")
        return

async def on_elig(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, key, ans = q.data.split(":")

    if ans == "SIM":
        ctx.user_data["eligible"] = "SIM"
        ctx.user_data["criterio"] = key
        ctx.user_data["mode"] = "sched"
        ctx.user_data["step"] = 0
        ctx.user_data["data"] = {}

        await q.edit_message_text(
            "✅ Paciente ELEGÍVEL para avaliação geriátrica perioperatória.\n\n"
            f"Critério positivo: {key}\n\n"
            f"FAZER AGENDAMENTO\n\n{FIELDS[0][1]}"
        )
        return

    step = int(ctx.user_data.get("elig_step", 0)) + 1
    ctx.user_data["elig_step"] = step

    if step >= len(ELIG_QUESTIONS):
        ctx.user_data["eligible"] = "NAO"
        ctx.user_data["criterio"] = "nenhum"
        await q.edit_message_text(
            "❌ PACIENTE NÃO ELEGÍVEL pelos critérios do bot.\n\n"
            "Se ainda houver dúvida clínica, considere discutir o caso com a equipe de geriatria."
        )
        await q.message.reply_text("Menu:", reply_markup=menu())
        reset(ctx)
        return

    next_key, next_q = ELIG_QUESTIONS[step]
    await q.edit_message_text(
        f"AVALIAR ELEGIBILIDADE\n\n{next_q}",
        reply_markup=yesno(f"ELIG:{next_key}"),
    )

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TY
