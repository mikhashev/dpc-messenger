# D-PC Federation Hub

> **Component:** Server-side Backend
> **License:** AGPL v3
> **Status:** MVP Feature-Complete

This directory contains the source code for the D-PC Federation Hub. The Hub is a central server application that provides essential services for the D-PC network, acting as a "phone book" and "matchmaker" for peers.

## Core Responsibilities

The Hub is intentionally designed to be "dumb" and minimalistic to respect user privacy. It **never** stores or processes users' private conversations or full personal contexts. Its sole responsibilities are:

1.  **User Authentication:** Securely authenticating users via OAuth 2.0 with providers like Google and GitHub.
2.  **Public Profile Hosting:** Storing and serving the non-sensitive, public-facing "expertise profiles" that users choose to share.
3.  **Peer Discovery:** Providing an API to search for other users based on their public expertise.
4.  **P2P Signaling:** Relaying messages between two clients to help them establish a direct, end-to-end encrypted P2P connection (NAT traversal).

## Technology Stack

-   **Language:** Python 3.12+
-   **Framework:** FastAPI
-   **Database:** PostgreSQL (with `asyncpg` driver)
-   **ORM:** SQLAlchemy (asynchronous)
-   **Migrations:** Alembic
-   **Authentication:** OAuth 2.0, JWT
-   **Dependencies:** Managed by Poetry

## API Specification

The full API contract is defined in the root of the repository: [`/specs/hub_api_v1.md`](../specs/hub_api_v1.md).

## Local Development Setup

### Prerequisites

1.  **Poetry:** For managing Python dependencies.
2.  **Docker:** For running the PostgreSQL database.
3.  **Google OAuth Credentials:** You must have a `Client ID` and `Client Secret` from the Google Cloud Console.

### Step-by-Step Instructions

1.  **Navigate to this Directory:**
    All commands should be run from the `dpc-hub/` directory.

2.  **Install Dependencies:**
    ```bash
    poetry install
    ```

3.  **Set Up the Database:**
    a. Start the PostgreSQL database using Docker. This command will create a persistent volume to store your data.
    ```bash
    docker run -d --name dpc-hub-db -e POSTGRES_USER=user -e POSTGRES_PASSWORD=password -e POSTGRES_DB=dpc_hub -p 5432:5432 -v dpc_postgres_data:/var/lib/postgresql/data postgres:16
    ```
    b. To stop the database: `docker stop dpc-hub-db`
    c. To start it again later: `docker start dpc-hub-db`

4.  **Configure Environment Variables:**
    a. Create a `.env` file in this directory (`dpc-hub/.env`).
    b. Add the following content, replacing the placeholders with your actual credentials. **This file is git-ignored and should never be committed.**
    ```.env
    # PostgreSQL Connection URL
    DATABASE_URL="postgresql+asyncpg://user:password@localhost:5432/dpc_hub"

    # Secret key for signing JWTs. Generate a random string, e.g., with `openssl rand -hex 32`
    SECRET_KEY="your_super_secret_random_string_here"

    # Google OAuth Credentials
    GOOGLE_CLIENT_ID="your_google_client_id.apps.googleusercontent.com"
    GOOGLE_CLIENT_SECRET="your_google_client_secret"
    ```

5.  **Run Database Migrations:**
    This command will create the necessary tables (`users`, `public_profiles`) in your database.
    ```bash
    poetry run alembic upgrade head
    ```

6.  **Run the Server:**
    ```bash
    poetry run uvicorn dpc_hub.main:app --reload
    ```
    The server will be running at `http://127.0.0.1:8000`.

### Testing the API

1.  **API Docs:** Open `http://127.0.0.1:8000/docs` in your browser to access the interactive Swagger UI.
2.  **Authentication:**
    a. Navigate to `http://127.0.0.1:8000/login/google`.
    b. Complete the Google login flow.
    c. You will be redirected to a page showing your JWT `access_token`. Copy this token.
3.  **Authorization:**
    a. In the API docs, click the "Authorize" button.
    b. In the dialog, type `Bearer ` (with a space) and paste your copied token.
    c. Click "Authorize".
4.  **Test Endpoints:** You can now use the "Try it out" feature for the protected endpoints like `/users/me/` and `/profile`.

## Project Structure

-   `dpc_hub/`: The main Python package for the Hub.
    -   `main.py`: The FastAPI application entry point and API routes.
    -   `database.py`: SQLAlchemy engine and session configuration.
    -   `models.py`: SQLAlchemy data models (tables).
    -   `schemas.py`: Pydantic models for API data validation.
    -   `crud.py`: Functions for database Create, Read, Update, Delete operations.
    -   `auth.py`: JWT and OAuth 2.0 logic.
    -   `settings.py`: Application configuration management.
    -   `alembic/`: Database migration scripts.
-   `pyproject.toml`: Project dependencies and metadata.
-   `.env`: Local environment variables (not committed).