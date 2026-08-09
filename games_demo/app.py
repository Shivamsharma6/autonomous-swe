# games_demo/app.py
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "games.db")
db_file_path = DEFAULT_DB_PATH

app = FastAPI(title="Video Game Database CRUD API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    conn = sqlite3.connect(db_file_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(custom_path: Optional[str] = None):
    global db_file_path
    if custom_path:
        db_file_path = custom_path
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        genre TEXT NOT NULL,
        platform TEXT NOT NULL,
        rating REAL DEFAULT 0.0,
        release_year INTEGER DEFAULT 2024,
        status TEXT DEFAULT 'Wishlist',
        cover_url TEXT DEFAULT '',
        created_at REAL
    );
    """)
    
    # Check if empty, seed default data
    cursor.execute("SELECT COUNT(*) as count FROM games")
    if cursor.fetchone()["count"] == 0:
        seed_games = [
            ("Elden Ring", "Action RPG", "PC / PS5 / Xbox", 9.6, 2022, "Completed", "https://images.unsplash.com/photo-1542751371-adc38448a05e?w=500"),
            ("Cyberpunk 2077: Phantom Liberty", "Sci-Fi RPG", "PC / PS5", 9.2, 2023, "Playing", "https://images.unsplash.com/photo-1511512578047-dfb367046420?w=500"),
            ("The Legend of Zelda: Tears of the Kingdom", "Action Adventure", "Nintendo Switch", 9.8, 2023, "Completed", "https://images.unsplash.com/photo-1550745165-9bc0b252726f?w=500"),
            ("Baldur's Gate 3", "Turn-Based RPG", "PC / PS5", 9.9, 2023, "Completed", "https://images.unsplash.com/photo-1538481199705-c710c4e965fc?w=500"),
            ("Hades II", "Rogue-like Action", "PC", 9.5, 2024, "Playing", "https://images.unsplash.com/photo-1518709268805-4e9042af9f23?w=500")
        ]
        for g in seed_games:
            cursor.execute("""
            INSERT INTO games (title, genre, platform, rating, release_year, status, cover_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (g[0], g[1], g[2], g[3], g[4], g[5], g[6], time.time()))
    conn.commit()
    conn.close()


init_db()


class GameCreateReq(BaseModel):
    title: str
    genre: str
    platform: str = "PC"
    rating: float = Field(default=8.0, ge=0.0, le=10.0)
    release_year: int = 2024
    status: str = "Wishlist"
    cover_url: str = ""


class GameUpdateReq(BaseModel):
    title: Optional[str] = None
    genre: Optional[str] = None
    platform: Optional[str] = None
    rating: Optional[float] = None
    release_year: Optional[int] = None
    status: Optional[str] = None
    cover_url: Optional[str] = None


@app.get("/api/games")
def list_games(search: Optional[str] = None, genre: Optional[str] = None, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_db()
    cursor = conn.cursor()
    query = "SELECT * FROM games WHERE 1=1"
    params = []
    
    if search:
        query += " AND (title LIKE ? OR genre LIKE ? OR platform LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term, term])
    if genre and genre != "ALL":
        query += " AND genre = ?"
        params.append(genre)
    if status_filter and status_filter != "ALL":
        query += " AND status = ?"
        params.append(status_filter)
        
    query += " ORDER BY id DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


@app.get("/api/games/{game_id}")
def get_game(game_id: int) -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM games WHERE id = ?", (game_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Game not found")
    return dict(row)


@app.post("/api/games", status_code=status.HTTP_201_CREATED)
def create_game(game: GameCreateReq) -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.cursor()
    created_at = time.time()
    cover = game.cover_url or "https://images.unsplash.com/photo-1550745165-9bc0b252726f?w=500"
    cursor.execute("""
    INSERT INTO games (title, genre, platform, rating, release_year, status, cover_url, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (game.title, game.genre, game.platform, game.rating, game.release_year, game.status, cover, created_at))
    conn.commit()
    game_id = cursor.lastrowid
    cursor.execute("SELECT * FROM games WHERE id = ?", (game_id,))
    new_row = cursor.fetchone()
    conn.close()
    return dict(new_row)


@app.put("/api/games/{game_id}")
def update_game(game_id: int, game: GameUpdateReq) -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM games WHERE id = ?", (game_id,))
    existing = cursor.fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail="Game not found")
    
    current = dict(existing)
    title = game.title if game.title is not None else current["title"]
    genre = game.genre if game.genre is not None else current["genre"]
    platform = game.platform if game.platform is not None else current["platform"]
    rating = game.rating if game.rating is not None else current["rating"]
    release_year = game.release_year if game.release_year is not None else current["release_year"]
    status_val = game.status if game.status is not None else current["status"]
    cover_url = game.cover_url if game.cover_url is not None else current["cover_url"]

    cursor.execute("""
    UPDATE games SET title=?, genre=?, platform=?, rating=?, release_year=?, status=?, cover_url=?
    WHERE id=?
    """, (title, genre, platform, rating, release_year, status_val, cover_url, game_id))
    conn.commit()
    cursor.execute("SELECT * FROM games WHERE id = ?", (game_id,))
    updated_row = cursor.fetchone()
    conn.close()
    return dict(updated_row)


@app.delete("/api/games/{game_id}")
def delete_game(game_id: int) -> Dict[str, Any]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM games WHERE id = ?", (game_id,))
    existing = cursor.fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail="Game not found")
    
    cursor.execute("DELETE FROM games WHERE id = ?", (game_id,))
    conn.commit()
    conn.close()
    return {"id": game_id, "status": "deleted"}


# Static files mount
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    def serve_frontend():
        return FileResponse(os.path.join(static_dir, "index.html"))
