# PF2e Puzzle Server

Real-time puzzle server for Pathfinder 2e with separate player and dungeon master authentication.

## Setup

Install dependencies:

```bash
pip install -r requirements.txt
```

## Running the Server

Start the server with:

```bash
uvicorn server:app --reload
```

By default, the DM password is `dm123`. You can change it by setting the `DM_PASSWORD` environment variable:

```bash
DM_PASSWORD=your_secret_password uvicorn server:app --reload
```

## Authentication

The application now has separate login flows:

- **Player Login**: Requires only a name (no password)
- **Dungeon Master Login**: Requires a password for access

Visit `http://localhost:8000/login.html` to log in. After logging in, you'll be redirected to the puzzle interface.

## Usage

- Navigate to `http://localhost:8000/login.html` to access the login page
- Choose between Player or Dungeon Master login
- After successful login, you'll be redirected to the puzzle interface
- Multiple users can connect to the same room by using the `?room=` URL parameter
- Click the Logout button in the top-right to log out

## Session Management

Sessions are stored in-memory and last for 24 hours. Logging out will clear the session immediately.
