from flask import Flask, render_template, request, redirect, url_for, session, flash
import csv
import io
import os
import sqlite3
import random
from difflib import SequenceMatcher

app = Flask(__name__)
app.secret_key = "local-lexlet-profile-switcher"
DB_NAME = "flashcards.db"
DECK_HEADERS = {"deck", "deck name", "deck_name", "set", "set name"}
TERM_HEADERS = {"term", "front", "word", "question"}
DEFINITION_HEADERS = {"definition", "definition / clue", "clue", "back", "answer", "meaning"}


def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_profile(conn, name):
    existing = conn.execute("SELECT id FROM profiles WHERE lower(name) = lower(?)", (name,)).fetchone()
    if existing:
        return existing["id"]
    cursor = conn.execute("INSERT INTO profiles (name) VALUES (?)", (name,))
    return cursor.lastrowid


def get_profiles(conn):
    return conn.execute("SELECT id, name FROM profiles ORDER BY name COLLATE NOCASE").fetchall()


def get_active_profile_id(conn):
    stored_profile_id = session.get("active_profile_id")
    if stored_profile_id:
        existing = conn.execute("SELECT id FROM profiles WHERE id = ?", (stored_profile_id,)).fetchone()
        if existing:
            return stored_profile_id

    eve = conn.execute("SELECT id FROM profiles WHERE lower(name) = 'eve'").fetchone()
    if eve:
        session["active_profile_id"] = eve["id"]
        return eve["id"]

    first_profile = conn.execute("SELECT id FROM profiles ORDER BY id LIMIT 1").fetchone()
    if first_profile:
        session["active_profile_id"] = first_profile["id"]
        return first_profile["id"]
    return None


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            profile_id INTEGER,
            UNIQUE(name, profile_id),
            FOREIGN KEY (profile_id) REFERENCES profiles (id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            term TEXT NOT NULL,
            definition TEXT NOT NULL,
            deck_id INTEGER,
            FOREIGN KEY (deck_id) REFERENCES decks (id)
        )
    """)

    card_columns = {row["name"] for row in conn.execute("PRAGMA table_info(cards)").fetchall()}
    if "deck_id" not in card_columns:
        conn.execute("ALTER TABLE cards ADD COLUMN deck_id INTEGER")

    deck_columns = {row["name"] for row in conn.execute("PRAGMA table_info(decks)").fetchall()}
    if "profile_id" not in deck_columns:
        conn.execute("ALTER TABLE decks ADD COLUMN profile_id INTEGER")

    eve_profile_id = ensure_profile(conn, "eve")
    ensure_profile(conn, "eddie")

    conn.execute("UPDATE decks SET profile_id = ? WHERE profile_id IS NULL", (eve_profile_id,))
    default_deck_id = ensure_deck(conn, "General", eve_profile_id)
    conn.execute("UPDATE cards SET deck_id = ? WHERE deck_id IS NULL", (default_deck_id,))
    conn.commit()
    conn.close()


def ensure_deck(conn, name, profile_id):
    existing = conn.execute(
        "SELECT id FROM decks WHERE lower(name) = lower(?) AND profile_id = ?",
        (name, profile_id),
    ).fetchone()
    if existing:
        return existing["id"]
    cursor = conn.execute(
        "INSERT INTO decks (name, profile_id) VALUES (?, ?)",
        (name, profile_id),
    )
    return cursor.lastrowid


def normalize_csv_header(value):
    return (value or "").strip().lower().replace("-", " ").replace("_", " ")


def find_csv_column(headers, accepted_names):
    for index, header in enumerate(headers):
        if normalize_csv_header(header) in accepted_names:
            return index
    return None


def uploaded_csv_rows(uploaded_file, fallback_deck_name, allow_unheaded_deck_column=False):
    raw = uploaded_file.read()
    if not raw:
        return []

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel

    rows = [
        [cell.strip() for cell in row]
        for row in csv.reader(io.StringIO(text), dialect)
        if any(cell.strip() for cell in row)
    ]
    if not rows:
        return []

    first_row = rows[0]
    deck_index = find_csv_column(first_row, DECK_HEADERS)
    term_index = find_csv_column(first_row, TERM_HEADERS)
    definition_index = find_csv_column(first_row, DEFINITION_HEADERS)
    has_header = term_index is not None and definition_index is not None

    if has_header:
        data_rows = rows[1:]
    elif len(first_row) >= 3 and allow_unheaded_deck_column:
        deck_index, term_index, definition_index = 0, 1, 2
        data_rows = rows
    else:
        deck_index, term_index, definition_index = None, 0, 1
        data_rows = rows

    parsed_rows = []
    for row in data_rows:
        if len(row) <= max(term_index, definition_index):
            continue

        term = row[term_index].strip()
        definition = row[definition_index].strip()
        deck_name = fallback_deck_name
        if deck_index is not None and len(row) > deck_index and row[deck_index].strip():
            deck_name = row[deck_index].strip()

        if term and definition and deck_name:
            parsed_rows.append({
                "deck_name": deck_name,
                "term": term,
                "definition": definition,
            })

    return parsed_rows


def get_all_decks(conn, profile_id):
    return conn.execute("""
        SELECT d.id, d.name, COUNT(c.id) AS card_count
        FROM decks d
        LEFT JOIN cards c ON c.deck_id = d.id
        WHERE d.profile_id = ?
        GROUP BY d.id, d.name
        ORDER BY d.name COLLATE NOCASE
    """, (profile_id,)).fetchall()


def get_deck(conn, deck_id, profile_id):
    return conn.execute("""
        SELECT d.id, d.name, COUNT(c.id) AS card_count
        FROM decks d
        LEFT JOIN cards c ON c.deck_id = d.id
        WHERE d.id = ? AND d.profile_id = ?
        GROUP BY d.id, d.name
    """, (deck_id, profile_id)).fetchone()


def similarity(a, b):
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def parse_card_order(raw_order):
    if not raw_order:
        return []
    card_ids = []
    for piece in raw_order.split(","):
        piece = piece.strip()
        if piece.isdigit():
            card_ids.append(int(piece))
    return card_ids


def render_test_template(**context):
    template = "_test_shell.html" if request.headers.get("X-Requested-With") == "XMLHttpRequest" else "test.html"
    return render_template(template, **context)


def render_test_session(decks, selected_deck, card, total_cards, correct_count, answered_count,
                        remaining_order="", result=False, correct=None, user_answer="",
                        real_answer="", score=0, session_complete=False):
    return render_test_template(
        decks=decks,
        selected_deck=selected_deck,
        card=card,
        total_cards=total_cards,
        correct_count=correct_count,
        answered_count=answered_count,
        remaining_order=remaining_order,
        result=result,
        correct=correct,
        user_answer=user_answer,
        real_answer=real_answer,
        score=score,
        session_complete=session_complete,
    )


@app.context_processor
def inject_profile_state():
    conn = get_db()
    profiles = get_profiles(conn)
    active_profile_id = get_active_profile_id(conn)
    active_profile = conn.execute(
        "SELECT id, name FROM profiles WHERE id = ?",
        (active_profile_id,),
    ).fetchone()
    conn.close()
    return {"profiles": profiles, "active_profile": active_profile}


@app.route("/switch-profile", methods=["POST"])
def switch_profile():
    conn = get_db()
    profile_id = request.form.get("profile_id", type=int)
    profile = conn.execute("SELECT id FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    conn.close()
    if profile:
        session["active_profile_id"] = profile["id"]
    return redirect(request.form.get("next") or url_for("index"))


@app.route("/")
def index():
    conn = get_db()
    active_profile_id = get_active_profile_id(conn)
    decks = get_all_decks(conn, active_profile_id)
    total_cards = conn.execute("""
        SELECT COUNT(*)
        FROM cards c
        JOIN decks d ON d.id = c.deck_id
        WHERE d.profile_id = ?
    """, (active_profile_id,)).fetchone()[0]
    conn.close()
    return render_template("index.html", decks=decks, total_cards=total_cards)


@app.route("/deck/<int:deck_id>")
def deck_detail(deck_id):
    conn = get_db()
    active_profile_id = get_active_profile_id(conn)
    deck = get_deck(conn, deck_id, active_profile_id)
    if not deck:
        conn.close()
        return redirect(url_for("index"))

    cards = conn.execute(
        "SELECT * FROM cards WHERE deck_id = ? ORDER BY id DESC",
        (deck_id,),
    ).fetchall()
    conn.close()
    return render_template("deck.html", deck=deck, cards=cards)


@app.route("/decks/create", methods=["POST"])
def create_deck():
    name = request.form.get("name", "").strip()
    if name:
        conn = get_db()
        active_profile_id = get_active_profile_id(conn)
        deck_id = ensure_deck(conn, name, active_profile_id)
        conn.commit()
        conn.close()
        return redirect(url_for("deck_detail", deck_id=deck_id))
    return redirect(url_for("index"))


@app.route("/decks/upload", methods=["POST"])
def upload_deck_csv():
    uploaded_file = request.files.get("csv_file")
    if not uploaded_file or not uploaded_file.filename:
        flash("Choose a CSV file to upload.")
        return redirect(url_for("index"))

    if not uploaded_file.filename.lower().endswith(".csv"):
        flash("Upload a .csv file.")
        return redirect(url_for("index"))

    submitted_deck_name = request.form.get("deck_name", "").strip()
    deck_name = submitted_deck_name or os.path.splitext(os.path.basename(uploaded_file.filename))[0].strip()

    imported_rows = uploaded_csv_rows(
        uploaded_file,
        deck_name or "Imported deck",
        allow_unheaded_deck_column=not submitted_deck_name,
    )
    if not imported_rows:
        flash("No valid cards found. Use term,definition columns or deck,term,definition columns.")
        return redirect(url_for("index"))

    conn = get_db()
    active_profile_id = get_active_profile_id(conn)
    deck_ids = {}
    for row in imported_rows:
        deck_id = deck_ids.get(row["deck_name"])
        if deck_id is None:
            deck_id = ensure_deck(conn, row["deck_name"], active_profile_id)
            deck_ids[row["deck_name"]] = deck_id
        conn.execute(
            "INSERT INTO cards (term, definition, deck_id) VALUES (?, ?, ?)",
            (row["term"], row["definition"], deck_id),
        )

    conn.commit()
    conn.close()

    deck_label = "deck" if len(deck_ids) == 1 else "decks"
    card_label = "card" if len(imported_rows) == 1 else "cards"
    flash(f"Imported {len(imported_rows)} {card_label} into {len(deck_ids)} {deck_label}.")

    if len(deck_ids) == 1:
        return redirect(url_for("deck_detail", deck_id=next(iter(deck_ids.values()))))
    return redirect(url_for("index"))


@app.route("/deck/<int:deck_id>/delete", methods=["POST"])
def delete_deck(deck_id):
    conn = get_db()
    active_profile_id = get_active_profile_id(conn)
    deck = get_deck(conn, deck_id, active_profile_id)
    if not deck:
        conn.close()
        return redirect(url_for("index"))

    conn.execute("DELETE FROM cards WHERE deck_id = ?", (deck_id,))
    conn.execute("DELETE FROM decks WHERE id = ?", (deck_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("index"))


@app.route("/add", methods=["GET", "POST"])
def add_card():
    conn = get_db()
    active_profile_id = get_active_profile_id(conn)
    decks = get_all_decks(conn, active_profile_id)
    selected_deck_id = request.args.get("deck_id", type=int)

    if request.method == "POST":
        term = request.form.get("term", "").strip()
        definition = request.form.get("definition", "").strip()
        deck_name = request.form.get("new_deck_name", "").strip()
        deck_id = request.form.get("deck_id", type=int)

        if deck_name:
            deck_id = ensure_deck(conn, deck_name, active_profile_id)

        deck = get_deck(conn, deck_id, active_profile_id) if deck_id else None
        if term and definition and deck:
            conn.execute(
                "INSERT INTO cards (term, definition, deck_id) VALUES (?, ?, ?)",
                (term, definition, deck_id),
            )
            conn.commit()
            conn.close()
            return redirect(url_for("deck_detail", deck_id=deck_id))

        decks = get_all_decks(conn, active_profile_id)
        selected_deck_id = deck_id

    conn.close()
    return render_template("add.html", decks=decks, selected_deck_id=selected_deck_id)


@app.route("/delete/<int:card_id>", methods=["POST"])
def delete_card(card_id):
    conn = get_db()
    active_profile_id = get_active_profile_id(conn)
    card = conn.execute("SELECT deck_id FROM cards WHERE id = ?", (card_id,)).fetchone()
    if card and not get_deck(conn, card["deck_id"], active_profile_id):
        conn.close()
        return redirect(url_for("index"))

    conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    conn.commit()
    conn.close()
    if card and card["deck_id"]:
        return redirect(url_for("deck_detail", deck_id=card["deck_id"]))
    return redirect(url_for("index"))


@app.route("/study")
def study():
    conn = get_db()
    active_profile_id = get_active_profile_id(conn)
    decks = get_all_decks(conn, active_profile_id)
    selected_deck_id = request.args.get("deck_id", type=int)
    selected_deck = get_deck(conn, selected_deck_id, active_profile_id) if selected_deck_id else None
    cards = []
    if selected_deck:
        cards = conn.execute("SELECT * FROM cards WHERE deck_id = ?", (selected_deck_id,)).fetchall()
    conn.close()
    cards = list(cards)
    random.shuffle(cards)
    return render_template("study.html", cards=cards, decks=decks, selected_deck=selected_deck)


@app.route("/test", methods=["GET", "POST"])
def test():
    conn = get_db()
    active_profile_id = get_active_profile_id(conn)
    decks = get_all_decks(conn, active_profile_id)
    selected_deck_id = request.values.get("deck_id", type=int)
    selected_deck = get_deck(conn, selected_deck_id, active_profile_id) if selected_deck_id else None

    if not selected_deck:
        conn.close()
        return render_test_template(decks=decks, selected_deck=None, result=False)

    cards = list(conn.execute("SELECT * FROM cards WHERE deck_id = ?", (selected_deck_id,)).fetchall())
    conn.close()
    if not cards:
        return render_test_template(decks=decks, selected_deck=selected_deck, result=False, card=None)

    cards_by_id = {card["id"]: card for card in cards}

    if request.method == "POST":
        action = request.form.get("action", "check")
        remaining_order = parse_card_order(request.form.get("remaining_order", ""))
        correct_count = request.form.get("correct_count", type=int, default=0)
        answered_count = request.form.get("answered_count", type=int, default=0)

        if action == "continue":
            if not remaining_order:
                return render_test_session(
                    decks=decks,
                    selected_deck=selected_deck,
                    card=None,
                    total_cards=len(cards),
                    correct_count=correct_count,
                    answered_count=answered_count,
                    session_complete=True,
                    result=True,
                    correct=True,
                )

            next_card = cards_by_id.get(remaining_order[0])
            if not next_card:
                return redirect(url_for("test", deck_id=selected_deck_id))

            return render_test_session(
                decks=decks,
                selected_deck=selected_deck,
                card=next_card,
                total_cards=len(cards),
                correct_count=correct_count,
                answered_count=answered_count,
                remaining_order=",".join(str(existing_id) for existing_id in remaining_order[1:]),
            )

        card_id = int(request.form["card_id"])
        user_answer = request.form.get("answer", "")
        card = cards_by_id.get(card_id)
        if not card:
            return redirect(url_for("test", deck_id=selected_deck_id))

        next_order = [existing_id for existing_id in remaining_order if existing_id in cards_by_id and existing_id != card_id]
        score = similarity(user_answer, card["term"])
        correct = score >= 0.8
        updated_correct_count = correct_count + (1 if correct else 0)
        updated_answered_count = answered_count + 1

        return render_test_session(
            decks=decks,
            selected_deck=selected_deck,
            card=card,
            total_cards=len(cards),
            correct_count=updated_correct_count,
            answered_count=updated_answered_count,
            remaining_order=",".join(str(existing_id) for existing_id in next_order),
            result=True,
            correct=correct,
            user_answer=user_answer,
            real_answer=card["term"],
            score=round(score * 100),
            session_complete=False,
        )

    card_order = [card["id"] for card in cards]
    random.shuffle(card_order)
    first_card = cards_by_id[card_order[0]]
    return render_test_session(
        decks=decks,
        selected_deck=selected_deck,
        card=first_card,
        total_cards=len(cards),
        correct_count=0,
        answered_count=0,
        remaining_order=",".join(str(existing_id) for existing_id in card_order[1:]),
    )


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
