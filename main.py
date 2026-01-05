import asyncio
import logging
import os
from datetime import datetime
from hashlib import md5
from pathlib import Path
from typing import List, Tuple, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, BaseFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram import types
from typing import Dict, List



from dotenv import load_dotenv
import aiofiles

from database import init_db, close_db, save_feedback_to_db, check_rate_limit, update_user_submission_time

BOT_TOKEN_ENV = "BOT_TOKEN"
FEEDBACK_FILE = "feedback.txt"
FILES_DIR = Path("user_files")

# Temporary storage for pending feedback (key: callback_query_id, value: feedback_text)
pending_feedback = {}

# Temporary storage for media groups (key: media_group_id, value: list of messages)
# media_groups: dict[str, List[Message]] = {}
media_groups: Dict[str, List[Message]] = {}

# Track which media groups are already being processed to avoid duplicates
processing_media_groups: set[str] = set()


class BranchCallback(CallbackData, prefix="branch"):
    """Callback data structure for branch selection."""
    branch: str


class FeedbackCallback(CallbackData, prefix="feedback"):
    """Callback data structure for feedback actions."""
    action: str
    value: str  # feedback hash


class FileAttachmentCallback(CallbackData, prefix="file_attach"):
    """Callback data structure for file attachment actions."""
    action: str  # "yes", "no", "done", "add_more"
    feedback_hash: str


class WaitingForDetailsFilter(BaseFilter):
    """Filter to check if we're waiting for details from the user."""
    async def __call__(self, message: Message) -> bool:
        if not message.text or message.text.startswith('/'):
            return False
        user_id = message.from_user.id
        waiting_name_key = f"waiting_name:{user_id}"
        waiting_phone_key = f"waiting_phone:{user_id}"
        return waiting_name_key in pending_feedback or waiting_phone_key in pending_feedback

def get_branch_keyboard():
    """Create inline keyboard for branch selection."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Chinobod",
            callback_data=BranchCallback(branch="Chinobod").pack()
        )],
        [InlineKeyboardButton(
            text="Jomiy",
            callback_data=BranchCallback(branch="Jomiy").pack()
        )],
        [InlineKeyboardButton(
            text="Chilonzor",
            callback_data=BranchCallback(branch="Chilonzor").pack()
        )],
        [InlineKeyboardButton(
            text="Qorasuv",
            callback_data=BranchCallback(branch="Qorasuv").pack()
        )],
        [InlineKeyboardButton(
            text="Центральный офис",
            callback_data=BranchCallback(branch="Central office").pack()
        )],
        [InlineKeyboardButton(
            text="Не указывать",
            callback_data=BranchCallback(branch="Not specified").pack()
        )]
    ])


async def handle_start(message: Message) -> None:
    """Handle /start command - show welcome message."""
    await message.answer(
        "Dobryj den'! Добро пожаловать в бота для обратной связи! 👋\n\n"
        "Я здесь, чтобы помочь вам отправить претензии, пожелания, предложения по улучшению, идеи, жалобы, комментарии по процессам, условиям труда, управлению, коммуникациям, любые другие мысли и обращения. Используйте /new, чтобы начать создание нового отзыва.\n\n"
        "Ваша обратная связь помогает нам улучшаться, и вы можете остаться анонимным или добавить свои данные."
    )


async def handle_new(message: Message) -> None:
    """Handle /new command - ask for branch selection."""
    user_id = message.from_user.id
    
    # Check rate limiting
    can_submit, last_submission = await check_rate_limit(user_id)
    if not can_submit:
        time_passed = (datetime.now() - last_submission.replace(tzinfo=None) if last_submission.tzinfo else datetime.now() - last_submission).total_seconds()
        seconds_left = max(0, int(30 - time_passed))
        await message.answer(
            f"⏰ Вы уже отправили отзыв недавно. Пожалуйста, подождите еще {seconds_left} секунд(ы) перед повторной отправкой."
        )
        return
    
    # Clear any existing waiting states for this user
    keys_to_remove = [
        f"waiting_feedback:{user_id}",
        f"waiting_files:{user_id}",
        f"waiting_name:{user_id}",
        f"waiting_phone:{user_id}",
        f"branch:{user_id}",
        f"user_name:{user_id}",
        f"files:{user_id}"
    ]
    for key in keys_to_remove:
        pending_feedback.pop(key, None)
    
    await message.answer(
        "Ваше обращение полностью анонимно и конфиденциально.\n\n"
        "Вы можете указать свои контактные данные или имя по желанию — это необязательно.\n\n"
        "Сначала, пожалуйста, выберите ваш филиал:",
        reply_markup=get_branch_keyboard()
    )


def get_feedback_keyboard(feedback_hash: str):
    """Create inline keyboard for feedback options."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="➕ Добавить мои данные", 
            callback_data=FeedbackCallback(action="add_details", value=feedback_hash).pack()
        )],
        [InlineKeyboardButton(
            text="🔒 Остаться анонимным", 
            callback_data=FeedbackCallback(action="keep_anonymous", value=feedback_hash).pack()
        )]
    ])


