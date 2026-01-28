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

if not BOT_TOKEN or not SHEET_ID or not GOOGLE_CREDS_JSON:
    raise RuntimeError("Variáveis de ambiente obrigatórias ausentes (BOT_TOKEN, SHEET_ID, GOOGLE_CREDS_JSON).")

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

def _today_local() -> date:
    # Usa timezone local configurado no ambiente (Render costuma ficar OK com astimezone()).
    return datetime.now(timezone.utc).astimezone().date()

def parse_data_prevista(s: str):
    """
    Interpreta:
    - yyyy-mm-dd
    - dd/mm/yyyy
    - dd/mm/yy
    - mm/yyyy (assume dia 01)
    - "abril/2026" (assume dia 01)
    - "abril 2026" (assume dia 01)
    Retorna date ou None.
    """
    if not s:
        return None

    raw = s.strip().lower()

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except Exception:
            pass

    # mm/yyyy
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
        row.get("slot_escolhido", ""),
        row.get("telegram_id", ""),
        row.get("telegram_user", ""),
    ], value_input_option="USER_ENTERED")

# =========================
# AGENDA (A=Data, B-G=6 vagas)
# =========================
def find_slots():
    ws = get_sheet(AGENDA_WORKSHEET_NAME)
    values = ws.get_all_values()
    slots = []

    # pula cabeçalho (linha 1)
    for r in range(1, len(values)):
        row_vals = values[r] if values[r] else []
        date_str = row_vals[0] if len(row_vals) > 0 else ""
        if not date_str:
            continue

        # B–G => índices 1..6 (0-based)
        for c in range(1, 7):
            cell_val = row_vals[c] if len(row_vals) > c else ""
            if (cell_val or "").strip() == "":
                slots.append({
                    "row": r + 1,        # 1-based
                    "col": c + 1,        # 1-based
                    "label": f"{date_str} – V{c}",
                    "date": date_str,
                    "slot": f"V{c}",
                })
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
    ctx.user_data["mode"] = None         # "elig" | "sched"
    ctx.user_data["elig_step"] = 0
    ctx.user_data["eligible"] = None     # "SIM" | "NAO"
    ctx.user_data["criterio"] = ""       # qual pergunta deu SIM
    ctx.user_data["step"] = 0            # passo do agendamento
    ctx.user_data["data"] = {}           # dados do agendamento
    ctx.user_data["booking_text"] = ""   # texto que vai para a célula
    ctx.user_data["slots_cache"] = []    # slots mostrados

# =========================
# UI
# =========================
def menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("AVALIAR ELEGIBILIDADE", callback_data="MENU:ELIG"),
            InlineKeyboardButton("FAZER AGENDAMENTO", callback_data="MENU:SCHED"),
        ]
    ])

def yesno(prefix: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Sim", callback_data=f"{prefix}:SIM"),
            InlineKeyboardButton("Não", callback_data=f"{prefix}:NAO"),
        ]
    ])

def confirm_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("CONFIRMAR", callback_data="CONFIRM:SIM"),
            InlineKeyboardButton("CANCELAR", callback_data="CONFIRM:NAO"),
        ]
    ])

# =========================
# FLUXO (imagem)
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

# Agendamento (SEM prioridade, como você decidiu)
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
    await update.message.reply_text("Escolha uma opção:", reply_markup=menu())

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
            reply_markup=yesno(f"ELIG:{key}")
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

    # callback: ELIG:<key>:SIM|NAO
    _, key, ans = q.data.split(":")

    if ans == "SIM":
        # elegível -> entra em agendamento
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

    # ans == NAO -> próxima pergunta
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
        reply_markup=yesno(f"ELIG:{next_key}")
    )

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("mode") != "sched":
        await update.message.reply_text("Use /start para abrir o menu.")
        return

    step = int(ctx.user_data.get("step", 0))
    if step >= len(FIELDS):
        return

    field, _prompt = FIELDS[step]
    value = (update.message.text or "").strip()
    if not value:
        await update.message.reply_text("Não entendi. Tente novamente.")
        return

    # bloqueio: data prevista no passado (quando interpretável)
    if field == "data_prevista":
        parsed = parse_data_prevista(value)
        if parsed is not None and parsed < _today_local():
            await update.message.reply_text(
                "⚠️ A data informada parece estar no passado.\n"
                "Digite uma data futura (ex: 03/03/2026) ou uma previsão (ex: Abril/2026):"
            )
            return

    ctx.user_data["data"][field] = value
    step += 1
    ctx.user_data["step"] = step

    # terminou perguntas -> resumo + confirmação
    if step >= len(FIELDS):
        d = ctx.user_data.get("data", {})
        resumo = (
            "📝 *CONFIRMAR SOLICITAÇÃO*\n\n"
            f"Paciente: {d.get('nome','')}\n"
            f"Prontuário: {d.get('prontuario','')}\n"
            f"Cirurgião: {d.get('cirurgiao','')}\n"
            f"Cirurgia proposta: {d.get('cirurgia','')}\n"
            f"Data prevista: {d.get('data_prevista','')}\n"
            f"Observações: {d.get('observacoes','')}\n\n"
            "Deseja confirmar e escolher uma vaga na agenda?"
        )
        await update.message.reply_text(resumo, parse_mode="Markdown", reply_markup=confirm_kb())
        return

    # próxima pergunta
    await update.message.reply_text(FIELDS[step][1])

