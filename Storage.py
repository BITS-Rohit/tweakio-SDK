"""SQL LITE Storage management"""
import asyncio
import sqlite3
from typing import List, Dict, Optional

from MessageProcessor import Message
from Shared_Resources import logger


class Storage:
    """Handles the DB queries with async queue-based writes"""
    def __init__(self, db_path: str = "tweakio.db"):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self.queue: asyncio.Queue[List[Message]] = asyncio.Queue()
        self._writer_task: Optional[asyncio.Task] = None
        self._init_db()
        self._start_writer()

    def _init_db(self):
        """Initialize the database connection and create tables if they don't exist."""
        try:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            # Enable WAL mode for better concurrency
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA busy_timeout=3000;")
            self._create_tables()
            logger.info(f"Storage initialized at {self.db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}", exc_info=True)
            raise

    def _create_tables(self):
        """Create the messages table."""
        try:
            query = """
                    CREATE TABLE IF NOT EXISTS messages
                    (
                        data_id TEXT PRIMARY KEY,
                        chat TEXT,
                        community TEXT,
                        jid TEXT,
                        message TEXT,
                        sender TEXT,
                        systime TEXT,
                        direction TEXT,
                        type TEXT
                    );
                    """
            self.conn.execute(query)
            self.conn.commit()
        except Exception as e:
            logger.error(f"Failed to create tables: {e}", exc_info=True)

    def _start_writer(self):
        """Start the background writer task"""
        try:
            self._writer_task = asyncio.create_task(self._writer_loop())
        except RuntimeError:
            # No event loop running yet - will be started later
            logger.warning("No event loop available yet - writer will start on first enqueue")

    async def _writer_loop(self):
        """Single writer coroutine - processes queue continuously"""
        while True:
            try:
                messages = await self.queue.get()
                try:
                    await self._insert_batch_internal(messages)
                except Exception as e:
                    logger.error(f"[DB Writer] {e}", exc_info=True)
                finally:
                    self.queue.task_done()
            except asyncio.CancelledError:
                logger.info("Writer loop cancelled")
                break
            except Exception as e:
                logger.error(f"[Writer Loop] Unexpected error: {e}", exc_info=True)

    async def enqueue_insert(self, messages: List[Message]) -> None:
        """
        Public async API: enqueue messages for DB write (non-blocking)
        This is the new recommended way to insert messages.
        """
        if messages:
            # Ensure writer is running
            if self._writer_task is None or self._writer_task.done():
                self._writer_task = asyncio.create_task(self._writer_loop())
            await self.queue.put(messages)

    async def _Insert_Batch(self, MessageList: List["Message"]) -> None:
        """
        Legacy method - kept for backward compatibility.
        Directly inserts without queueing (blocking).
        Consider using enqueue_insert() for better performance.
        """
        await self._insert_batch_internal(MessageList)

    async def _insert_batch_internal(self, MessageList: List["Message"]) -> None:
        """
        Internal batch insert logic used by both queue and direct methods.
        """
        try:
            """
            "Every Message entering Storage has a non-null data_id."
            """
            prepared_rows = await asyncio.gather(
                *(msg.GetTraceObj() for msg in MessageList)
            )

            Insert_Query = """
                           INSERT OR IGNORE INTO messages 
                (data_id, chat, community, jid, message, sender, systime, direction, type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                           """

            self.conn.executemany(Insert_Query, prepared_rows)
            self.conn.commit()

            # Failure update - mark messages that failed to insert
            ids = [m.data_id for m in MessageList]
            if not ids:
                return

            placeholders = ",".join("?" * len(ids))
            query = f"SELECT data_id FROM messages WHERE data_id IN ({placeholders})"

            rows = self.conn.execute(query, ids).fetchall()
            inserted_ids = {r[0] for r in rows}

            for msg in MessageList:
                if msg.data_id not in inserted_ids:
                    msg.Failed = True
        except Exception as e:
            logger.error(f"SQL Batch Insertion Error:\n {e}", exc_info=True)

    def message_exists(self, data_id: str) -> bool:
        """Check if a message with the given data_id exists."""
        try:
            cursor = self.conn.execute("SELECT 1 FROM messages WHERE data_id = ?", (data_id,))
            return cursor.fetchone() is not None
        except Exception as e:
            logger.error(f"Failed to check message existence {data_id}: {e}", exc_info=True)
            return False

    def get_all_messages(self) -> List[Dict]:
        """Retrieve all messages as a list of dictionaries."""
        try:
            self.conn.row_factory = sqlite3.Row
            cursor = self.conn.execute("SELECT * FROM messages")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Failed to get messages: {e}", exc_info=True)
            return []

    async def close(self):
        """
        Close the database connection gracefully.
        Waits for queue to finish processing before closing.
        """
        # Cancel writer task
        if self._writer_task and not self._writer_task.done():
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
        
        # Wait for remaining queue items
        if not self.queue.empty():
            logger.info("Waiting for queue to finish...")
            await self.queue.join()
        
        # Close connection
        if self.conn:
            self.conn.close()
            logger.info("Storage connection closed.")