async def handle_branch_selection(callback: CallbackQuery, callback_data: BranchCallback) -> None:
    """Handle branch selection and ask for feedback message."""
    branch = callback_data.branch
    user_id = callback.from_user.id
    
    # Store branch and mark that we're waiting for feedback
    pending_feedback[f"branch:{user_id}"] = branch
    pending_feedback[f"waiting_feedback:{user_id}"] = True
    
    await callback.message.edit_text(
        f"✅ Филиал выбран: {branch}\n\n"
        "Теперь, пожалуйста, напишите ваш отзыв:"
    )
    await callback.answer()


async def download_file(bot: Bot, file_id: str, file_type: str, user_id: int) -> Optional[str]:
    """Download a file from Telegram and save it locally."""
    try:
        # Create files directory if it doesn't exist
        FILES_DIR.mkdir(exist_ok=True)
        
        # Get file info
        file = await bot.get_file(file_id)
        
        # Determine file extension
        file_path_obj = Path(file.file_path)
        extension = file_path_obj.suffix or (".jpg" if file_type == "photo" else ".bin")
        
        # Create unique filename using file_id to ensure uniqueness
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")  # Include microseconds
        filename = f"{user_id}_{timestamp}_{file_id[:12]}{extension}"
        local_path = FILES_DIR / filename
        
        # Download file
        await bot.download_file(file.file_path, local_path)
        
        return str(local_path)
    except Exception as e:
        logging.error(f"Error downloading file: {e}")
        return None


async def handle_feedback(message: Message) -> None:
    """Handle feedback submission and ask about file attachments."""
    user_id = message.from_user.id
    waiting_feedback_key = f"waiting_feedback:{user_id}"
    waiting_files_key = f"waiting_files:{user_id}"
    
    # Check if we're waiting for files (file attachment mode)
    if waiting_files_key in pending_feedback:
        # User is in file attachment mode, handle file uploads
        await handle_file_upload(message, user_id)
        return
    
    # Check if we're waiting for feedback (after branch selection)
    if waiting_feedback_key not in pending_feedback:
        return  # Not in feedback flow, ignore
    
    # Get feedback text
    feedback_text = message.text or message.caption or ""
    
    # Skip if it's a command
    if feedback_text.startswith('/'):
        return
    
    # If no text, ask for text
    if not feedback_text:
        await message.answer(
            "Пожалуйста, отправьте ваш отзыв текстовым сообщением."
        )
        return
    
    # If we have text, store it and ask about file attachments
    if feedback_text:
        # Remove waiting flag
        pending_feedback.pop(waiting_feedback_key, None)
        branch_key = f"branch:{user_id}"
        branch = pending_feedback.pop(branch_key, "Unknown")
        
        # Store feedback temporarily using hash (include branch in the stored data)
        feedback_hash = md5(f"{feedback_text}:{branch}:{user_id}:{datetime.now()}".encode()).hexdigest()
        pending_feedback[feedback_hash] = {
            "text": feedback_text,
            "branch": branch,
            "files": []
        }
        
        # Ask if user wants to attach files
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Да",
                callback_data=FileAttachmentCallback(action="yes", feedback_hash=feedback_hash).pack()
            )],
            [InlineKeyboardButton(
                text="Нет",
                callback_data=FileAttachmentCallback(action="no", feedback_hash=feedback_hash).pack()
            )]
        ])
        
        await message.answer(
            "✅ Спасибо за ваш отзыв!\n\n"
            "Хотите прикрепить изображения или файлы?",
            reply_markup=keyboard
        )


