import hmac
import json
import logging
import os
import re
import secrets
import urllib.error
import urllib.request

from dotenv import load_dotenv
from flask import Flask, abort, flash, g, jsonify, make_response, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.exceptions import HTTPException

import db
import safety

load_dotenv()
AGES = {1, 4, 9, 13}
MEALS = {"breakfast", "lunch", "dinner", "snack"}
PROFILE_AGES = {0, 1, 4, 9, 13}
ALLERGENS = {"milk", "egg", "wheat", "peanut", "tree nuts", "soy", "fish", "shellfish", "sesame", "coconut"}
DIETS = {"", "no listed meat or fish found", "no listed animal ingredients found", "no listed dairy found", "no listed gluten found"}
AGE_LABELS = {0: "Under 12 months", 1: "1 year", 4: "4 years", 9: "9 years", 13: "13 years"}
COMMUNITY_NOTE = "Community recipe, pending admin review. Check every ingredient, label, and texture for your child."
TIME_PATTERN = re.compile(r"^\d{1,3}\s?(?:min|mins|minutes|hr|hrs|hours)$", re.IGNORECASE)
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,32}$")
CHOKING_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"\bwhole\s+grapes?\b", r"\bwhole\s+(?:tree\s+)?nuts?\b", r"\bwhole\s+peanuts?\b",
    r"\bwhole\s+(?:almonds?|cashews?|walnuts?|pecans?|pistachios?)\b", r"\bpopcorn\b",
    r"\bhard\s+raw\s+carrots?\b", r"\blarge\s+(?:chunks?|pieces?)\s+(?:of\s+)?hot[\s-]?dogs?\b",
    r"\bwhole\s+cherry\s+tomatoes?\b",
))

ALLERGY_GROUPS = {
    "dairy": ("dairy", "milk", "buttermilk", "cheese", "butter", "cream", "yogurt", "yoghurt", "whey", "casein"),
    "milk": ("dairy", "milk", "buttermilk", "cheese", "butter", "cream", "yogurt", "yoghurt", "whey", "casein"),
    "peanut": ("peanut", "groundnut"), "peanuts": ("peanut", "groundnut"),
    "groundnut": ("peanut", "groundnut"), "groundnuts": ("peanut", "groundnut"),
    "nut": ("nut", "peanut", "groundnut", "almond", "cashew", "walnut", "pecan", "pistachio", "hazelnut", "macadamia", "pine nut"),
    "nuts": ("nut", "peanut", "groundnut", "almond", "cashew", "walnut", "pecan", "pistachio", "hazelnut", "macadamia", "pine nut"),
    "tree nut": ("tree nut", "almond", "cashew", "walnut", "pecan", "pistachio", "hazelnut", "macadamia", "pine nut"),
    "tree nuts": ("tree nut", "almond", "cashew", "walnut", "pecan", "pistachio", "hazelnut", "macadamia", "pine nut"),
}


def _payload():
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else None


def _clean_list(value, limit, max_length):
    if not isinstance(value, list) or len(value) > limit:
        return None
    cleaned = []
    for item in value:
        if not isinstance(item, str) or len(item.strip()) > max_length:
            return None
        if item.strip():
            cleaned.append(item.strip())
    return cleaned


def _validate_community_recipe(data):
    """Shared create/edit validation; keep text, age, timing and safety rules aligned."""
    name = data.get("name")
    ingredients = _clean_list(data.get("ingredients"), 30, 120)
    steps = _clean_list(data.get("steps"), 20, 300)
    age, recipe_time = data.get("from"), data.get("time", "")
    raw_meals = data.get("meals", data.get("meal", "dinner"))
    meals = [raw_meals] if isinstance(raw_meals, str) else raw_meals
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        return None, "Please give your recipe a name (up to 80 characters).", 400
    if ingredients is None or steps is None or not ingredients or not steps:
        return None, "Add ingredients and steps within the size limits.", 400
    if type(age) is not int or age not in AGES:
        return None, "Community recipes are for ages 1 and up. Choose a supported age group.", 400
    if not isinstance(meals, list) or not 1 <= len(meals) <= len(MEALS) or any(not isinstance(meal, str) or meal not in MEALS for meal in meals):
        return None, "Choose one or more supported meal options.", 400
    meals = list(dict.fromkeys(meals))
    if not isinstance(recipe_time, str) or len(recipe_time) > 32 or (recipe_time and not TIME_PATTERN.fullmatch(recipe_time.strip())):
        return None, "Time must look like ‘20 min’ or ‘1 hour’.", 400
    whole_text = " ".join(ingredients + steps)
    unsafe = any(pattern.search(whole_text) for pattern in CHOKING_PATTERNS)
    if age < 4 and re.search(r"\bgrapes?\b", whole_text, re.I) and not re.search(r"quarter|lengthwise|finely\s+chop|mash", whole_text, re.I):
        unsafe = True
    if age < 4 and re.search(r"\braw\s+carrots?\b", whole_text, re.I) and not re.search(r"grated|shredded|soft|cooked", whole_text, re.I):
        unsafe = True
    if age < 4 and unsafe:
        return None, "This recipe may include a choking hazard for children under 4. Use a safer preparation and texture before sharing.", 400
    if age <= 1 and re.search(r"(?<![a-z])honey(?![a-z])", whole_text, re.I):
        return None, "Do not include honey in a recipe for babies or very young children.", 400
    return {"name": name.strip(), "from": age, "time": recipe_time.strip(), "ingredients": ingredients,
            "steps": steps, "meal": meals[0], "meals": meals}, None, None


