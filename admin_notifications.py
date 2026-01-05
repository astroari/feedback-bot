"""Admin notification functionality for feedback bot."""
from aiogram import Bot
from datetime import datetime
from typing import Optional, List, Tuple
from aiogram.types import FSInputFile
import os
import logging
from dotenv import load_dotenv


load_dotenv()

# Get admin chat ID from environment variable
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))


async def send_to_admin(
    bot: Bot,
    feedback_id: int,
    text: str,
    branch: Optional[str] = None,
    user_name: Optional[str] = None,
    phone: Optional[str] = None,
    file_paths: Optional[List[Tuple[str, str]]] = None
) -> None:
    """Send feedback notification to admin channel"""
    
    # Check if admin chat ID is configured
    if not ADMIN_CHAT_ID:
        logging.warning("ADMIN_CHAT_ID not configured, skipping admin notification")
        return
    
    try:
        # Convert ADMIN_CHAT_ID to int if it's a string
        chat_id = int(ADMIN_CHAT_ID) if isinstance(ADMIN_CHAT_ID, str) else ADMIN_CHAT_ID
        
        # Format the message
        message = f"""
📝 <b>Новое обращение</b>

🏢 <b>Филиал:</b> {branch or '❌ Не указан'}
👤 <b>От:</b> {user_name or '🔒 Анонимно'}
📞 <b>Телефон:</b> {phone or '❌ Не указан'}

💬 <b>Сообщение:</b>
{text}

🆔 ID: #{feedback_id}
📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}
        """
        
        # Send text
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode="HTML"
        )
        
        # Send files if any
        if file_paths:
            for file_path, _ in file_paths:  # file_paths is List[Tuple[str, str]]
                try:
                    file = FSInputFile(file_path)
                    await bot.send_document(
                        chat_id=chat_id,
                        document=file
                    )
                except Exception as e:
                    logging.error(f"Error sending file {file_path}: {e}")
    except Exception as e:
        logging.error(f"Error sending admin notification: {e}")