async def save_feedback(
    feedback_text: str,
    branch: str,
    user_id: int,
    name: Optional[str] = None,
    phone: Optional[str] = None,
    file_paths: Optional[List[Tuple[str, str]]] = None
) -> None:
    """Save feedback to database and optionally to file."""
    # Save to database
    try:
        feedback_id = await save_feedback_to_db(
            message=feedback_text,
            branch=branch,
            name=name,
            phone=phone,
            file_paths=file_paths or []
        )
        
        # Update user submission time for rate limiting
        await update_user_submission_time(user_id)
        
        logging.info(f"Feedback saved to database with ID: {feedback_id}")
    except Exception as e:
        logging.error(f"Error saving feedback to database: {e}")
        raise
    
    # Also save to file for backup
    # try:
    #     timestamp = datetime.now().isoformat()
    #     feedback_entry = f"[{timestamp}] Branch: {branch}\n{feedback_text}\n"
    #     if name:
    #         feedback_entry += f"Name: {name}\n"
    #     if phone:
    #         feedback_entry += f"Phone: {phone}\n"
    #     if file_paths:
    #         feedback_entry += f"Files: {len(file_paths)} attached\n"
    #     feedback_entry += "\n"
    #     
    #     async with aiofiles.open(FEEDBACK_FILE, mode='a') as f:
    #         await f.write(feedback_entry)
    # except Exception as e:
    #     logging.warning(f"Error saving feedback to file: {e}")


async def handle_file_attachment_yes(callback: CallbackQuery, callback_data: FileAttachmentCallback) -> None:
    """Handle user selecting 'yes' to attach files."""
    feedback_hash = callback_data.feedback_hash
    user_id = callback.from_user.id
    
    # Mark that we're waiting for files
    pending_feedback[f"waiting_files:{user_id}"] = feedback_hash
    
    await callback.message.edit_text(
        "📎 Пожалуйста, отправьте ваши изображения или файлы. Вы можете отправить несколько файлов.\n\n"
        "После того, как закончите, используйте кнопки ниже, чтобы продолжить."
    )
    await callback.answer()


async def handle_file_attachment_no(callback: CallbackQuery, callback_data: FileAttachmentCallback) -> None:
    """Handle user selecting 'no' to skip file attachments."""
    feedback_hash = callback_data.feedback_hash
    feedback_data = pending_feedback.get(feedback_hash)
    
    if not feedback_data:
        await callback.answer("Отзыв не найден. Пожалуйста, отправьте снова.", show_alert=True)
        return
    
    # Proceed directly to anonymity question
    keyboard = get_feedback_keyboard(feedback_hash)
    
    await callback.message.edit_text(
        "✅ Спасибо за ваш отзыв!\n\n"
        "Хотите добавить ваши данные или оставить отправку анонимной?",
        reply_markup=keyboard
    )
    await callback.answer()


