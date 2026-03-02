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
        CREATE TABLE IF NOT EXISTS token_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain TEXT NOT NULL,
            pair_address TEXT NOT NULL,
            token_address TEXT NOT NULL,
            token_symbol TEXT NOT NULL,
            token_name TEXT NOT NULL,
            price_usd REAL,
            market_cap REAL,
            liquidity_usd REAL,
            volume_24h REAL,
            volume_6h REAL,
            volume_1h REAL,
            price_change_24h REAL,
            price_change_6h REAL,
            price_change_1h REAL,
            buys_24h INTEGER,
            sells_24h INTEGER,
            buys_6h INTEGER,
            sells_6h INTEGER,
            buys_1h INTEGER,
            sells_1h INTEGER,
            timestamp REAL NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alerts_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_address TEXT NOT NULL,
            chain TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            message TEXT,
            timestamp REAL NOT NULL
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_snapshots_token
        ON token_snapshots(token_address, chain, timestamp)
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_alerts_token
        ON alerts_sent(token_address, chain, timestamp)
    """)

    conn.commit()
    conn.close()


def save_snapshot(data: dict) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO token_snapshots (
            chain, pair_address, token_address, token_symbol, token_name,
            price_usd, market_cap, liquidity_usd,
            volume_24h, volume_6h, volume_1h,
            price_change_24h, price_change_6h, price_change_1h,
            buys_24h, sells_24h, buys_6h, sells_6h, buys_1h, sells_1h,
            timestamp
        ) VALUES (
            :chain, :pair_address, :token_address, :token_symbol, :token_name,
            :price_usd, :market_cap, :liquidity_usd,
            :volume_24h, :volume_6h, :volume_1h,
            :price_change_24h, :price_change_6h, :price_change_1h,
            :buys_24h, :sells_24h, :buys_6h, :sells_6h, :buys_1h, :sells_1h,
            :timestamp
        )
    """, data)
    conn.commit()
    conn.close()


def get_historical_avg_volume(token_address: str, chain: str,
                              hours: int = 24) -> Optional[float]:
    """Get average 1h volume over the past N hours from stored snapshots."""
    conn = get_connection()
    cursor = conn.cursor()
    cutoff = time.time() - (hours * 3600)
    cursor.execute("""
        SELECT AVG(volume_1h) as avg_vol
        FROM token_snapshots
        WHERE token_address = ? AND chain = ? AND timestamp > ?
    """, (token_address, chain, cutoff))
    row = cursor.fetchone()
    conn.close()
    if row and row["avg_vol"] is not None:
        return row["avg_vol"]
    return None


def get_historical_avg_buys(token_address: str, chain: str,
                            hours: int = 24) -> Optional[float]:
    """Get average 1h buy count over the past N hours."""
    conn = get_connection()
    cursor = conn.cursor()
    cutoff = time.time() - (hours * 3600)
    cursor.execute("""
        SELECT AVG(buys_1h) as avg_buys
        FROM token_snapshots
        WHERE token_address = ? AND chain = ? AND timestamp > ?
    """, (token_address, chain, cutoff))
    row = cursor.fetchone()
    conn.close()
    if row and row["avg_buys"] is not None:
        return row["avg_buys"]
    return None


def was_alert_sent_recently(token_address: str, chain: str,
                            alert_type: str, cooldown_hours: int = 4) -> bool:
    """Check if we already sent an alert for this token recently."""
    conn = get_connection()
    cursor = conn.cursor()
    cutoff = time.time() - (cooldown_hours * 3600)
    cursor.execute("""
        SELECT COUNT(*) as cnt
        FROM alerts_sent
        WHERE token_address = ? AND chain = ? AND alert_type = ?
              AND timestamp > ?
    """, (token_address, chain, alert_type, cutoff))
    row = cursor.fetchone()
    conn.close()
    return row["cnt"] > 0


def record_alert(token_address: str, chain: str,
                 alert_type: str, message: str) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO alerts_sent (token_address, chain, alert_type, message, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (token_address, chain, alert_type, message, time.time()))
    conn.commit()
    conn.close()


def cleanup_old_data(days: int = 7) -> None:
    """Remove snapshots older than N days to keep DB size in check."""
    conn = get_connection()
    cursor = conn.cursor()
    cutoff = time.time() - (days * 86400)
    cursor.execute("DELETE FROM token_snapshots WHERE timestamp < ?", (cutoff,))
    cursor.execute("DELETE FROM alerts_sent WHERE timestamp < ?", (cutoff,))
    conn.commit()
    conn.close()
