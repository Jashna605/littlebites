# LittleBites

LittleBites is a family meal-idea app built with Flask, SQLite, HTML, CSS, and vanilla JavaScript. It includes age, ingredient, pantry, and diet-clue filters; favorites; step-by-step cooking mode and timers; reversible made marks and kitchen badges; child profiles; moderated community recipes with comments; account recovery; and an Ollama chat helper. Recipes can appear under more than one meal, including snacks. Starter recipes use 19 self-hosted, AI-generated photorealistic food photos, with recipe-specific images for dishes such as mango coconut rice pudding, zucchini corn fritters, polenta squares, and cucumber hummus boats. Photos are examples of a serving; the actual result may look different.

After updating the project files, restart Flask so the seed photo assignments are refreshed in SQLite. If a browser still shows an older image, reload the page once while online so the updated service worker can replace its previous image cache.

Ingredient labels are cautious text clues, never allergy or diet guarantees. The app uses wording such as “no listed dairy found” instead of claiming that a recipe is dairy-free. Always check labels and cross-contact information, adapt texture and serving size, and ask a pediatrician about infant feeding. LittleBites is for inspiration, not medical advice.

## Local setup

1. Install Python 3.10 or newer. Install Ollama if you want to use the chat helper, then start it and download a model such as llama3.2.
2. Install requirements:

       python -m pip install -r requirements.txt

3. Copy .env.example to .env. Generate a private signing key:

       python -c "import secrets; print(secrets.token_urlsafe(48))"

   Replace the example SECRET_KEY value with the generated value. LittleBites refuses to start with an absent, short, or example key.
4. Choose an ADMIN_USERNAME in .env. This must match the parent username used for the first administrator account. The first account can be created even when ALLOW_SIGNUP=false.
5. For local HTTP development, run with Flask debug enabled so the session cookie can work without HTTPS:

       FLASK_DEBUG=1 python app.py

   On PowerShell:

       $env:FLASK_DEBUG = "1"
       py app.py

6. Open http://127.0.0.1:5000, choose Sign in → Create an account, and register with the ADMIN_USERNAME you configured. Set ALLOW_SIGNUP=false and restart the app after the first account is created if you want to close registration.

The app creates or migrates the SQLite database at DB_PATH on startup. Keep .env private. For production, use HTTPS and a production server; the session cookie is Secure whenever Flask debug mode is off.

## Deploy online with HTTPS (Render)

The included `render.yaml` is configured for a Docker web service with HTTPS, `/health` checks, and a persistent disk mounted at `/data` for SQLite. The app runs as one service instance because the database is SQLite. Render's persistent disks require a paid web-service plan.

1. Put the project in a GitHub repository. Keep `.env`, database files, and passwords out of the repository; `.gitignore` excludes them.
2. In Render, create a Blueprint from that repository and use the `render.yaml` file at the project root.
3. When asked for `ADMIN_USERNAME`, enter the username you want to use for the first account. Render generates `SECRET_KEY` for the service.
4. Wait for the service to deploy, then open the HTTPS `onrender.com` address shown in its dashboard.
5. Create the first account with the exact `ADMIN_USERNAME` you configured. Afterward, set `ALLOW_SIGNUP=false` in the service environment settings and redeploy.

The initial hosted configuration sets `MEAL_BUDDY_DEMO=true`, so chat can respond without a separately hosted Ollama server. To use a real Ollama model, host Ollama privately and set `OLLAMA_BASE_URL` to an address reachable from the app; do not expose Ollama directly to the public internet. The health endpoint is `/health`. Render terminates HTTPS and forwards traffic to the app; `TRUST_PROXY=true` is enabled for that trusted proxy.

Once the HTTPS site is live, its public address can be put into a QR code so people can open it on their own devices.

## Accounts and child profiles

Guests can browse recipes, filter, and use the cooking view. Signing in is required to share or report community recipes and to sync favorites, made-recipes badges, and profiles. Passwords are stored as Werkzeug hashes. Sessions use signed cookies, and forms/API writes require a session CSRF token.

