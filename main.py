import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from dotenv import load_dotenv
import aiofiles

BOT_TOKEN_ENV = "BOT_TOKEN"
FEEDBACK_FILE = "feedback.txt"

async def handle_start(message: Message) -> None:
    await message.answer(
        "👋 Welcome! I'm here to collect your anonymous feedback.\n\n"
        "Please share your thoughts, suggestions, or any feedback you'd like to submit. "
        "Your feedback will be completely anonymous.\n\n"
        "Just type your message and send it to me!"
    )


async def handle_feedback(message: Message) -> None:
    """Handle anonymous feedback submission."""
    feedback_text = message.text
    
    # Skip if it's a command
    if feedback_text.startswith('/'):
        return
    
    # Store feedback anonymously (without user info)
    timestamp = datetime.now().isoformat()
    feedback_entry = f"[{timestamp}] {feedback_text}\n"
    
    try:
        async with aiofiles.open(FEEDBACK_FILE, mode='a') as f:
            await f.write(feedback_entry)
        
        await message.answer(
            "✅ Thank you for your feedback! Your submission has been received anonymously.\n\n"
            "Feel free to submit more feedback anytime!"
        )
    except Exception as e:
        logging.error(f"Error saving feedback: {e}")
        await message.answer(
            "❌ Sorry, there was an error saving your feedback. Please try again later."
        )


async def main() -> None: 
    logging.basicConfig(level=logging.INFO)
    load_dotenv()

    token = os.getenv(BOT_TOKEN_ENV)
    if not token:
       raise RuntimeError("BOT_TOKEN is not set")

    bot = Bot(token=token)
    dp = Dispatcher()
    dp.message.register(handle_start, CommandStart())

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())