async def process_media_group(media_group_id: str, user_id: int, feedback_hash: str) -> None:
    """Process all messages in a media group after collecting them."""
    # Check if already processing this group
    if media_group_id in processing_media_groups:
        return
    
    # Mark as processing
    processing_media_groups.add(media_group_id)
    
    try:
        # Get messages and remove from tracking (to avoid processing twice)
        messages = media_groups.pop(media_group_id, None)
        if not messages:
            return
        
        feedback_data = pending_feedback.get(feedback_hash)
        if not feedback_data:
            return
        
        # Get existing files
        existing_files = feedback_data.get("files", [])
        
        # Process all files from the media group, avoiding duplicates within the group
        file_paths: List[Tuple[str, str]] = []
        processed_file_ids = set()  # Track file_ids processed in this batch to avoid duplicates
        
        for msg in messages:
            if msg.photo:
                # Get the largest photo
                photo = msg.photo[-1]
                # Skip if we've already processed this file_id in this batch
                if photo.file_id not in processed_file_ids:
                    file_path = await download_file(msg.bot, photo.file_id, "photo", user_id)
                    if file_path:
                        file_paths.append((file_path, "photo"))
                        processed_file_ids.add(photo.file_id)
            
            if msg.document:
                # Skip if we've already processed this file_id in this batch
                if msg.document.file_id not in processed_file_ids:
                    file_path = await download_file(msg.bot, msg.document.file_id, "document", user_id)
                    if file_path:
                        file_paths.append((file_path, "document"))
                        processed_file_ids.add(msg.document.file_id)
        
        if not file_paths:
            return
        
        # Add files to feedback data, avoiding duplicate file paths
        existing_file_paths = {file_path for file_path, _ in existing_files}
        new_files = [(fp, ft) for fp, ft in file_paths if fp not in existing_file_paths]
        
        if new_files:
            existing_files.extend(new_files)
            feedback_data["files"] = existing_files
            pending_feedback[feedback_hash] = feedback_data
        
        # Show confirmation buttons (only once for the group)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Готово, продолжить",
                callback_data=FileAttachmentCallback(action="done", feedback_hash=feedback_hash).pack()
            )],
            [InlineKeyboardButton(
                text="Добавить еще файлы",
                callback_data=FileAttachmentCallback(action="add_more", feedback_hash=feedback_hash).pack()
            )]
        ])
        
        file_count = len(existing_files)
        # Send confirmation to the last message in the group
        await messages[-1].answer(
            f"✅ Файлы получены! Всего файлов: {file_count}\n\n"
            "Что вы хотите сделать?",
            reply_markup=keyboard
        )
    finally:
        # Remove from processing set
        processing_media_groups.discard(media_group_id)


async def handle_file_upload(message: Message, user_id: int) -> None:
    """Handle file uploads when user is in file attachment mode."""
    waiting_files_key = f"waiting_files:{user_id}"
    feedback_hash = pending_feedback.get(waiting_files_key)
    
    if not feedback_hash:
        return
    
    feedback_data = pending_feedback.get(feedback_hash)
    if not feedback_data:
        await message.answer("Отзыв не найден. Пожалуйста, отправьте снова.")
        pending_feedback.pop(waiting_files_key, None)
        return
    
    # Check if this is part of a media group (album)
    if message.media_group_id is not None:
        media_group_id = str(message.media_group_id)  # Ensure it's a string for dict key
        
        # Add message to media group collection
        is_first_message = media_group_id not in media_groups
        if is_first_message:
            media_groups[media_group_id] = []
            # Schedule processing after a short delay to collect all messages
            # Only schedule once per media group
            async def delayed_process():
                await asyncio.sleep(1.5)  # Wait 1.5 seconds for all messages in group
                await process_media_group(media_group_id, user_id, feedback_hash)
            asyncio.create_task(delayed_process())
        
        # Only add message if it's not already in the list (avoid duplicates by message_id)
        existing_message_ids = {msg.message_id for msg in media_groups[media_group_id]}
        if message.message_id not in existing_message_ids:
            media_groups[media_group_id].append(message)
        
        return  # Don't process individually, wait for the group
    
    # Handle single file (not part of media group)
    file_paths: List[Tuple[str, str]] = []
    
    if message.photo:
        # Get the largest photo
        photo = message.photo[-1]
        file_path = await download_file(message.bot, photo.file_id, "photo", user_id)
        if file_path:
            file_paths.append((file_path, "photo"))
    
    if message.document:
        file_path = await download_file(message.bot, message.document.file_id, "document", user_id)
        if file_path:
            file_paths.append((file_path, "document"))
    
    if not file_paths:
        await message.answer("Пожалуйста, отправьте изображение или файл.")
        return
    
    # Add files to feedback data
    existing_files = feedback_data.get("files", [])
    existing_files.extend(file_paths)
    feedback_data["files"] = existing_files
    pending_feedback[feedback_hash] = feedback_data
    
    # Show confirmation buttons
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Готово, продолжить",
            callback_data=FileAttachmentCallback(action="done", feedback_hash=feedback_hash).pack()
        )],
        [InlineKeyboardButton(
            text="Добавить еще файлы",
            callback_data=FileAttachmentCallback(action="add_more", feedback_hash=feedback_hash).pack()
        )]
    ])
    
    file_count = len(existing_files)
    await message.answer(
        f"✅ Файл получен! Всего файлов: {file_count}\n\n"
        "Что вы хотите сделать?",
        reply_markup=keyboard
    )