Favorites and “I made this!” marks created while browsing as a guest remain in that browser. On the first sign-in in that browser, LittleBites offers to import those saved IDs once. Signed-in favorites and badges are stored per account in SQLite and the app stops reading/writing those local-storage keys.

Profiles store a parent-entered name or nickname, age group, diet clue, and selected allergens in the local SQLite database. The profile name is not sent to Ollama. The chat receives the selected age group, diet clue, allergens, pantry items, and recent conversation only. Chat history is kept in page memory and is not saved by LittleBites.

## Community recipes and admin review

Community recipes are limited to ages 1+ and begin as pending. Only the author and admin can see a pending recipe. The admin is the signed-in user whose username matches ADMIN_USERNAME; open Admin in the header to approve, reject, or delete submissions and review reports. Authors may delete their own submissions.

The report route permits one report per signed-in user for each recipe and is rate-limited. Three reports from distinct accounts automatically hide an approved recipe until the admin reviews it. Recipes for young children are rejected when text includes listed choking-risk preparations; recipes for age 1 are also rejected when they include honey.

## Recipe tags and filters

Tags look for known ingredient words and hidden sources such as ranch seasoning, tamari, and Worcestershire sauce. Absence tags say “no listed … found”; they do not establish that a recipe meets a diet or is safe for an allergy. Ingredient cards and details remind parents to check labels and cross-contact. “Free-from” phrases such as “peanut-free” are not tagged as containing the negated allergen.

The starter catalog contains 68 recipes for under 12 months, age 1+, age 4+, age 9+, and age 13+. Baby recipes avoid honey and include a pediatrician reminder and texture/choking notes. Recipe seed IDs keep startup additions stable and prevent duplicates. Starter recipes may be tagged for multiple meals.

## Accounts and community changes

From **Your account**, a signed-in parent can change a password after entering the current password, or delete their account after password re-entry and typing `DELETE`. The configured administrator account cannot be deleted from the account page. Admins can issue a one-time temporary password from Admin; the recipient must change it at next sign-in. This app has no email-based password reset.

Authors can edit their own community recipes. Every edit returns an approved recipe to pending review. Parents can edit their own comments, reply one level deep, and report comments; three distinct reports hide a comment for admin review. Admins can restore or delete comments. Pending and hidden recipe comments remain private to the author and admin.

The recipe list supports server-side `page`, `per_page` (maximum 60), `meal`, `age`, `q`, and `sort` filters. A request without query parameters keeps the original full-list API response. In the app, pages are loaded as needed while allergen and diet clues are evaluated across all server-matching recipes before the visible “Load more” slice, so those client-side filters are not restricted to the current page.

### API additions and behavior

| Route | Purpose |
| --- | --- |
| `GET /api/recipes?page=1&per_page=24` | Paginated recipe listing; optional meal, age, search, and sort parameters. |
| `PUT /api/recipes/<id>` | Edit the signed-in author's community recipe; edits become pending. |
| `GET/POST /api/recipes/<id>/comments` | Read visible comments or add a plain-text comment/reply. |
| `PUT /api/comments/<id>` | Edit the signed-in author's comment. |
| `POST /api/comments/<id>/report` | Report a comment once per signed-in user. |
| `POST /account/password` | Change password with current-password verification and session rotation. |
| `POST /account/delete` | Delete the signed-in account and its associated content after confirmation. |
| `POST /admin/users/<id>/reset-password` | Admin-assisted one-time password reset. |
| `POST /admin/comments/<id>/<restore\|delete>` | Restore or remove a comment. |

All state-changing routes require the app's CSRF token. Rate limits are stored in SQLite and shared between app instances using the same database.

## Ollama settings

The meal buddy uses Ollama locally by default. Set MEAL_BUDDY_DEMO=true for a built-in sample reply without starting a model. The server limits chat context to the latest 10 messages, validates text length, and checks replies against profile and typed allergens. That warning is a reminder only and cannot prove a meal is safe.

