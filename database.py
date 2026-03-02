import sqlite3
import time
from typing import Optional

import config


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dca_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_signature TEXT UNIQUE NOT NULL,
            dca_account TEXT NOT NULL,
            user_wallet TEXT NOT NULL,
            input_mint TEXT NOT NULL,
            output_mint TEXT NOT NULL,
            in_amount_raw INTEGER NOT NULL,
            in_amount_per_cycle_raw INTEGER NOT NULL,
            cycle_frequency_seconds INTEGER NOT NULL,
            in_amount_usd REAL,
            total_cycles INTEGER,
            token_symbol TEXT,
            token_name TEXT,
            token_mcap REAL,
            token_price REAL,
            timestamp REAL NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS token_volume (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_mint TEXT NOT NULL,
            chain TEXT NOT NULL DEFAULT 'solana',
            volume_24h REAL,
            price_usd REAL,
            market_cap REAL,
            liquidity_usd REAL,
            timestamp REAL NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alerts_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_mint TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            message TEXT,
            timestamp REAL NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scanner_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_dca_output_mint
        ON dca_orders(output_mint, timestamp)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_dca_timestamp
        ON dca_orders(timestamp)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_volume_token
        ON token_volume(token_mint, chain, timestamp)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_alerts_token
        ON alerts_sent(token_mint, alert_type, timestamp)
    """)

    conn.commit()
    conn.close()


def save_dca_order(order: dict) -> bool:
    """Save a DCA order. Returns True if new (not duplicate)."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT OR IGNORE INTO dca_orders (
                tx_signature, dca_account, user_wallet,
                input_mint, output_mint,
                in_amount_raw, in_amount_per_cycle_raw, cycle_frequency_seconds,
                in_amount_usd, total_cycles,
                token_symbol, token_name, token_mcap, token_price,
                timestamp
            ) VALUES (
                :tx_signature, :dca_account, :user_wallet,
                :input_mint, :output_mint,
                :in_amount_raw, :in_amount_per_cycle_raw, :cycle_frequency_seconds,
                :in_amount_usd, :total_cycles,
                :token_symbol, :token_name, :token_mcap, :token_price,
                :timestamp
            )
        """, order)
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_recent_dca_orders(output_mint: str, hours: int = 24) -> list[dict]:
    """Get recent DCA orders for a specific output token."""
    conn = get_connection()
    cursor = conn.cursor()
    cutoff = time.time() - (hours * 3600)
    cursor.execute("""
        SELECT * FROM dca_orders
        WHERE output_mint = ? AND timestamp > ?
        ORDER BY timestamp DESC
    """, (output_mint, cutoff))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def get_dca_order_count(output_mint: str, hours: int = 24) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cutoff = time.time() - (hours * 3600)
    cursor.execute("""
        SELECT COUNT(*) as cnt FROM dca_orders
        WHERE output_mint = ? AND timestamp > ?
    """, (output_mint, cutoff))
    row = cursor.fetchone()
    conn.close()
    return row["cnt"]


def get_total_dca_value(output_mint: str, hours: int = 24) -> float:
    conn = get_connection()
    cursor = conn.cursor()
    cutoff = time.time() - (hours * 3600)
    cursor.execute("""
        SELECT COALESCE(SUM(in_amount_usd), 0) as total_usd FROM dca_orders
        WHERE output_mint = ? AND timestamp > ? AND in_amount_usd IS NOT NULL
    """, (output_mint, cutoff))
    row = cursor.fetchone()
    conn.close()
    return row["total_usd"]


def get_unique_dca_wallets(output_mint: str, hours: int = 24) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cutoff = time.time() - (hours * 3600)
    cursor.execute("""
        SELECT COUNT(DISTINCT user_wallet) as cnt FROM dca_orders
        WHERE output_mint = ? AND timestamp > ?
    """, (output_mint, cutoff))
    row = cursor.fetchone()
    conn.close()
    return row["cnt"]


def save_volume_snapshot(data: dict) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO token_volume (
            token_mint, chain, volume_24h,
            price_usd, market_cap, liquidity_usd, timestamp
        ) VALUES (
            :token_mint, :chain, :volume_24h,
            :price_usd, :market_cap, :liquidity_usd, :timestamp
        )
    """, data)
    conn.commit()
    conn.close()


def get_avg_volume(token_mint: str, hours: int = 72) -> Optional[float]:
    """Get average 24h volume over the past N hours of snapshots."""
    conn = get_connection()
    cursor = conn.cursor()
    cutoff = time.time() - (hours * 3600)
    cursor.execute("""
        SELECT AVG(volume_24h) as avg_vol FROM token_volume
        WHERE token_mint = ? AND timestamp > ?
    """, (token_mint, cutoff))
    row = cursor.fetchone()
    conn.close()
    if row and row["avg_vol"] is not None:
        return row["avg_vol"]
    return None


def get_state(key: str) -> Optional[str]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM scanner_state WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row["value"] if row else None


def set_state(key: str, value: str) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO scanner_state (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()
    conn.close()


def was_alert_sent_recently(token_mint: str, alert_type: str,
                            cooldown_hours: int = 4) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cutoff = time.time() - (cooldown_hours * 3600)
    cursor.execute("""
        SELECT COUNT(*) as cnt FROM alerts_sent
        WHERE token_mint = ? AND alert_type = ? AND timestamp > ?
    """, (token_mint, alert_type, cutoff))
    row = cursor.fetchone()
    conn.close()
    return row["cnt"] > 0


def record_alert(token_mint: str, alert_type: str, message: str) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO alerts_sent (token_mint, alert_type, message, timestamp)
        VALUES (?, ?, ?, ?)
    """, (token_mint, alert_type, message, time.time()))
    conn.commit()
    conn.close()


def cleanup_old_data(days: int = 14) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cutoff = time.time() - (days * 86400)
    cursor.execute("DELETE FROM dca_orders WHERE timestamp < ?", (cutoff,))
    cursor.execute("DELETE FROM token_volume WHERE timestamp < ?", (cutoff,))
    cursor.execute("DELETE FROM alerts_sent WHERE timestamp < ?", (cutoff,))
    conn.commit()
    conn.close()
