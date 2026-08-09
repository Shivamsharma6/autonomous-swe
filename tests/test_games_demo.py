# tests/test_games_demo.py
import pytest
from fastapi.testclient import TestClient
from games_demo.app import app, init_db

@pytest.fixture
def client(tmp_path):
    db_file = str(tmp_path / "test_games.db")
    init_db(db_file)
    with TestClient(app) as test_client:
        yield test_client

def test_get_games_initial(client):
    res = client.get("/api/games")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) >= 3
    titles = [g["title"] for g in data]
    assert "Elden Ring" in titles
    assert "Cyberpunk 2077: Phantom Liberty" in titles


def test_create_game(client):
    new_game = {
        "title": "Hollow Knight: Silksong",
        "genre": "Metroidvania",
        "platform": "PC / Switch / PS5",
        "rating": 9.8,
        "release_year": 2024,
        "status": "Wishlist",
        "cover_url": "https://images.unsplash.com/photo-1550745165-9bc0b252726f?w=400"
    }
    res = client.post("/api/games", json=new_game)
    assert res.status_code == 201
    created = res.json()
    assert created["title"] == "Hollow Knight: Silksong"
    assert "id" in created

def test_update_game(client):
    # Fetch first game
    games = client.get("/api/games").json()
    game_id = games[0]["id"]

    update_payload = {"status": "Completed", "rating": 10.0}
    res = client.put(f"/api/games/{game_id}", json=update_payload)
    assert res.status_code == 200
    updated = res.json()
    assert updated["status"] == "Completed"
    assert updated["rating"] == 10.0

def test_delete_game(client):
    games = client.get("/api/games").json()
    game_id = games[0]["id"]

    res_del = client.delete(f"/api/games/{game_id}")
    assert res_del.status_code == 200
    assert res_del.json()["status"] == "deleted"

    res_get = client.get(f"/api/games/{game_id}")
    assert res_get.status_code == 404