## Install LittleBites on a phone

The PWA service worker requires HTTPS on a hosted site (or localhost for local development). Put the app behind HTTPS, open the app URL in the phone browser, then use Add to Home Screen on iPhone/Safari or Install app from the Chrome/Edge browser menu on Android.

The service worker caches the guest app shell, local food photos, and recipe GET responses. Recipe browsing and the cooking view can work offline after a visit. Login, profile, favorites-sync, community, and other state-changing requests are never cached. A banner appears when the browser is offline.

## Backup and restore

Create a consistent SQLite backup with the database backup API:

       python scripts/backup.py

Backups go to backups/ with a timestamp; the script keeps the 10 newest copies. Set DB_PATH to point to a non-default database. To restore, stop LittleBites, copy the chosen backup over the configured database path, and restart.

## Configuration

| Variable | Purpose |
| --- | --- |
| SECRET_KEY | Required private Flask session signing key. |
| ADMIN_USERNAME | Username that receives admin moderation access. |
| ALLOW_SIGNUP | Defaults to true; set false to close sign-ups after creating the first account. |
| TRUST_PROXY | Defaults to false; enables one trusted proxy hop via ProxyFix when true. |
| DB_PATH | SQLite file path. |
| OLLAMA_BASE_URL | Local Ollama URL, default http://localhost:11434. |
| OLLAMA_MODEL | Ollama model name, default llama3.2. |
| OLLAMA_TIMEOUT | Ollama request timeout in seconds. |
| MEAL_BUDDY_DEMO | Use the local sample chat reply without Ollama when true. |

## Tests

Install test dependencies and run the suite:

       python -m pip install -r requirements-dev.txt
       python -m pytest

Coverage includes account hashing and limits, CSRF, profiles and chat context, recipe ownership/moderation, comments, allergy wording, pagination, additive migration from an older single-meal database, PWA assets, the 68-recipe catalog, database WAL mode, and backup retention.

Optional browser flow tests cover guest browsing, meal tabs, allergen filtering, recipe details, cooking mode, signup, recipe sharing, comments, and layout overflow at 375, 768, and 1280 pixels. Install Playwright and its Chromium browser, then run `python -m pytest -m e2e`; these tests are skipped when Playwright or the browser is unavailable:

       python -m pip install -r requirements-e2e.txt
       python -m playwright install chromium
       python -m pytest -m e2e

## Docker

Copy `.env.example` to `.env`, set a private `SECRET_KEY` (at least 32 characters) and `ADMIN_USERNAME`, then build and start the Waitress container:

       docker compose up --build

Open http://127.0.0.1:8000. Compose mounts a named volume at `/data` for the SQLite database. Keep `.env` private and back up the database volume regularly. To stop the service, run `docker compose down`; the named data volume is retained.

## Contributing

Keep the Flask, SQLite, and vanilla JavaScript stack. Add additive database migrations, preserve the strict CSP and CSRF checks, build user-facing DOM with `textContent` and the existing `node()` helper, and keep allergy/diet wording cautious. Add tests for each behavior, run `python -m pytest`, and bump the service-worker cache version whenever a cached static asset changes.

## Main files

- app.py — Flask routes, sessions, CSRF, account/profile endpoints, moderation, security headers, Ollama chat.
- db.py — SQLite schema/migrations, user data, recipes, profiles, reports, and seed loading.
- safety.py — ingredient clues with hidden-source and negated phrase handling.
- data/seed_recipes.json — 68 stable-ID starter recipes.
- templates/index.html, templates/login.html, templates/signup.html, templates/admin.html — app, account, and moderation pages.
- static/app.js, static/styles.css, static/pwa.js, static/sw.js — responsive UI behavior, clay theme, and offline support.
- static/fonts/, static/icons/, static/manifest.webmanifest — self-hosted fonts and install assets.
- scripts/backup.py — timestamped SQLite backup and rotation.
- tests/ — automated test suite.