async def handle_file_done(callback: CallbackQuery, callback_data: FileAttachmentCallback) -> None:
    """Handle user selecting 'Done, continue' after file uploads."""
    feedback_hash = callback_data.feedback_hash
    user_id = callback.from_user.id
    
    # Remove waiting for files flag
    pending_feedback.pop(f"waiting_files:{user_id}", None)
    
    feedback_data = pending_feedback.get(feedback_hash)
    if not feedback_data:
        await callback.answer("Отзыв не найден. Пожалуйста, отправьте снова.", show_alert=True)
        return
    
    # Proceed to anonymity question
    keyboard = get_feedback_keyboard(feedback_hash)
    
    await callback.message.edit_text(
        "✅ Спасибо за ваш отзыв!\n\n"
        "Хотите добавить ваши данные или оставить отправку анонимной?",
        reply_markup=keyboard
    )
    await callback.answer()


async def handle_file_add_more(callback: CallbackQuery, callback_data: FileAttachmentCallback) -> None:
    """Handle user selecting 'Add more files'."""
    feedback_hash = callback_data.feedback_hash
    user_id = callback.from_user.id
    
    # Keep waiting for files flag
    pending_feedback[f"waiting_files:{user_id}"] = feedback_hash
    
    feedback_data = pending_feedback.get(feedback_hash)
    file_count = len(feedback_data.get("files", [])) if feedback_data else 0
    
    await callback.message.edit_text(
        f"📎 Пожалуйста, отправьте еще изображения или файлы. Текущих файлов: {file_count}\n\n"
        "После того, как закончите, используйте кнопки ниже, чтобы продолжить."
    )
    await callback.answer()


async def handle_keep_anonymous(callback: CallbackQuery, callback_data: FeedbackCallback) -> None:
    """Handle anonymous feedback submission."""
    feedback_hash = callback_data.value
    feedback_data = pending_feedback.pop(feedback_hash, None)
    
    if not feedback_data:
        await callback.answer("Отзыв не найден. Пожалуйста, отправьте снова.", show_alert=True)
        return
    
    feedback_text = feedback_data.get("text") if isinstance(feedback_data, dict) else feedback_data
    branch = feedback_data.get("branch", "Unknown") if isinstance(feedback_data, dict) else "Unknown"
    file_paths = feedback_data.get("files", []) if isinstance(feedback_data, dict) else []
    user_id = callback.from_user.id
    
    try:
        await save_feedback(feedback_text, branch, user_id, name=None, phone=None, file_paths=file_paths)
        await callback.message.edit_text(
            "✅ Ваш отзыв был сохранен анонимно.\n\n"
            "Спасибо за вашу отправку!"
        )
    except Exception as e:
        logging.error(f"Error saving feedback: {e}")
        await callback.answer("Ошибка при сохранении отзыва. Пожалуйста, попробуйте снова.", show_alert=True)


async def handle_add_details(callback: CallbackQuery, callback_data: FeedbackCallback) -> None:
    """Handle request to add details."""
    feedback_hash = callback_data.value
    feedback_data = pending_feedback.get(feedback_hash)
    
    if not feedback_data:
        await callback.answer("Отзыв не найден. Пожалуйста, отправьте снова.", show_alert=True)
        return
    
    # Store the hash in pending_feedback with a special marker to indicate we're waiting for name
    pending_feedback[f"waiting_name:{callback.from_user.id}"] = feedback_hash
    
    await callback.message.edit_text(
        "Пожалуйста, укажите ваше имя:"
    )
    await callback.answer()


