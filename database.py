"""Database operations for feedback bot using asyncpg."""
import asyncio
import logging
import os
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Optional, List, Tuple

import asyncpg


# Database connection pool
db: Optional[asyncpg.Pool] = None


def hash_user_id(telegram_user_id: int) -> str:
    """Hash telegram user ID for privacy-preserving spam check."""
    return sha256(str(telegram_user_id).encode()).hexdigest()


async def init_db() -> None:
    """Initialize database connection pool."""
    global db
    
    # Get database connection string from environment
    # Format: postgresql://user:password@host:port/database
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        # Fallback to individual environment variables
        db_host = os.getenv("DB_HOST", "localhost")
        db_port = int(os.getenv("DB_PORT", "5432"))
        db_user = os.getenv("DB_USER", "postgres")
        db_password = os.getenv("DB_PASSWORD", "")
        db_name = os.getenv("DB_NAME", "postgres")
        
        # Build connection parameters for asyncpg
        db = await asyncpg.create_pool(
            host=db_host,
            port=db_port,
            user=db_user,
            password=db_password,
            database=db_name,
            min_size=1,
            max_size=10
        )
        logging.info(f"Connecting to database: {db_user}@{db_host}:{db_port}/{db_name}")
    else:
        # Parse DATABASE_URL for asyncpg
        # asyncpg expects postgresql:// format
        db = await asyncpg.create_pool(db_url, min_size=1, max_size=10)
        # Log connection info (without password)
        if "@" in db_url and "/" in db_url:
            parts = db_url.split("@")
            if len(parts) == 2:
                db_info = parts[1].split("/")
                if len(db_info) == 2:
                    logging.info(f"Connecting to database via DATABASE_URL: {db_info[0]}/{db_info[1]}")
    
    try:
        # Test the connection and verify tables exist
        async with db.acquire() as conn:
            await conn.fetchval("SELECT 1")
            
            # Check current database and schema
            current_db = await conn.fetchval("SELECT current_database()")
            current_schema = await conn.fetchval("SELECT current_schema()")
            logging.info(f"Connected to database: {current_db}, schema: {current_schema}")
            
            # Verify feedback table exists
            table_exists = await conn.fetchval("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_schema = $1 
                    AND table_name = 'feedback'
                )
            """, current_schema)
            
            if not table_exists:
                logging.error(f"Table 'feedback' not found in schema '{current_schema}' of database '{current_db}'")
                logging.error("Please verify that:")
                logging.error("  1. You're connected to the correct database")
                logging.error("  2. The tables exist in the current schema")
                logging.error("  3. The user has permissions to access the tables")
            else:
                logging.info("Database connection established and tables verified")
    except Exception as e:
        logging.error(f"Failed to connect to database: {e}")
        raise


async def close_db() -> None:
    """Close database connection pool."""
    global db
    if db:
        await db.close()
        db = None
        logging.info("Database connection closed")


async def check_rate_limit(user_id: int, cooldown_seconds: int = 30) -> Tuple[bool, Optional[datetime]]:
    """
    Check if user can submit feedback based on rate limiting.
    
    Args:
        user_id: Telegram user ID
        cooldown_seconds: Seconds to wait between submissions (default: 30)
    
    Returns:
        Tuple of (can_submit, last_submission_time)
    """
    if not db:
        return True, None
    
    user_id_hash = hash_user_id(user_id)
    
    try:
        async with db.acquire() as conn:
            result = await conn.fetchrow(
                "SELECT last_submission_time FROM feedback_submissions WHERE user_id_hash = $1",
                user_id_hash
            )
        
        if not result:
            # First submission, allow it
            return True, None
        
        last_submission = result["last_submission_time"]
        if isinstance(last_submission, str):
            last_submission = datetime.fromisoformat(last_submission.replace('Z', '+00:00'))
        elif not isinstance(last_submission, datetime):
            last_submission = datetime.now()
        
        time_since_submission = datetime.now() - last_submission.replace(tzinfo=None) if last_submission.tzinfo else datetime.now() - last_submission
        
        if time_since_submission < timedelta(seconds=cooldown_seconds):
            return False, last_submission
        
        return True, last_submission
    except Exception as e:
        logging.error(f"Error checking rate limit: {e}")
        # On error, allow submission
        return True, None


async def save_feedback_to_db(
    message: str,
    branch: str,
    name: Optional[str] = None,
    phone: Optional[str] = None,
    file_paths: Optional[List[Tuple[str, str]]] = None
) -> int:
    """
    Save feedback to database.
    
    Args:
        message: Feedback message text
        branch: Selected branch
        name: Optional user name
        phone: Optional user phone number
        file_paths: Optional list of (file_path, file_type) tuples
    
    Returns:
        feedback_id: The ID of the created feedback record
    """
    if not db:
        raise RuntimeError("Database not initialized")
    
    try:
        async with db.acquire() as conn:
            # Use a transaction to ensure atomicity and handle concurrent submissions
            async with conn.transaction():
                # Insert feedback
                feedback_id = await conn.fetchval(
                    """
                    INSERT INTO feedback (message, branch, name, phone, created_at)
                    VALUES ($1, $2, $3, $4, $5)
                    RETURNING id
                    """,
                    message,
                    branch,
                    name,
                    phone,
                    datetime.now()
                )
                
                # Insert file records if any
                if file_paths:
                    for file_path, file_type in file_paths:
                        await conn.execute(
                            """
                            INSERT INTO feedback_files (feedback_id, file_path, file_type, created_at)
                            VALUES ($1, $2, $3, $4)
                            """,
                            feedback_id,
                            file_path,
                            file_type,
                            datetime.now()
                        )
        
        return feedback_id
    except Exception as e:
        logging.error(f"Error saving feedback to database: {e}")
        raise


async def update_user_submission_time(user_id: int) -> None:
    """Update the last submission time for a user."""
    if not db:
        return
    
    user_id_hash = hash_user_id(user_id)
    
    try:
        async with db.acquire() as conn:
            # Use a transaction to ensure atomicity for concurrent updates
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO feedback_submissions (user_id_hash, last_submission_time)
                    VALUES ($1, $2)
                    ON CONFLICT (user_id_hash) 
                    DO UPDATE SET last_submission_time = $2
                    """,
                    user_id_hash,
                    datetime.now()
                )
    except Exception as e:
        logging.error(f"Error updating user submission time: {e}")

