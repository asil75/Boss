import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot_worker")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [
            InlineKeyboardButton(
                "Mini App очиш",
                web_app=WebAppInfo(url=settings.mini_app_url),
            )
        ]
    ]
    await update.message.reply_text(
        "Botim Delivery Mini App га хуш келибсиз.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def main() -> None:
    if not settings.bot_token:
        raise RuntimeError("BOTIM_BOT_TOKEN созланмаган")
    application = ApplicationBuilder().token(settings.bot_token).build()
    application.add_handler(CommandHandler("start", start))
    logger.info("Bot worker started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
