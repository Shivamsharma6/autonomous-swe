document.addEventListener('DOMContentLoaded', () => {
    let games = [];

    // DOM Elements
    const gamesGrid = document.getElementById('gamesGrid');
    const searchInput = document.getElementById('searchInput');
    const genreFilter = document.getElementById('genreFilter');
    const statusFilter = document.getElementById('statusFilter');
    const addGameBtn = document.getElementById('addGameBtn');
    const gameModal = document.getElementById('gameModal');
    const closeModalBtn = document.getElementById('closeModalBtn');
    const cancelModalBtn = document.getElementById('cancelModalBtn');
    const gameForm = document.getElementById('gameForm');

    // Stats
    const statTotal = document.getElementById('statTotal');
    const statCompleted = document.getElementById('statCompleted');
    const statPlaying = document.getElementById('statPlaying');
    const statAvgRating = document.getElementById('statAvgRating');

    // Init
    fetchGames();
    bindEvents();

    function bindEvents() {
        searchInput.addEventListener('input', fetchGames);
        genreFilter.addEventListener('change', fetchGames);
        statusFilter.addEventListener('change', fetchGames);

        addGameBtn.addEventListener('click', () => {
            document.getElementById('modalTitle').textContent = 'Add Game to Vault';
            gameForm.reset();
            document.getElementById('gameIdInput').value = '';
            gameModal.classList.remove('hidden');
        });

        closeModalBtn.addEventListener('click', () => gameModal.classList.add('hidden'));
        cancelModalBtn.addEventListener('click', () => gameModal.classList.add('hidden'));

        gameForm.addEventListener('submit', (e) => {
            e.preventDefault();
            saveGame();
        });
    }

    function fetchGames() {
        const query = searchInput.value.trim();
        const genre = genreFilter.value;
        const status = statusFilter.value;

        let url = '/api/games?';
        if (query) url += `search=${encodeURIComponent(query)}&`;
        if (genre) url += `genre=${encodeURIComponent(genre)}&`;
        if (status) url += `status_filter=${encodeURIComponent(status)}&`;

        fetch(url)
            .then(res => res.json())
            .then(data => {
                games = data;
                renderGames(games);
                updateStats(games);
            })
            .catch(err => console.error('Error fetching games:', err));
    }

    function renderGames(items) {
        gamesGrid.innerHTML = '';
        if (items.length === 0) {
            gamesGrid.innerHTML = `
                <div style="grid-column: 1/-1; text-align: center; padding: 4rem; color: #94a3b8;">
                    <h3>No video games found</h3>
                    <p>Click "+ Add New Game" above to insert your first entry!</p>
                </div>
            `;
            return;
        }

        items.forEach(game => {
            const card = document.createElement('div');
            card.className = 'game-card glass-card';
            
            const statusClass = `status-${game.status.toLowerCase()}`;
            const cover = game.cover_url || 'https://images.unsplash.com/photo-1550745165-9bc0b252726f?w=500';

            card.innerHTML = `
                <img src="${cover}" alt="${game.title}" class="game-cover">
                <div class="game-card-body">
                    <div class="game-header">
                        <h3 class="game-title">${game.title}</h3>
                        <span class="rating-badge font-mono">★ ${game.rating.toFixed(1)}</span>
                    </div>

                    <div class="game-tags">
                        <span class="tag">${game.genre}</span>
                        <span class="tag">${game.platform}</span>
                        <span class="tag font-mono">${game.release_year}</span>
                    </div>

                    <div>
                        <span class="status-chip ${statusClass}">${game.status}</span>
                    </div>

                    <div class="game-actions">
                        <button class="btn btn-secondary btn-sm edit-btn" data-id="${game.id}">Edit</button>
                        <button class="btn btn-danger btn-sm delete-btn" data-id="${game.id}">Delete</button>
                    </div>
                </div>
            `;

            // Attach event listeners
            card.querySelector('.edit-btn').addEventListener('click', () => editGame(game));
            card.querySelector('.delete-btn').addEventListener('click', () => deleteGame(game.id));

            gamesGrid.appendChild(card);
        });
    }

    function updateStats(items) {
        statTotal.textContent = items.length;
        const completed = items.filter(g => g.status === 'Completed').length;
        const playing = items.filter(g => g.status === 'Playing').length;
        statCompleted.textContent = completed;
        statPlaying.textContent = playing;

        if (items.length > 0) {
            const avg = items.reduce((acc, g) => acc + g.rating, 0) / items.length;
            statAvgRating.textContent = `${avg.toFixed(1)} / 10`;
        } else {
            statAvgRating.textContent = '0.0 / 10';
        }
    }

    function saveGame() {
        const id = document.getElementById('gameIdInput').value;
        const payload = {
            title: document.getElementById('titleInput').value.trim(),
            genre: document.getElementById('genreInput').value.trim(),
            platform: document.getElementById('platformInput').value.trim(),
            rating: parseFloat(document.getElementById('ratingInput').value),
            release_year: parseInt(document.getElementById('yearInput').value),
            status: document.getElementById('statusInput').value,
            cover_url: document.getElementById('coverInput').value.trim()
        };

        const method = id ? 'PUT' : 'POST';
        const url = id ? `/api/games/${id}` : '/api/games';

        fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(res => res.json())
        .then(() => {
            gameModal.classList.add('hidden');
            fetchGames();
        });
    }

    function editGame(game) {
        document.getElementById('modalTitle').textContent = 'Edit Game Entry';
        document.getElementById('gameIdInput').value = game.id;
        document.getElementById('titleInput').value = game.title;
        document.getElementById('genreInput').value = game.genre;
        document.getElementById('platformInput').value = game.platform;
        document.getElementById('ratingInput').value = game.rating;
        document.getElementById('yearInput').value = game.release_year;
        document.getElementById('statusInput').value = game.status;
        document.getElementById('coverInput').value = game.cover_url || '';
        gameModal.classList.remove('hidden');
    }

    function deleteGame(id) {
        if (confirm('Are you sure you want to delete this game entry?')) {
            fetch(`/api/games/${id}`, { method: 'DELETE' })
                .then(res => res.json())
                .then(() => fetchGames());
        }
    }
});
