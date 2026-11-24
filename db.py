"""SQLite persistence for LittleBites."""

import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path

BASE = Path(__file__).parent


@contextmanager
def connection(path):
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 10000")
    con.execute("PRAGMA journal_mode = WAL")
    try:
        yield con
    finally:
        con.close()


def _seed_id(recipe):
    if recipe.get("seed_id"):
        return str(recipe["seed_id"])
    value = unicodedata.normalize("NFKD", recipe["n"]).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def normalize_text(value):
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    return " ".join(re.findall(r"[\w]+", value, flags=re.UNICODE))


def normalize_ingredients(ingredients):
    return tuple(sorted(normalize_text(item) for item in ingredients))


def init(path):
    with connection(path) as con, con:
        con.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            local_import_done INTEGER NOT NULL DEFAULT 0,
            must_change_password INTEGER NOT NULL DEFAULT 0)""")
        user_columns = {row["name"] for row in con.execute("PRAGMA table_info(users)")}
        if "local_import_done" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN local_import_done INTEGER NOT NULL DEFAULT 0")
        if "must_change_password" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
        con.execute("""CREATE TABLE IF NOT EXISTS child_profiles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            age_group INTEGER NOT NULL CHECK(age_group IN (0,1,4,9,13)),
            diet TEXT NOT NULL DEFAULT '',
            allergens TEXT NOT NULL DEFAULT '[]',
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("""CREATE TABLE IF NOT EXISTS recipes(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, from_age INTEGER NOT NULL,
            time TEXT DEFAULT '', ingredients TEXT NOT NULL, steps TEXT NOT NULL, note TEXT DEFAULT '',
            community INTEGER DEFAULT 0, created TEXT DEFAULT CURRENT_TIMESTAMP, seed_id TEXT,
            author_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            status TEXT NOT NULL DEFAULT 'approved', meal TEXT NOT NULL DEFAULT 'dinner')""")
        columns = {row["name"] for row in con.execute("PRAGMA table_info(recipes)")}
        if "seed_id" not in columns:
            con.execute("ALTER TABLE recipes ADD COLUMN seed_id TEXT")
        if "author_user_id" not in columns:
            con.execute("ALTER TABLE recipes ADD COLUMN author_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
        if "status" not in columns:
            con.execute("ALTER TABLE recipes ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'")
        if "meal" not in columns:
            con.execute("ALTER TABLE recipes ADD COLUMN meal TEXT NOT NULL DEFAULT 'dinner'")
        if "meals" not in columns:
            con.execute("ALTER TABLE recipes ADD COLUMN meals TEXT NOT NULL DEFAULT '[\"dinner\"]'")
            for row in con.execute("SELECT id,meal FROM recipes").fetchall():
                old_meal = row["meal"] if row["meal"] in {"breakfast", "lunch", "dinner", "snack"} else "dinner"
                con.execute("UPDATE recipes SET meals=? WHERE id=?", (json.dumps([old_meal]), row["id"]))
        if "photo" not in columns:
            con.execute("ALTER TABLE recipes ADD COLUMN photo TEXT NOT NULL DEFAULT ''")
        con.execute("""CREATE TABLE IF NOT EXISTS recipe_reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            reason TEXT NOT NULL DEFAULT '', created TEXT DEFAULT CURRENT_TIMESTAMP,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE)""")
        reports_columns = {row["name"] for row in con.execute("PRAGMA table_info(recipe_reports)")}
        if "user_id" not in reports_columns:
            con.execute("ALTER TABLE recipe_reports ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS recipes_seed_id_unique ON recipes(seed_id) WHERE seed_id IS NOT NULL")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS recipe_reports_user_unique ON recipe_reports(recipe_id,user_id) WHERE user_id IS NOT NULL")
        con.execute("""CREATE TABLE IF NOT EXISTS recipe_comments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            parent_id INTEGER REFERENCES recipe_comments(id) ON DELETE CASCADE,
            body TEXT NOT NULL,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            edited_at TEXT,
            hidden INTEGER NOT NULL DEFAULT 0)""")
        comment_columns = {row["name"] for row in con.execute("PRAGMA table_info(recipe_comments)")}
        if "parent_id" not in comment_columns:
            con.execute("ALTER TABLE recipe_comments ADD COLUMN parent_id INTEGER REFERENCES recipe_comments(id) ON DELETE CASCADE")
        if "edited_at" not in comment_columns:
            con.execute("ALTER TABLE recipe_comments ADD COLUMN edited_at TEXT")
        if "hidden" not in comment_columns:
            con.execute("ALTER TABLE recipe_comments ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        con.execute("""CREATE TABLE IF NOT EXISTS comment_reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id INTEGER NOT NULL REFERENCES recipe_comments(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reason TEXT NOT NULL DEFAULT '', created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(comment_id,user_id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS rate_limits(
            bucket TEXT NOT NULL,
            identity TEXT NOT NULL,
            ts REAL NOT NULL)""")
        con.execute("CREATE INDEX IF NOT EXISTS rate_limits_lookup ON rate_limits(bucket,identity,ts)")
        con.execute("""CREATE TABLE IF NOT EXISTS user_favorites(
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id,recipe_id))""")
        con.execute("""CREATE TABLE IF NOT EXISTS user_made(
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(user_id,recipe_id))""")

        seeds = json.loads((BASE / "data" / "seed_recipes.json").read_text(encoding="utf-8"))
        for recipe in seeds:
            seed_id = _seed_id(recipe)
            meals = recipe.get("meals") or [recipe.get("meal", "dinner")]
            meals = list(dict.fromkeys(meals))
            encoded_meals = json.dumps(meals)
            photo = recipe.get("photo", "")
            exists = con.execute("SELECT 1 FROM recipes WHERE seed_id = ?", (seed_id,)).fetchone()
            if exists:
                con.execute("UPDATE recipes SET meals=?,meal=?,photo=? WHERE seed_id=?", (encoded_meals, meals[0], photo, seed_id))
                continue
            old_row = con.execute(
                "SELECT id FROM recipes WHERE name = ? AND community = 0 AND seed_id IS NULL LIMIT 1",
                (recipe["n"],),
            ).fetchone()
            if old_row:
                con.execute("UPDATE recipes SET seed_id=?,meals=?,meal=?,photo=? WHERE id=?", (seed_id, encoded_meals, meals[0], photo, old_row["id"]))
                continue
            con.execute(
                """INSERT INTO recipes(name,from_age,time,ingredients,steps,note,community,seed_id,status,meal,meals,photo)
                   VALUES(?,?,?,?,?,?,0,?,'approved',?,?,?)""",
                (recipe["n"], recipe["from"], recipe.get("time", ""), json.dumps(recipe["ing"]),
                 json.dumps(recipe["steps"]), recipe.get("note", ""), seed_id, meals[0], encoded_meals, photo),
            )


def _recipe(row, user_id=None):
    meals = json.loads(row["meals"]) if "meals" in row.keys() else [row["meal"] if "meal" in row.keys() else "dinner"]
    if not isinstance(meals, list) or not meals:
        meals = [row["meal"] if "meal" in row.keys() else "dinner"]
    return {"id": row["id"], "n": row["name"], "from": row["from_age"], "time": row["time"],
            "ing": json.loads(row["ingredients"]), "steps": json.loads(row["steps"]),
            "note": row["note"], "community": bool(row["community"]), "status": row["status"],
            "meal": meals[0], "meals": meals, "photo": row["photo"] if "photo" in row.keys() else "",
            "can_edit": bool(row["community"] and user_id and row["author_user_id"] == user_id)}


def all_recipes(path, user_id=None, is_admin=False):
    with connection(path) as con:
        if is_admin:
            rows = con.execute("SELECT * FROM recipes ORDER BY community, id").fetchall()
        else:
            rows = con.execute("""SELECT * FROM recipes WHERE community=0 OR status='approved'
                OR (status IN ('pending','hidden') AND author_user_id=?) ORDER BY community,id""", (user_id,)).fetchall()
        return [_recipe(row, user_id) for row in rows]


def paginated_recipes(path, user_id=None, is_admin=False, *, meal=None, age=None, query="", sort="newest", page=1, per_page=24):
    clauses, params = [], []
    if not is_admin:
        clauses.append("(community=0 OR status='approved' OR (status IN ('pending','hidden') AND author_user_id=?))")
        params.append(user_id)
    if meal:
        clauses.append("meals LIKE ?")
        params.append(f'%"{meal}"%')
    if age is not None:
        clauses.append("from_age<=?")
        params.append(age)
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        clauses.append("(name LIKE ? ESCAPE '\\' COLLATE NOCASE OR ingredients LIKE ? ESCAPE '\\' COLLATE NOCASE OR steps LIKE ? ESCAPE '\\' COLLATE NOCASE)")
        params.extend((pattern, pattern, pattern))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connection(path) as con:
        rows = con.execute(f"SELECT * FROM recipes{where}", params).fetchall()
    recipes = [_recipe(row, user_id) for row in rows]
    if sort == "age":
        recipes.sort(key=lambda recipe: (recipe["from"], -recipe["id"]))
    elif sort == "quickest":
        def minutes(recipe):
            match = re.search(r"\d+", recipe["time"] or "")
            return int(match.group()) if match else 10**9
        recipes.sort(key=lambda recipe: (minutes(recipe), -recipe["id"]))
    else:
        recipes.sort(key=lambda recipe: recipe["id"], reverse=True)
    total = len(recipes)
    pages = max(1, (total + per_page - 1) // per_page)
    start = (page - 1) * per_page
    return {"items": recipes[start:start + per_page], "total": total, "page": page, "pages": pages}


def recent_recipes(path, limit=6, user_id=None, is_admin=False):
    bounded_limit = max(1, min(int(limit), 30))
    with connection(path) as con:
        if is_admin:
            rows = con.execute("SELECT * FROM recipes ORDER BY created DESC,id DESC LIMIT ?", (bounded_limit,)).fetchall()
        else:
            rows = con.execute("""SELECT * FROM recipes WHERE community=0 OR status='approved'
                OR (status IN ('pending','hidden') AND author_user_id=?) ORDER BY created DESC,id DESC LIMIT ?""",
                (user_id, bounded_limit)).fetchall()
        return [_recipe(row, user_id) for row in rows]


def has_duplicate_community(path, name, ingredients, exclude_id=None):
    wanted_name, wanted_ingredients = normalize_text(name), normalize_ingredients(ingredients)
    with connection(path) as con:
        rows = con.execute("SELECT id,name,ingredients FROM recipes WHERE community=1 AND (? IS NULL OR id<>?)", (exclude_id, exclude_id)).fetchall()
        return any(normalize_text(row["name"]) == wanted_name and
                   normalize_ingredients(json.loads(row["ingredients"])) == wanted_ingredients for row in rows)


def add_recipe(path, name, from_age, ingredients, steps, note, recipe_time="", author_user_id=None, meals=None):
    meals = list(dict.fromkeys(meals or ["dinner"]))
    with connection(path) as con, con:
        cursor = con.execute("""INSERT INTO recipes(name,from_age,time,ingredients,steps,note,community,
            author_user_id,status,meal,meals) VALUES(?,?,?,?,?,?,1,?,'pending',?,?)""",
            (name, from_age, recipe_time, json.dumps(ingredients), json.dumps(steps), note, author_user_id, meals[0], json.dumps(meals)))
        return cursor.lastrowid


def edit_community_recipe(path, recipe_id, user_id, name, from_age, ingredients, steps, note, recipe_time, meals):
    meals = list(dict.fromkeys(meals or ["dinner"]))
    with connection(path) as con, con:
        cursor = con.execute("""UPDATE recipes SET name=?,from_age=?,ingredients=?,steps=?,note=?,time=?,meal=?,meals=?,status='pending'
            WHERE id=? AND community=1 AND author_user_id=?""",
            (name, from_age, json.dumps(ingredients), json.dumps(steps), note, recipe_time, meals[0], json.dumps(meals), recipe_id, user_id))
        return cursor.rowcount == 1


def user_owns_community_recipe(path, recipe_id, user_id):
    with connection(path) as con:
        row = con.execute("SELECT 1 FROM recipes WHERE id=? AND community=1 AND author_user_id=?", (recipe_id, user_id)).fetchone()
        return row is not None


def admin_update_recipe_classification(path, recipe_id, from_age, meals):
    meals = list(dict.fromkeys(meals or ["dinner"]))
    with connection(path) as con, con:
        cursor = con.execute("UPDATE recipes SET from_age=?,meal=?,meals=? WHERE id=? AND community=1", (from_age, meals[0], json.dumps(meals), recipe_id))
        return cursor.rowcount == 1


def recipe_is_community(path, recipe_id):
    with connection(path) as con:
        row = con.execute("SELECT community FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        return bool(row and row["community"])


def report_recipe(path, recipe_id, reason="", user_id=None):
    with connection(path) as con, con:
        recipe = con.execute("SELECT community,status FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        if not recipe or not recipe["community"] or recipe["status"] == "rejected":
            return False
        try:
            con.execute("INSERT INTO recipe_reports(recipe_id,reason,user_id) VALUES(?,?,?)", (recipe_id, reason, user_id))
        except sqlite3.IntegrityError:
            return False
        report_count = con.execute("SELECT COUNT(DISTINCT user_id) FROM recipe_reports WHERE recipe_id=?", (recipe_id,)).fetchone()[0]
        if report_count >= 3:
            con.execute("UPDATE recipes SET status='hidden' WHERE id=? AND status='approved'", (recipe_id,))
        return True


def reported_recipes(path):
    with connection(path) as con:
        rows = con.execute("""SELECT r.*,COUNT(DISTINCT rr.user_id) report_count
            FROM recipes r JOIN recipe_reports rr ON rr.recipe_id=r.id
            WHERE r.community=1 GROUP BY r.id ORDER BY report_count DESC,r.id""").fetchall()
        result = []
        for row in rows:
            item = _recipe(row)
            item["report_count"] = row["report_count"]
            item["reports"] = [dict(report) for report in con.execute(
                "SELECT reason,created FROM recipe_reports WHERE recipe_id=? ORDER BY created", (row["id"],)).fetchall()]
            result.append(item)
        return result


def pending_recipes(path):
    with connection(path) as con:
        return [_recipe(row) for row in con.execute("SELECT * FROM recipes WHERE community=1 AND status IN ('pending','hidden') ORDER BY created").fetchall()]


def community_recipes(path):
    with connection(path) as con:
        rows = con.execute("SELECT * FROM recipes WHERE community=1 ORDER BY created DESC,id DESC").fetchall()
        return [_recipe(row) for row in rows]


def moderate_recipe(path, recipe_id, status):
    if status not in {"approved", "rejected"}:
        return False
    with connection(path) as con, con:
        cursor = con.execute("UPDATE recipes SET status=? WHERE id=? AND community=1", (status, recipe_id))
        return cursor.rowcount == 1


def delete_community_recipe(path, recipe_id, user_id=None, admin=False):
    with connection(path) as con, con:
        if admin:
            cursor = con.execute("DELETE FROM recipes WHERE id=? AND community=1", (recipe_id,))
        else:
            cursor = con.execute("DELETE FROM recipes WHERE id=? AND community=1 AND author_user_id=?", (recipe_id, user_id))
        return cursor.rowcount == 1


def create_user(path, username, password_hash, first_account_only=False):
    try:
        with connection(path) as con:
            con.execute("BEGIN IMMEDIATE")
            if first_account_only and con.execute("SELECT COUNT(*) FROM users").fetchone()[0] != 0:
                con.rollback()
                return False
            cursor = con.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", (username, password_hash))
            con.commit()
            return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None


def user_by_username(path, username):
    with connection(path) as con:
        row = con.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        return dict(row) if row else None


def user_by_id(path, user_id):
    with connection(path) as con:
        row = con.execute("SELECT id,username,created,local_import_done,must_change_password FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def all_users(path):
    with connection(path) as con:
        rows = con.execute("SELECT id,username,created FROM users ORDER BY username COLLATE NOCASE").fetchall()
        return [dict(row) for row in rows]


def update_password(path, user_id, password_hash, must_change=False):
    with connection(path) as con, con:
        cursor = con.execute("UPDATE users SET password_hash=?,must_change_password=? WHERE id=?",
                             (password_hash, int(must_change), user_id))
        return cursor.rowcount == 1


def delete_user(path, user_id):
    """Delete a user's community posts before cascading their account data."""
    with connection(path) as con, con:
        con.execute("DELETE FROM recipes WHERE community=1 AND author_user_id=?", (user_id,))
        cursor = con.execute("DELETE FROM users WHERE id=?", (user_id,))
        return cursor.rowcount == 1


def user_count(path):
    with connection(path) as con:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def set_local_import_done(path, user_id):
    with connection(path) as con, con:
        con.execute("UPDATE users SET local_import_done=1 WHERE id=?", (user_id,))


def profile_list(path, user_id):
    with connection(path) as con:
        rows = con.execute("SELECT * FROM child_profiles WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
        return [{"id": r["id"], "name": r["name"], "age": r["age_group"], "diet": r["diet"],
                 "allergens": json.loads(r["allergens"])} for r in rows]


def save_profile(path, user_id, profile_id, name, age, diet, allergens):
    encoded = json.dumps(allergens)
    with connection(path) as con, con:
        if profile_id:
            cursor = con.execute("""UPDATE child_profiles SET name=?,age_group=?,diet=?,allergens=?
                WHERE id=? AND user_id=?""", (name, age, diet, encoded, profile_id, user_id))
            return profile_id if cursor.rowcount else None
        cursor = con.execute("INSERT INTO child_profiles(user_id,name,age_group,diet,allergens) VALUES(?,?,?,?,?)",
                             (user_id, name, age, diet, encoded))
        return cursor.lastrowid


def delete_profile(path, user_id, profile_id):
    with connection(path) as con, con:
        cursor = con.execute("DELETE FROM child_profiles WHERE id=? AND user_id=?", (profile_id, user_id))
        return cursor.rowcount == 1


def _user_recipe_items(path, table, user_id):
    with connection(path) as con:
        return [row[0] for row in con.execute(f"SELECT recipe_id FROM {table} WHERE user_id=? ORDER BY recipe_id", (user_id,)).fetchall()]


def favorites(path, user_id):
    return _user_recipe_items(path, "user_favorites", user_id)


def made_recipes(path, user_id):
    return _user_recipe_items(path, "user_made", user_id)


def set_user_recipe(path, table, user_id, recipe_id, active):
    if table not in {"user_favorites", "user_made"}:
        raise ValueError("Invalid user recipe table")
    with connection(path) as con, con:
        recipe = con.execute("SELECT id FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        if not recipe:
            return False
        if active:
            con.execute(f"INSERT OR IGNORE INTO {table}(user_id,recipe_id) VALUES(?,?)", (user_id, recipe_id))
        else:
            con.execute(f"DELETE FROM {table} WHERE user_id=? AND recipe_id=?", (user_id, recipe_id))
        return True


def import_user_recipe_items(path, user_id, favorites_ids, made_ids):
    with connection(path) as con, con:
        for table, recipe_ids in (("user_favorites", favorites_ids), ("user_made", made_ids)):
            for recipe_id in recipe_ids:
                con.execute(f"INSERT OR IGNORE INTO {table}(user_id,recipe_id) SELECT ?,id FROM recipes WHERE id=?",
                            (user_id, recipe_id))


def recipe_comment_access(path, recipe_id, user_id, is_admin=False):
    with connection(path) as con:
        recipe = con.execute("SELECT community,status,author_user_id FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        if not recipe:
            return False
        if not recipe["community"] or recipe["status"] == "approved":
            return True
        return bool(is_admin or (user_id and recipe["author_user_id"] == user_id))


def recipe_comments(path, recipe_id, user_id, include_hidden=False):
    hidden_filter = "" if include_hidden else "AND c.hidden=0"
    with connection(path) as con:
        rows = con.execute(f"""SELECT c.*,u.username,
              EXISTS(SELECT 1 FROM comment_reports cr WHERE cr.comment_id=c.id AND cr.user_id=?) AS has_reported
            FROM recipe_comments c JOIN users u ON u.id=c.user_id
            WHERE c.recipe_id=? {hidden_filter} ORDER BY c.created,c.id""", (user_id, recipe_id)).fetchall()
        return [{"id": row["id"], "recipe_id": row["recipe_id"], "parent_id": row["parent_id"],
                 "body": row["body"], "username": row["username"], "created": row["created"],
                 "edited_at": row["edited_at"], "hidden": bool(row["hidden"]),
                 "has_reported": bool(row["has_reported"]), "can_edit": row["user_id"] == user_id}
                for row in rows]


def add_comment(path, recipe_id, user_id, body, parent_id=None):
    with connection(path) as con, con:
        recipe = con.execute("SELECT id FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        if not recipe:
            return None
        if parent_id is not None:
            parent = con.execute("SELECT recipe_id,parent_id FROM recipe_comments WHERE id=?", (parent_id,)).fetchone()
            if not parent or parent["recipe_id"] != recipe_id or parent["parent_id"] is not None:
                return None
        cursor = con.execute("INSERT INTO recipe_comments(recipe_id,user_id,parent_id,body) VALUES(?,?,?,?)",
                             (recipe_id, user_id, parent_id, body))
        return cursor.lastrowid


def edit_comment(path, comment_id, user_id, body):
    with connection(path) as con, con:
        cursor = con.execute("UPDATE recipe_comments SET body=?,edited_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
                             (body, comment_id, user_id))
        return cursor.rowcount == 1


def comment_context(path, comment_id):
    with connection(path) as con:
        row = con.execute("SELECT recipe_id,user_id FROM recipe_comments WHERE id=?", (comment_id,)).fetchone()
        return dict(row) if row else None


def report_comment(path, comment_id, user_id, reason=""):
    with connection(path) as con, con:
        target = con.execute("SELECT user_id FROM recipe_comments WHERE id=?", (comment_id,)).fetchone()
        if not target or target["user_id"] == user_id:
            return False
        try:
            con.execute("INSERT INTO comment_reports(comment_id,user_id,reason) VALUES(?,?,?)", (comment_id, user_id, reason))
        except sqlite3.IntegrityError:
            return False
        count = con.execute("SELECT COUNT(*) FROM comment_reports WHERE comment_id=?", (comment_id,)).fetchone()[0]
        if count >= 3:
            con.execute("UPDATE recipe_comments SET hidden=1 WHERE id=?", (comment_id,))
        return True


def admin_comments(path):
    with connection(path) as con:
        rows = con.execute("""SELECT c.*,u.username,r.name AS recipe_name,
              (SELECT COUNT(DISTINCT cr.user_id) FROM comment_reports cr WHERE cr.comment_id=c.id) AS report_count
            FROM recipe_comments c JOIN users u ON u.id=c.user_id JOIN recipes r ON r.id=c.recipe_id
            WHERE c.hidden=1 OR EXISTS(SELECT 1 FROM comment_reports cr WHERE cr.comment_id=c.id)
               OR c.id IN (SELECT id FROM recipe_comments ORDER BY created DESC,id DESC LIMIT 50)
            ORDER BY c.created DESC,c.id DESC""").fetchall()
        return [{"id": row["id"], "recipe_id": row["recipe_id"], "recipe_name": row["recipe_name"],
                 "parent_id": row["parent_id"], "body": row["body"], "username": row["username"],
                 "created": row["created"], "edited_at": row["edited_at"], "hidden": bool(row["hidden"]),
                 "report_count": row["report_count"]} for row in rows]


def moderate_comment(path, comment_id, action):
    with connection(path) as con, con:
        if action == "delete":
            cursor = con.execute("DELETE FROM recipe_comments WHERE id=?", (comment_id,))
            return cursor.rowcount == 1
        if action == "restore":
            cursor = con.execute("UPDATE recipe_comments SET hidden=0 WHERE id=?", (comment_id,))
            con.execute("DELETE FROM comment_reports WHERE comment_id=?", (comment_id,))
            return cursor.rowcount == 1
        return False


def health(path):
    with connection(path) as con:
        return con.execute("SELECT 1").fetchone()[0] == 1




def check_rate_limit(path, bucket, identity, max_requests, window_seconds, now=None):
    """Atomically consume a persistent request slot, returning retry seconds if full."""
    import math
    import time

    now = time.time() if now is None else float(now)
    cutoff = now - int(window_seconds)
    with connection(path) as con:
        con.execute("BEGIN IMMEDIATE")
        # Remove old rows gradually so the rate-limit store stays small.
        con.execute("DELETE FROM rate_limits WHERE ts < ?", (now - max(86_400, int(window_seconds) * 2),))
        count, oldest = con.execute(
            "SELECT COUNT(*),MIN(ts) FROM rate_limits WHERE bucket=? AND identity=? AND ts>?",
            (bucket, str(identity), cutoff),
        ).fetchone()
        if count >= max_requests:
            retry_after = max(1, math.ceil(int(window_seconds) - (now - oldest)))
            con.commit()
            return retry_after
        con.execute("INSERT INTO rate_limits(bucket,identity,ts) VALUES(?,?,?)", (bucket, str(identity), now))
        con.commit()
        return 0