def _phrase_pattern(phrase):
    phrase = re.escape(phrase.strip().casefold()).replace(r"\ ", r"\s+")
    if phrase.endswith("s"):
        plural = ""
    elif phrase.endswith(("ch", "sh", "x", "z")):
        plural = r"(?:s|es)?"
    else:
        plural = "s?"
    return re.compile(rf"(?<![a-z0-9]){phrase}{plural}(?![a-z0-9])", re.IGNORECASE)


def _allergy_mentions(reply, requested):
    hits = []
    for raw in requested.split(","):
        allergen = raw.strip().casefold()
        if not allergen or allergen == "none":
            continue
        terms = ALLERGY_GROUPS.get(allergen, (allergen.rstrip("s"),))
        if any(_phrase_pattern(term).search(reply.casefold()) for term in terms):
            hits.append(raw.strip())
    return hits


def create_app(db_path=None):
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    secret = os.getenv("SECRET_KEY", "").strip()
    secret_lower = secret.casefold()
    if len(secret) < 32 or any(marker in secret_lower for marker in ("change-this", "change-me", "example", "replace-with", "your-secret", "dev-secret")):
        raise RuntimeError("Set SECRET_KEY in .env to a private random value of at least 32 characters before starting LittleBites.")
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret,
        DB_PATH=db_path or os.getenv("DB_PATH", "littlebites.db"),
        MAX_CONTENT_LENGTH=20_000,
        PROPAGATE_EXCEPTIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("FLASK_DEBUG", "").lower() not in {"1", "true", "yes", "on"},
        ASSISTANT_RATE_LIMIT=(6, 60), RECIPE_RATE_LIMIT=(10, 60), REPORT_RATE_LIMIT=(10, 60),
        LOGIN_RATE_LIMIT=(8, 900), SIGNUP_RATE_LIMIT=(8, 900),
        COMMENT_RATE_LIMIT=(10, 60),
    )
    if os.getenv("TRUST_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db.init(app.config["DB_PATH"])

    def error_response(code, title, message):
        wants_html = (
            not request.path.startswith("/api/") and bool(request.headers.get("Accept"))
            and request.accept_mimetypes.best_match(["text/html", "application/json"]) == "text/html"
        )
        if wants_html:
            return make_response(render_template("error.html", code=code, title=title, message=message, back_url=url_for("index")), code)
        return make_response(jsonify(error=message), code)

    def limited(bucket_name, config_name, identity=None):
        max_requests, window_seconds = app.config[config_name]
        bucket_identity = identity if identity is not None else request.remote_addr or "unknown"
        retry_after = db.check_rate_limit(app.config["DB_PATH"], bucket_name, bucket_identity, max_requests, window_seconds)
        if retry_after:
            response = error_response(429, "Let’s take a short break", "Too many requests. Please wait a little and try again.")
            response.headers["Retry-After"] = str(retry_after)
            return response
        return None

    def csrf_token():
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    def is_admin(user=None):
        user = user or getattr(g, "current_user", None)
        expected = os.getenv("ADMIN_USERNAME", "").strip()
        return bool(user and expected and user["username"].casefold() == expected.casefold())

    def login_required_json():
        if not getattr(g, "current_user", None):
            return jsonify(error="Please log in to use this feature.", login=url_for("login")), 401
        return None

    @app.before_request
    def load_user_and_check_csrf():
        user_id = session.get("user_id")
        g.current_user = db.user_by_id(app.config["DB_PATH"], user_id) if user_id else None
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            expected = session.get("csrf_token", "")
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
            if not expected or not supplied or not hmac.compare_digest(expected, supplied):
                if request.path.startswith("/api/"):
                    return jsonify(error="This request could not be verified. Refresh the page and try again."), 400
                abort(400, description="This form expired. Refresh the page and try again.")

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if getattr(g, "current_user", None):
            response.headers["X-LittleBites-Authenticated"] = "1"
        return response

    @app.context_processor
    def template_globals():
        return {"current_user": getattr(g, "current_user", None), "csrf_token": csrf_token,
                "current_user_is_admin": is_admin()}

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/login")
    def login():
        if g.current_user:
            return redirect(url_for("index"))
        return render_template("login.html", allow_signup=os.getenv("ALLOW_SIGNUP", "true").lower() not in {"0", "false", "no"} or db.user_count(app.config["DB_PATH"]) == 0)

    @app.post("/login")
    def login_submit():
        raw_username = request.form.get("username", "")
        username = raw_username.strip()[:33]
        password = request.form.get("password", "")
        ip_limit = limited("login-ip", "LOGIN_RATE_LIMIT")
        user_limit = limited("login-username", "LOGIN_RATE_LIMIT", username.casefold())
        if ip_limit or user_limit:
            return error_response(429, "Let’s take a short break", "Too many sign-in attempts. Please wait and try again.")
        user = db.user_by_username(app.config["DB_PATH"], username) if len(raw_username.strip()) <= 32 and len(password) <= 256 else None
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Wrong username or password.", "error")
            return redirect(url_for("login")), 401
        session.clear()
        session["user_id"] = user["id"]
        csrf_token()
        if user.get("must_change_password"):
            flash("An administrator reset your password. Choose a new password before continuing.", "error")
            return redirect(url_for("account_page"))
        return redirect(url_for("index"))

    @app.get("/signup")
    def signup():
        can_signup = os.getenv("ALLOW_SIGNUP", "true").strip().lower() not in {"0", "false", "no"} or db.user_count(app.config["DB_PATH"]) == 0
        if not can_signup:
            return error_response(403, "Sign-ups are closed", "You can still browse recipes as a guest. Ask the family administrator if you need an account.")
        return render_template("signup.html", closed=False)

    @app.post("/signup")
    def signup_submit():
        if os.getenv("ALLOW_SIGNUP", "true").strip().lower() in {"0", "false", "no"} and db.user_count(app.config["DB_PATH"]) > 0:
            return error_response(403, "Sign-ups are closed", "You can still browse recipes as a guest. Ask the family administrator if you need an account.")
        raw_username = request.form.get("username", "")
        username = raw_username.strip()[:33]
        ip_limit = limited("signup-ip", "SIGNUP_RATE_LIMIT")
        user_limit = limited("signup-username", "SIGNUP_RATE_LIMIT", username.casefold())
        if ip_limit or user_limit:
            return error_response(429, "Let’s take a short break", "Too many sign-up attempts. Please wait and try again.")
        password = request.form.get("password", "")
        confirmation = request.form.get("confirm_password", "")
        if len(raw_username.strip()) > 32 or not USERNAME_PATTERN.fullmatch(username):
            flash("Use a username with 3–32 letters, numbers, or underscores.", "error")
            return render_template("signup.html", closed=False), 400
        if len(password) < 8 or len(password) > 256:
            flash("Use a password between 8 and 256 characters.", "error")
            return render_template("signup.html", closed=False), 400
        if password != confirmation:
            flash("Those passwords do not match.", "error")
            return render_template("signup.html", closed=False), 400
        first_account = db.user_count(app.config["DB_PATH"]) == 0
        admin_username = os.getenv("ADMIN_USERNAME", "").strip()
        if first_account and admin_username and username.casefold() != admin_username.casefold():
            flash("The first account must use the configured ADMIN_USERNAME.", "error")
            return render_template("signup.html", closed=False), 400
        user_id = db.create_user(app.config["DB_PATH"], username, generate_password_hash(password), first_account_only=first_account)
        if user_id is False:
            flash("The first account was just created. Please sign in.", "error")
            return redirect(url_for("login")), 409
        if user_id is None:
            flash("That username is already in use.", "error")
            return render_template("signup.html", closed=False), 409
        session.clear()
        session["user_id"] = user_id
        csrf_token()
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.get("/account")
    def account_page():
        if not g.current_user:
            return redirect(url_for("login"))
        return render_template("account.html", user=g.current_user, admin=is_admin())

    @app.post("/account/password")
    def change_password():
        if not g.current_user:
            return redirect(url_for("login"))
        username = g.current_user["username"]
        if limited("password-change-ip", "LOGIN_RATE_LIMIT") or limited("password-change-user", "LOGIN_RATE_LIMIT", username.casefold()):
            return error_response(429, "Let’s take a short break", "Too many password attempts. Please wait and try again.")
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirmation = request.form.get("confirm_password", "")
        stored_user = db.user_by_username(app.config["DB_PATH"], username)
        if not stored_user or not check_password_hash(stored_user["password_hash"], current_password):
            flash("The current password is incorrect.", "error")
            return redirect(url_for("account_page")), 400
        if len(new_password) < 8 or len(new_password) > 256:
            flash("Choose a password between 8 and 256 characters.", "error")
            return redirect(url_for("account_page")), 400
        if new_password != confirmation:
            flash("The new passwords do not match.", "error")
            return redirect(url_for("account_page")), 400
        db.update_password(app.config["DB_PATH"], g.current_user["id"], generate_password_hash(new_password))
        session.clear()
        session["user_id"] = g.current_user["id"]
        csrf_token()
        flash("Your password has been changed.", "success")
        return redirect(url_for("account_page"))

    @app.post("/account/delete")
    def delete_account():
        if not g.current_user:
            return redirect(url_for("login"))
        if is_admin():
            flash("The administrator account cannot be deleted here.", "error")
            return redirect(url_for("account_page")), 403
        username = g.current_user["username"]
        if limited("account-delete-ip", "LOGIN_RATE_LIMIT") or limited("account-delete-user", "LOGIN_RATE_LIMIT", username.casefold()):
            return error_response(429, "Let’s take a short break", "Too many account security attempts. Please wait and try again.")
        stored_user = db.user_by_username(app.config["DB_PATH"], username)
        password = request.form.get("password", "")
        confirmation = request.form.get("confirmation", "")
        if not stored_user or not check_password_hash(stored_user["password_hash"], password):
            flash("The password is incorrect. Your account was not deleted.", "error")
            return redirect(url_for("account_page")), 400
        if confirmation != "DELETE":
            flash("Type DELETE exactly to confirm account deletion.", "error")
            return redirect(url_for("account_page")), 400
        user_id = g.current_user["id"]
        db.delete_user(app.config["DB_PATH"], user_id)
        session.clear()
        flash("Your account and its saved LittleBites data have been deleted.", "success")
        return redirect(url_for("index"))

    @app.get("/health")
    def health():
        try:
            if db.health(app.config["DB_PATH"]):
                return jsonify(status="ok")
        except Exception:
            app.logger.exception("Health check database query failed")
        return jsonify(status="error"), 500

    @app.get("/api/csrf")
    def csrf_state():
        response = jsonify(csrf_token=csrf_token())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/recipes")
    def list_recipes():
        user_id = g.current_user["id"] if g.current_user else None
        if not request.args:
            recipes = db.all_recipes(app.config["DB_PATH"], user_id, is_admin())
            for recipe in recipes:
                recipe["tags"] = safety.tag(recipe["ing"])
            return jsonify(recipes)
        allowed = {"page", "per_page", "meal", "age", "q", "sort"}
        if set(request.args) - allowed:
            return jsonify(error="Use only page, per_page, meal, age, q, and sort filters."), 400
        try:
            page = int(request.args.get("page", "1"))
            per_page = int(request.args.get("per_page", "24"))
        except ValueError:
            return jsonify(error="Page and per_page must be whole numbers."), 400
        if page < 1 or page > 100_000:
            return jsonify(error="Page must be between 1 and 100000."), 400
        if per_page < 1 or per_page > 60:
            return jsonify(error="per_page must be between 1 and 60."), 400
        meal = request.args.get("meal", "")
        if meal and meal not in MEALS:
            return jsonify(error="Choose a supported meal."), 400
        age_text = request.args.get("age", "")
        age = None
        if age_text:
            try:
                age = int(age_text)
            except ValueError:
                return jsonify(error="Age must be one of the listed age groups."), 400
            if age not in PROFILE_AGES:
                return jsonify(error="Age must be one of the listed age groups."), 400
        query = request.args.get("q", "").strip()
        if len(query) > 100:
            return jsonify(error="Search text must be 100 characters or fewer."), 400
        sort = request.args.get("sort", "newest")
        if sort not in {"newest", "quickest", "age"}:
            return jsonify(error="Choose newest, quickest, or age sorting."), 400
        result = db.paginated_recipes(app.config["DB_PATH"], user_id, is_admin(), meal=meal or None, age=age,
                                      query=query, sort=sort, page=page, per_page=per_page)
        for recipe in result["items"]:
            recipe["tags"] = safety.tag(recipe["ing"])
        return jsonify(result)

    @app.get("/api/recipes/new")
    def new_recipes():
        recipes = db.recent_recipes(app.config["DB_PATH"], 6, g.current_user["id"] if g.current_user else None, is_admin())
        for recipe in recipes:
            recipe["tags"] = safety.tag(recipe["ing"])
        return jsonify(recipes)

    @app.post("/api/recipes")
    def add_recipe():
        limited_response = limited("recipes", "RECIPE_RATE_LIMIT")
        if limited_response:
            return limited_response
        if denied := login_required_json():
            return denied
        data = _payload()
        if data is None:
            return jsonify(error="Send recipe details as a JSON object."), 400
        recipe, error, status = _validate_community_recipe(data)
        if error:
            return jsonify(error=error), status
        if db.has_duplicate_community(app.config["DB_PATH"], recipe["name"], recipe["ingredients"]):
            return jsonify(error="That recipe has already been shared."), 409
        recipe_id = db.add_recipe(app.config["DB_PATH"], recipe["name"], recipe["from"], recipe["ingredients"], recipe["steps"], COMMUNITY_NOTE, recipe["time"], g.current_user["id"], recipe["meals"])
        app.logger.info("Community recipe submitted: id=%s author=%s", recipe_id, g.current_user["id"])
        return jsonify(ok=True, status="pending", message="Thanks! Your recipe is pending admin review."), 201

    @app.put("/api/recipes/<int:recipe_id>")
    def edit_recipe(recipe_id):
        limited_response = limited("recipes", "RECIPE_RATE_LIMIT")
        if limited_response:
            return limited_response
        if denied := login_required_json():
            return denied
        if not db.user_owns_community_recipe(app.config["DB_PATH"], recipe_id, g.current_user["id"]):
            return jsonify(error="You can edit only your own community recipes."), 404
        data = _payload()
        if data is None:
            return jsonify(error="Send recipe details as a JSON object."), 400
        recipe, error, status = _validate_community_recipe(data)
        if error:
            return jsonify(error=error), status
        if db.has_duplicate_community(app.config["DB_PATH"], recipe["name"], recipe["ingredients"], exclude_id=recipe_id):
            return jsonify(error="That recipe has already been shared."), 409
        updated = db.edit_community_recipe(app.config["DB_PATH"], recipe_id, g.current_user["id"], recipe["name"], recipe["from"], recipe["ingredients"], recipe["steps"], COMMUNITY_NOTE, recipe["time"], recipe["meals"])
        if not updated:
            return jsonify(error="Recipe not found."), 404
        return jsonify(ok=True, status="pending", message="Recipe updated and returned to the admin review queue.")

    @app.post("/api/recipes/<int:recipe_id>/report")
    def report_recipe(recipe_id):
        if denied := login_required_json():
            return denied
        if limited_response := limited("reports", "REPORT_RATE_LIMIT", f"{request.remote_addr}:{g.current_user['id']}"):
            return limited_response
        data = _payload() or {}
        reason = data.get("reason", "")
        if not isinstance(reason, str) or len(reason.strip()) > 500:
            return jsonify(error="Report details must be 500 characters or fewer."), 400
        if not db.report_recipe(app.config["DB_PATH"], recipe_id, reason.strip(), g.current_user["id"]):
            return jsonify(error="Recipe not found, or you already reported it."), 409
        return jsonify(ok=True), 201

    @app.get("/api/recipes/<int:recipe_id>/comments")
    def get_comments(recipe_id):
        user_id = g.current_user["id"] if g.current_user else None
        admin = is_admin()
        if not db.recipe_comment_access(app.config["DB_PATH"], recipe_id, user_id, admin):
            return jsonify(error="Comments for this recipe are private while it is under review."), 403
        comments = db.recipe_comments(app.config["DB_PATH"], recipe_id, user_id, include_hidden=admin)
        return jsonify(comments=comments)

    @app.post("/api/recipes/<int:recipe_id>/comments")
    def post_comment(recipe_id):
        if denied := login_required_json():
            return denied
        if limited_response := limited("comments", "COMMENT_RATE_LIMIT", f"{request.remote_addr}:{g.current_user['id']}"):
            return limited_response
        if not db.recipe_comment_access(app.config["DB_PATH"], recipe_id, g.current_user["id"], is_admin()):
            return jsonify(error="Comments are available only on approved recipes or your own recipe."), 403
        data = _payload() or {}
        body = data.get("body")
        parent_id = data.get("parent_id")
        if not isinstance(body, str) or not body.strip() or len(body.strip()) > 500:
            return jsonify(error="Comments must contain 1–500 characters."), 400
        if parent_id is not None and (type(parent_id) is not int or parent_id < 1):
            return jsonify(error="Choose a valid comment to reply to."), 400
        comment_id = db.add_comment(app.config["DB_PATH"], recipe_id, g.current_user["id"], body.strip(), parent_id)
        if comment_id is None:
            return jsonify(error="Replies must be attached to a comment on this recipe."), 400
        return jsonify(ok=True, id=comment_id), 201

    @app.put("/api/comments/<int:comment_id>")
    def put_comment(comment_id):
        if denied := login_required_json():
            return denied
        if limited_response := limited("comments", "COMMENT_RATE_LIMIT", f"{request.remote_addr}:{g.current_user['id']}"):
            return limited_response
        context = db.comment_context(app.config["DB_PATH"], comment_id)
        if not context:
            return jsonify(error="Comment not found."), 404
        if not db.recipe_comment_access(app.config["DB_PATH"], context["recipe_id"], g.current_user["id"], is_admin()):
            return jsonify(error="This comment is private while its recipe is under review."), 403
        data = _payload() or {}
        body = data.get("body")
        if not isinstance(body, str) or not body.strip() or len(body.strip()) > 500:
            return jsonify(error="Comments must contain 1–500 characters."), 400
        if not db.edit_comment(app.config["DB_PATH"], comment_id, g.current_user["id"], body.strip()):
            return jsonify(error="You can edit only your own comments."), 403
        return jsonify(ok=True, edited=True)

    @app.post("/api/comments/<int:comment_id>/report")
    def post_comment_report(comment_id):
        if denied := login_required_json():
            return denied
        if limited_response := limited("comment-reports", "COMMENT_RATE_LIMIT", f"{request.remote_addr}:{g.current_user['id']}"):
            return limited_response
        context = db.comment_context(app.config["DB_PATH"], comment_id)
        if not context:
            return jsonify(error="Comment not found."), 404
        if not db.recipe_comment_access(app.config["DB_PATH"], context["recipe_id"], g.current_user["id"], is_admin()):
            return jsonify(error="This comment is private while its recipe is under review."), 403
        data = _payload() or {}
        reason = data.get("reason", "")
        if not isinstance(reason, str) or len(reason.strip()) > 500:
            return jsonify(error="Report details must be 500 characters or fewer."), 400
        if not db.report_comment(app.config["DB_PATH"], comment_id, g.current_user["id"], reason.strip()):
            return jsonify(error="Comment not found, your own comment, or already reported."), 409
        return jsonify(ok=True), 201

    @app.delete("/api/recipes/<int:recipe_id>")
    def delete_recipe(recipe_id):
        if denied := login_required_json():
            return denied
        if not db.delete_community_recipe(app.config["DB_PATH"], recipe_id, g.current_user["id"], is_admin()):
            return jsonify(error="You can delete only your own community recipes."), 404
        return jsonify(ok=True)

    @app.get("/admin")
    def admin_page():
        if not g.current_user:
            return redirect(url_for("login"))
        if not is_admin():
            abort(403)
        return render_template("admin.html", pending=db.pending_recipes(app.config["DB_PATH"]), reported=db.reported_recipes(app.config["DB_PATH"]), users=db.all_users(app.config["DB_PATH"]), community_recipes=db.community_recipes(app.config["DB_PATH"]), comments=db.admin_comments(app.config["DB_PATH"]))

    @app.post("/admin/users/<int:user_id>/reset-password")
    def admin_reset_password(user_id):
        if not is_admin():
            abort(403)
        target = next((user for user in db.all_users(app.config["DB_PATH"]) if user["id"] == user_id), None)
        if not target or is_admin(target):
            flash("That account cannot be reset from this page.", "error")
            return redirect(url_for("admin_page")), 404
        temporary_password = secrets.token_urlsafe(12)
        db.update_password(app.config["DB_PATH"], user_id, generate_password_hash(temporary_password), must_change=True)
        flash(f"One-time temporary password for {target['username']}: {temporary_password}. Copy it now; it is shown only once.", "success")
        return redirect(url_for("admin_page"))

    @app.post("/admin/recipes/<int:recipe_id>/<action>")
    def admin_moderate(recipe_id, action):
        if not is_admin():
            abort(403)
        if action == "delete":
            ok = db.delete_community_recipe(app.config["DB_PATH"], recipe_id, admin=True)
        else:
            ok = db.moderate_recipe(app.config["DB_PATH"], recipe_id, "approved" if action == "approve" else "rejected" if action == "reject" else "")
        if not ok:
            flash("That recipe could not be updated.", "error")
        else:
            flash("Recipe moderation updated.", "success")
        return redirect(url_for("admin_page"))

    @app.post("/admin/recipes/<int:recipe_id>/details")
    def admin_recipe_details(recipe_id):
        if not is_admin():
            abort(403)
        try:
            age = int(request.form.get("from", ""))
        except ValueError:
            age = -1
        meals = request.form.getlist("meals") or [request.form.get("meal", "")]
        if age not in AGES or not meals or any(meal not in MEALS for meal in meals):
            flash("Choose a supported age and meal.", "error")
        elif not db.admin_update_recipe_classification(app.config["DB_PATH"], recipe_id, age, meals):
            flash("That community recipe could not be updated.", "error")
        else:
            flash("Recipe age and meal updated.", "success")
        return redirect(url_for("admin_page"))

    @app.post("/admin/comments/<int:comment_id>/<action>")
    def admin_moderate_comment(comment_id, action):
        if not is_admin():
            abort(403)
        if not db.moderate_comment(app.config["DB_PATH"], comment_id, action):
            flash("That comment could not be updated.", "error")
        else:
            flash("Comment moderation updated.", "success")
        return redirect(url_for("admin_page"))

    @app.get("/api/account/state")
    def account_state():
        if denied := login_required_json():
            return denied
        user_id = g.current_user["id"]
        return jsonify(username=g.current_user["username"], is_admin=is_admin(), favorites=db.favorites(app.config["DB_PATH"], user_id),
                       made=db.made_recipes(app.config["DB_PATH"], user_id), import_pending=not bool(g.current_user.get("local_import_done")),
                       profiles=db.profile_list(app.config["DB_PATH"], user_id))

    @app.post("/api/account/import-local")
    def import_local():
        if denied := login_required_json():
            return denied
        user_id = g.current_user["id"]
        if g.current_user.get("local_import_done"):
            return jsonify(ok=True, already_done=True)
        data = _payload() or {}
        favorites_ids, made_ids = _clean_id_list(data.get("favorites", [])), _clean_id_list(data.get("made", []))
        if favorites_ids is None or made_ids is None:
            return jsonify(error="Favorites and made recipes must be lists of recipe IDs."), 400
        db.import_user_recipe_items(app.config["DB_PATH"], user_id, favorites_ids, made_ids)
        db.set_local_import_done(app.config["DB_PATH"], user_id)
        return jsonify(ok=True, favorites=db.favorites(app.config["DB_PATH"], user_id), made=db.made_recipes(app.config["DB_PATH"], user_id))

    @app.post("/api/account/skip-import")
    def skip_import():
        if denied := login_required_json():
            return denied
        db.set_local_import_done(app.config["DB_PATH"], g.current_user["id"])
        return jsonify(ok=True)

    def _clean_id_list(value):
        if not isinstance(value, list) or len(value) > 1000:
            return None
        ids = []
        for item in value:
            if type(item) is not int or item < 1:
                return None
            ids.append(item)
        return list(dict.fromkeys(ids))

    def user_recipe_action(table):
        if denied := login_required_json():
            return denied
        data = _payload() or {}
        recipe_id, active = data.get("recipe_id"), data.get("active")
        if type(recipe_id) is not int or recipe_id < 1 or type(active) is not bool:
            return jsonify(error="Provide a recipe ID and a true or false value."), 400
        if not db.set_user_recipe(app.config["DB_PATH"], table, g.current_user["id"], recipe_id, active):
            return jsonify(error="Recipe not found."), 404
        items = db.favorites(app.config["DB_PATH"], g.current_user["id"]) if table == "user_favorites" else db.made_recipes(app.config["DB_PATH"], g.current_user["id"])
        return jsonify(ok=True, items=items)

    @app.post("/api/account/favorites")
    def account_favorite():
        return user_recipe_action("user_favorites")

    @app.post("/api/account/made")
    def account_made():
        return user_recipe_action("user_made")

    @app.get("/api/profiles")
    def get_profiles():
        if denied := login_required_json():
            return denied
        return jsonify(profiles=db.profile_list(app.config["DB_PATH"], g.current_user["id"]))

    @app.post("/api/profiles")
    def post_profile():
        if denied := login_required_json():
            return denied
        data = _payload() or {}
        name, age, diet, allergens = data.get("name"), data.get("age"), data.get("diet", ""), data.get("allergens", [])
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
            return jsonify(error="Enter a profile nickname up to 40 characters."), 400
        if type(age) is not int or age not in PROFILE_AGES:
            return jsonify(error="Choose one of the listed age groups."), 400
        if not isinstance(diet, str) or diet not in DIETS:
            return jsonify(error="Choose one of the listed diet filters."), 400
        if not isinstance(allergens, list) or len(allergens) > 10 or any(not isinstance(x, str) or x not in ALLERGENS for x in allergens):
            return jsonify(error="Choose allergens from the listed options."), 400
        profile_id = data.get("id")
        if profile_id is not None and (type(profile_id) is not int or profile_id < 1):
            return jsonify(error="Invalid profile ID."), 400
        saved_id = db.save_profile(app.config["DB_PATH"], g.current_user["id"], profile_id, name.strip(), age, diet, list(dict.fromkeys(allergens)))
        if saved_id is None:
            return jsonify(error="Profile not found."), 404
        return jsonify(ok=True, profiles=db.profile_list(app.config["DB_PATH"], g.current_user["id"]))

    @app.delete("/api/profiles/<int:profile_id>")
    def remove_profile(profile_id):
        if denied := login_required_json():
            return denied
        if not db.delete_profile(app.config["DB_PATH"], g.current_user["id"], profile_id):
            return jsonify(error="Profile not found."), 404
        return jsonify(ok=True)

    @app.post("/api/chat")
    @app.post("/api/assistant")
    def assistant():
        limited_response = limited("assistant", "ASSISTANT_RATE_LIMIT")
        if limited_response:
            return limited_response
        data = _payload()
        if data is None:
            return jsonify(error="Send your question as a JSON object."), 400
        question = data.get("message", data.get("question"))
        if not isinstance(question, str) or not question.strip():
            return jsonify(error="Type a message before sending."), 400
        if len(question.strip()) > 500:
            return jsonify(error="Messages must be 500 characters or fewer."), 400
        question = question.strip()
        ctx, profile_allergens = {}, []
        profile_id = data.get("profile_id")
        profile = None
        if profile_id is not None:
            if not g.current_user or type(profile_id) is not int:
                return jsonify(error="Choose a saved profile after signing in."), 400
            profile = next((p for p in db.profile_list(app.config["DB_PATH"], g.current_user["id"]) if p["id"] == profile_id), None)
            if not profile:
                return jsonify(error="Profile not found."), 404
            profile_allergens = profile["allergens"]
            typed_allergies, pantry = data.get("allergies", ""), data.get("pantry", "")
            if not isinstance(typed_allergies, str) or len(typed_allergies.strip()) > 200:
                return jsonify(error="Allergies must be text within 200 characters."), 400
            if not isinstance(pantry, str) or len(pantry.strip()) > 200:
                return jsonify(error="Pantry must be text within 200 characters."), 400
            combined_allergens = list(dict.fromkeys(profile_allergens + [x.strip() for x in typed_allergies.split(",") if x.strip()]))
            ctx = {"age": AGE_LABELS[profile["age"]], "diet": profile["diet"], "allergies": ", ".join(combined_allergens), "pantry": pantry.strip()}
        else:
            aliases = {"age": ("age_range", "child_age", "age"), "diet": ("diet",), "allergies": ("allergies",), "pantry": ("pantry",)}
            for key, names in aliases.items():
                value = next((data[name] for name in names if name in data), "")
                if not isinstance(value, str) or len(value.strip()) > 200:
                    return jsonify(error=f"{key.title()} must be text within 200 characters."), 400
                ctx[key] = value.strip()
        raw_history = data.get("history", [])
        if not isinstance(raw_history, list):
            return jsonify(error="Chat history must be a list."), 400
        history = []
        for item in raw_history[-10:]:
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                return jsonify(error="Chat history may contain only user and assistant messages."), 400
            content = item.get("content")
            if not isinstance(content, str) or not content.strip() or len(content.strip()) > 500:
                return jsonify(error="Each chat history message must contain 1–500 characters."), 400
            history.append({"role": item["role"], "content": content.strip()})
        reply = ask_llm(question, ctx, history)
        model_errors = ("Ollama returned HTTP ", "Could not reach Ollama.", "Ollama returned an unreadable response.", "The helper could not answer right now.")
        if request.path == "/api/chat" and reply.startswith(model_errors):
            return jsonify(error="The meal buddy could not connect just now. Check that Ollama is running, then try again."), 503
        warning_terms = list(dict.fromkeys(profile_allergens + [x.strip() for x in ctx["allergies"].split(",") if x.strip()]))
        hits = _allergy_mentions(reply, ",".join(warning_terms))
        if hits:
            reply += "\n\nSafety check: this reply mentions " + ", ".join(hits) + ", which you marked as an allergy. Check every ingredient and label before cooking."
        return jsonify(reply=reply)

    @app.get("/sw.js")
    def service_worker():
        response = app.send_static_file("sw.js")
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.errorhandler(400)
    def bad_request(error):
        return error_response(400, "That request needs another look", error.description or "Please refresh the page and try again.")

    @app.errorhandler(403)
    def forbidden(_error):
        return error_response(403, "This page is just for the family admin", "You do not have permission to view this page.")

    @app.errorhandler(404)
    def not_found(_error):
        return error_response(404, "We couldn’t find that page", "The page may have moved or the address may be mistyped.")

    @app.errorhandler(413)
    def too_large(_error):
        return error_response(413, "That message is too big", "Please shorten the submitted text and try again.")

    @app.errorhandler(429)
    def too_many_requests(_error):
        return error_response(429, "Let’s take a short break", "Too many requests. Please wait a little and try again.")

    @app.errorhandler(500)
    def internal_error(error):
        app.logger.exception("Unhandled request error: %s", error)
        return error_response(500, "Something went wrong", "Please try again in a moment.")

    @app.errorhandler(HTTPException)
    def http_error(error):
        if error.code in {400, 403, 404, 413, 429}:
            return error_response(error.code, "A little pause in the kitchen", error.description)
        return error

    return app


def ask_llm(question, ctx, history=None):
    if os.getenv("MEAL_BUDDY_DEMO", "").strip().lower() in {"1", "true", "yes", "on"}:
        return "Demo mode is on. Try a simple, soft meal idea using ingredients you already know work for your family. Check every label and texture before serving; this is not an allergy-safety guarantee."
    prompt = (f"Child age range: {ctx['age']}\nDiet: {ctx['diet']}\nAllergies to avoid entirely: {ctx['allergies']}\n"
              f"Ingredients at home: {ctx['pantry'] or 'not specified'}\n\nParent's request: {question}")
    system = ("You are a cautious family meal-idea helper in a kids' food app. Never include a listed allergen or break the diet. "
              "Never promise a recipe is allergy-safe; remind parents to read labels and avoid cross-contact. Give food ideas only, "
              "not medical advice, supplements, weight-loss advice, or calorie/vitamin numbers. Mention texture and choking cautions "
              "when relevant. If the child may be under 12 months, suggest checking with a pediatrician. Keep the reply under 180 words.")
    try:
        messages = [{"role": "system", "content": system}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": prompt})
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip().rstrip("/")
        model = os.getenv("OLLAMA_MODEL", "llama3.2").strip()
        payload = json.dumps({"model": model, "messages": messages, "stream": False, "options": {"temperature": 0.4, "num_predict": 250}}).encode("utf-8")
        req = urllib.request.Request(f"{base_url}/api/chat", data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=float(os.getenv("OLLAMA_TIMEOUT", "120"))) as response:
            result = json.loads(response.read().decode("utf-8"))
        return (result.get("message", {}).get("content") or "").strip() or "The helper had no suggestion. Please try again."
    except urllib.error.HTTPError as error:
        return f"Ollama returned HTTP {error.code}. Check that the selected model is installed and Ollama is up to date."
    except urllib.error.URLError:
        return "Could not reach Ollama. Start Ollama, confirm the model is installed, and check OLLAMA_BASE_URL."
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return "Ollama returned an unreadable response. Check the model and try again."
    except Exception:
        return "The helper could not answer right now. Please try again in a moment."


if __name__ == "__main__":
    create_app().run(debug=os.getenv("FLASK_DEBUG", "").lower() in {"1", "true", "yes", "on"})