async def handle_details_submission(message: Message) -> None:
    """Handle user details submission (name first, then phone number)."""
    user_id = message.from_user.id
    waiting_name_key = f"waiting_name:{user_id}"
    waiting_phone_key = f"waiting_phone:{user_id}"
    
    # Check if we're waiting for name
    if waiting_name_key in pending_feedback:
        feedback_hash = pending_feedback.pop(waiting_name_key)
        feedback_data = pending_feedback.get(feedback_hash)
        name = message.text.strip()
        
        if not feedback_data:
            await message.answer("Отзыв не найден. Пожалуйста, отправьте снова.")
            return
        
        # Store name and feedback_hash, then ask for phone number
        pending_feedback[f"user_name:{user_id}"] = name
        pending_feedback[f"waiting_phone:{user_id}"] = feedback_hash
        
        await message.answer("Спасибо! Теперь, пожалуйста, укажите ваш номер телефона:")
        return
    
    # Check if we're waiting for phone number
    if waiting_phone_key in pending_feedback:
        feedback_hash = pending_feedback.pop(waiting_phone_key)
        feedback_data = pending_feedback.pop(feedback_hash, None)
        name_key = f"user_name:{user_id}"
        name = pending_feedback.pop(name_key, "Unknown")
        phone = message.text.strip()
        
        if not feedback_data:
            await message.answer("Отзыв не найден. Пожалуйста, отправьте снова.")
            return
        
        # Extract feedback text and branch
        feedback_text = feedback_data.get("text") if isinstance(feedback_data, dict) else feedback_data
        branch = feedback_data.get("branch", "Unknown") if isinstance(feedback_data, dict) else "Unknown"
        file_paths = feedback_data.get("files", []) if isinstance(feedback_data, dict) else []
        
        try:
            await save_feedback(feedback_text, branch, user_id, name=name, phone=phone, file_paths=file_paths)
            await message.answer(
                "✅ Ваш отзыв был сохранен с вашими данными.\n\n"
                "Спасибо за вашу отправку!"
            )
        except Exception as e:
            logging.error(f"Error saving feedback: {e}")
            await message.answer(
                "❌ Извините, произошла ошибка при сохранении вашего отзыва. Пожалуйста, попробуйте позже."
            )


async def main() -> None: 
    logging.basicConfig(level=logging.INFO)
    load_dotenv()

    token = os.getenv(BOT_TOKEN_ENV)
    if not token:
       raise RuntimeError("BOT_TOKEN is not set")

    # Initialize database
    try:
        await init_db()
    except Exception as e:
        logging.error(f"Failed to initialize database: {e}")
        logging.warning("Bot will continue but database features may not work")

    bot = Bot(token=token)
    dp = Dispatcher()
    
    # Register handlers - order matters!
    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_new, Command("new"))
    
    # Register callback handlers first (they have specific filters)
    dp.callback_query.register(handle_branch_selection, BranchCallback.filter())
    dp.callback_query.register(handle_file_attachment_yes, FileAttachmentCallback.filter(F.action == "yes"))
    dp.callback_query.register(handle_file_attachment_no, FileAttachmentCallback.filter(F.action == "no"))
    dp.callback_query.register(handle_file_done, FileAttachmentCallback.filter(F.action == "done"))
    dp.callback_query.register(handle_file_add_more, FileAttachmentCallback.filter(F.action == "add_more"))
    dp.callback_query.register(handle_keep_anonymous, FeedbackCallback.filter(F.action == "keep_anonymous"))
    dp.callback_query.register(handle_add_details, FeedbackCallback.filter(F.action == "add_details"))
    
    # Register message handlers - details submission with filter, then feedback
    dp.message.register(handle_details_submission, WaitingForDetailsFilter())  # Only when waiting for details
    dp.message.register(handle_feedback)  # Handle regular feedback

    try:
        await dp.start_polling(bot)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())