async def on_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, ans = q.data.split(":")

    if ans == "NAO":
        await q.edit_message_text("Solicitação cancelada.")
        await q.message.reply_text("Menu:", reply_markup=menu())
        reset(ctx)
        return

    # CONFIRMAR -> monta texto único e mostra vagas
    d = ctx.user_data.get("data", {})
    texto = (
        "📝 CONFIRMAR SOLICITAÇÃO\n"
        f"Paciente: {d.get('nome','')}\n"
        f"Prontuário: {d.get('prontuario','')}\n"
        f"Cirurgião: {d.get('cirurgiao','')}\n"
        f"Cirurgia proposta: {d.get('cirurgia','')}\n"
        f"Data prevista: {d.get('data_prevista','')}\n"
        f"Observações: {d.get('observacoes','')}\n"
    )
    ctx.user_data["booking_text"] = texto

    slots = find_slots()
    if not slots:
        await q.edit_message_text("⚠️ Sem vagas disponíveis no momento.")
        await q.message.reply_text("Menu:", reply_markup=menu())
        reset(ctx)
        return

    ctx.user_data["slots_cache"] = slots

    kb = []
    for s in slots:
        kb.append([InlineKeyboardButton(s["label"], callback_data=f"SLOT:{s['row']}:{s['col']}")])
    kb.append([InlineKeyboardButton("CANCELAR", callback_data="SLOT:CANCEL")])

    await q.edit_message_text("Escolha uma vaga:", reply_markup=InlineKeyboardMarkup(kb))

async def on_slot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "SLOT:CANCEL":
        await q.edit_message_text("Cancelado.")
        await q.message.reply_text("Menu:", reply_markup=menu())
        reset(ctx)
        return

    _, r, c = q.data.split(":")
    r_i, c_i = int(r), int(c)

    ok = book_slot(r_i, c_i, ctx.user_data.get("booking_text", ""))
    if not ok:
        await q.edit_message_text("Vaga ocupada. Use /start e tente novamente para ver vagas atualizadas.")
        return

    # identifica label do slot escolhido (para log)
    slot_label = ""
    for s in (ctx.user_data.get("slots_cache") or []):
        if s.get("row") == r_i and s.get("col") == c_i:
            slot_label = s.get("label", "")
            break

    # log em SOLICITACOES
    u = q.from_user
    d = ctx.user_data.get("data", {})
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    append_log({
        "timestamp": now,
        "caminho": "avaliacao_eligibilidade" if ctx.user_data.get("criterio") not in ("", "agendamento_direto") else "agendamento_direto",
        "elegivel": ctx.user_data.get("eligible") or "SIM",
        "criterio": ctx.user_data.get("criterio") or "",
        "nome": d.get("nome", ""),
        "prontuario": d.get("prontuario", ""),
        "cirurgiao": d.get("cirurgiao", ""),
        "cirurgia": d.get("cirurgia", ""),
        "data_prevista": d.get("data_prevista", ""),
        "observacoes": d.get("observacoes", ""),
        "slot_escolhido": slot_label,
        "telegram_id": str(u.id),
        "telegram_user": u.username or "",
    })

    await q.edit_message_text(
        "✅ Agendamento realizado.\n\n"
        "Solicitação registrada e vaga preenchida na agenda."
    )
    await q.message.reply_text("Menu:", reply_markup=menu())
    reset(ctx)

def build_app():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^MENU:"))
    app.add_handler(CallbackQueryHandler(on_elig, pattern=r"^ELIG:"))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^CONFIRM:"))
    app.add_handler(CallbackQueryHandler(on_slot, pattern=r"^SLOT:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app

if __name__ == "__main__":
    app = build_app()
    if not RENDER_EXTERNAL_URL:
        raise RuntimeError("Faltou RENDER_EXTERNAL_URL nas variáveis de ambiente do Render.")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{RENDER_EXTERNAL_URL.rstrip('/')}/{BOT_TOKEN}",
        drop_pending_updates=True,
    )
