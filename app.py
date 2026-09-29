import sqlite3
from datetime import datetime
from functools import wraps
from io import BytesIO
from pathlib import Path

from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas
from werkzeug.security import check_password_hash, generate_password_hash

APP_DIR = Path(__file__).resolve().parent
DATABASE = APP_DIR / "logistyka.db"

FUEL_L_PER_100KM = 30
FUEL_PRICE_PLN = 6.50
AVG_SPEED_KMH = 70
WAREHOUSE_LAT = 52.2297
WAREHOUSE_LNG = 21.0122
WAREHOUSE_NAME = "Magazyn centralny · Warszawa"

DESTINATIONS = [
    {"name": "Warszawa", "lat": 52.2297, "lng": 21.0122},
    {"name": "Kraków", "lat": 50.0647, "lng": 19.9450},
    {"name": "Gdańsk", "lat": 54.3520, "lng": 18.6466},
    {"name": "Wrocław", "lat": 51.1079, "lng": 17.0385},
    {"name": "Poznań", "lat": 52.4064, "lng": 16.9252},
    {"name": "Łódź", "lat": 51.7592, "lng": 19.4560},
]

app = Flask(__name__)
app.secret_key = "dev-prototyp-logistyka"

_FONT_REGULAR = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"


def _register_pdf_fonts():
    global _FONT_REGULAR, _FONT_BOLD
    candidates = [
        (Path(r"C:\Windows\Fonts\arial.ttf"), Path(r"C:\Windows\Fonts\arialbd.ttf")),
        (Path(r"C:\Windows\Fonts\calibri.ttf"), Path(r"C:\Windows\Fonts\calibrib.ttf")),
        (
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ),
    ]
    for regular, bold in candidates:
        if regular.exists():
            pdfmetrics.registerFont(TTFont("LogiRegular", str(regular)))
            _FONT_REGULAR = "LogiRegular"
            if bold.exists():
                pdfmetrics.registerFont(TTFont("LogiBold", str(bold)))
                _FONT_BOLD = "LogiBold"
            else:
                _FONT_BOLD = "LogiRegular"
            return


_register_pdf_fonts()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_eta(minutes):
    minutes = int(minutes or 0)
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} godz. {rest} min"
    if hours:
        return f"{hours} godz."
    return f"{rest} min"


def calculate_transport(distance_km):
    fuel_cost = round(distance_km * (FUEL_L_PER_100KM / 100.0) * FUEL_PRICE_PLN, 2)
    eta_minutes = int(round((distance_km / AVG_SPEED_KMH) * 60))
    return fuel_cost, eta_minutes


def destination_by_name(name):
    for item in DESTINATIONS:
        if item["name"] == name:
            return item
    return DESTINATIONS[0]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def table_columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def add_column_if_missing(db, table, column, definition):
    if column not in table_columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def add_log(db, action, username=None):
    if username is None:
        user = getattr(g, "user", None)
        username = user["username"] if user is not None else "system"
    db.execute(
        "INSERT INTO logs (username, action, created_at) VALUES (?, ?, ?)",
        (username, action, now_str()),
    )


def init_db():
    db = sqlite3.connect(DATABASE)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity >= 0),
            location TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brand_model TEXT NOT NULL,
            registration TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK (status IN ('Wolny', 'W trasie'))
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            vehicle_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            created_at TEXT NOT NULL,
            FOREIGN KEY (product_id) REFERENCES products(id),
            FOREIGN KEY (vehicle_id) REFERENCES vehicles(id)
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('menedzer', 'kierowca')),
            vehicle_id INTEGER,
            FOREIGN KEY (vehicle_id) REFERENCES vehicles(id)
        );

        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )

    add_column_if_missing(db, "products", "min_quantity", "INTEGER NOT NULL DEFAULT 20")
    add_column_if_missing(db, "orders", "status", "TEXT NOT NULL DEFAULT 'W realizacji'")
    add_column_if_missing(db, "orders", "driver_id", "INTEGER")
    add_column_if_missing(db, "orders", "distance_km", "REAL NOT NULL DEFAULT 0")
    add_column_if_missing(db, "orders", "fuel_cost", "REAL NOT NULL DEFAULT 0")
    add_column_if_missing(db, "orders", "eta_minutes", "INTEGER NOT NULL DEFAULT 0")
    add_column_if_missing(db, "orders", "destination", "TEXT NOT NULL DEFAULT 'Warszawa'")
    add_column_if_missing(db, "orders", "dest_lat", "REAL NOT NULL DEFAULT 52.2297")
    add_column_if_missing(db, "orders", "dest_lng", "REAL NOT NULL DEFAULT 21.0122")

    if db.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
        db.executemany(
            """
            INSERT INTO products (name, quantity, location, min_quantity)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("Palety EURO", 120, "Hala A / Rząd 3", 30),
                ("Kartony 40x40", 450, "Hala B / Regał 12", 80),
                ("Folia stretch", 80, "Magazyn folii", 40),
            ],
        )
        db.executemany(
            "INSERT INTO vehicles (brand_model, registration, status) VALUES (?, ?, ?)",
            [
                ("Mercedes Actros", "WA 12345", "Wolny"),
                ("Volvo FH16", "KR 98765", "W trasie"),
                ("Scania R450", "GD 45678", "Wolny"),
            ],
        )

    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        vehicle = db.execute("SELECT id FROM vehicles ORDER BY id LIMIT 1").fetchone()
        vehicle_id = vehicle[0] if vehicle else None
        db.execute(
            """
            INSERT INTO users (username, password_hash, role, vehicle_id)
            VALUES (?, ?, 'menedzer', NULL)
            """,
            ("menedzer", generate_password_hash("menedzer123")),
        )
        db.execute(
            """
            INSERT INTO users (username, password_hash, role, vehicle_id)
            VALUES (?, ?, 'kierowca', ?)
            """,
            ("kierowca", generate_password_hash("kierowca123"), vehicle_id),
        )
        add_log(db, "System utworzył konta startowe: menedzer i kierowca.", "system")

    driver = db.execute(
        "SELECT id, vehicle_id FROM users WHERE username = 'kierowca'"
    ).fetchone()
    if driver:
        db.execute(
            """
            UPDATE orders
            SET driver_id = ?
            WHERE driver_id IS NULL AND vehicle_id = ?
            """,
            (driver[0], driver[1]),
        )

    db.execute(
        "UPDATE products SET min_quantity = 90 WHERE name = 'Folia stretch' AND min_quantity = 20"
    )

    db.commit()
    db.close()


def fetch_order_details(db, order_id):
    return db.execute(
        """
        SELECT
            o.id,
            o.quantity,
            o.created_at,
            o.status,
            o.driver_id,
            o.distance_km,
            o.fuel_cost,
            o.eta_minutes,
            o.destination,
            o.dest_lat,
            o.dest_lng,
            o.vehicle_id,
            p.name AS product_name,
            v.brand_model AS vehicle_name,
            v.registration,
            v.status AS vehicle_status,
            u.username AS driver_name
        FROM orders o
        JOIN products p ON p.id = o.product_id
        JOIN vehicles v ON v.id = o.vehicle_id
        LEFT JOIN users u ON u.id = o.driver_id
        WHERE o.id = ?
        """,
        (order_id,),
    ).fetchone()


def driver_can_see_order(user, order):
    if order is None or user is None:
        return False
    if user["role"] == "menedzer":
        return True
    if order["driver_id"] and order["driver_id"] == user["id"]:
        return True
    return bool(user["vehicle_id"] and order["vehicle_id"] == user["vehicle_id"])


def build_waybill_pdf(order):
    buffer = BytesIO()
    page = pdf_canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    navy = HexColor("#0f172a")
    blue = HexColor("#1d4ed8")
    gray = HexColor("#475569")

    page.setFillColor(navy)
    page.rect(0, height - 90, width, 90, fill=1, stroke=0)
    page.setFillColor(white)
    page.setFont(_FONT_BOLD, 18)
    page.drawString(40, height - 42, "LogiFlow")
    page.setFont(_FONT_REGULAR, 11)
    page.drawString(40, height - 62, "List przewozowy / dokument zlecenia transportowego")
    page.setFont(_FONT_BOLD, 12)
    page.drawRightString(width - 40, height - 48, f"Zlecenie nr {order['id']}")

    page.setFillColor(blue)
    page.rect(40, height - 145, width - 80, 36, fill=1, stroke=0)
    page.setFillColor(white)
    page.setFont(_FONT_BOLD, 13)
    page.drawString(52, height - 132, "LIST PRZEWOZOWY")

    rows = [
        ("Numer zlecenia", f"#{order['id']}"),
        ("Status", order["status"]),
        ("Data utworzenia", order["created_at"]),
        ("Nazwa towaru", order["product_name"]),
        ("Ilość towaru", str(order["quantity"])),
        ("Pojazd (marka / model)", order["vehicle_name"]),
        ("Numer rejestracyjny", order["registration"]),
        ("Kierowca", order["driver_name"] or "—"),
        ("Cel dostawy", order["destination"]),
        ("Odległość", f"{order['distance_km']} km"),
        ("Szacowany koszt paliwa", f"{order['fuel_cost']:.2f} zł"),
        ("Szacowany czas (ETA)", format_eta(order["eta_minutes"])),
    ]

    y = height - 185
    page.setStrokeColor(HexColor("#e2e8f0"))
    for label, value in rows:
        page.setFillColor(HexColor("#f8fafc"))
        page.roundRect(40, y - 6, width - 80, 28, 4, fill=1, stroke=1)
        page.setFillColor(gray)
        page.setFont(_FONT_REGULAR, 8)
        page.drawString(52, y + 10, label.upper())
        page.setFillColor(navy)
        page.setFont(_FONT_BOLD, 11)
        page.drawString(52, y - 2, str(value))
        y -= 34
        if y < 60:
            page.showPage()
            y = height - 60

    page.setFillColor(gray)
    page.setFont(_FONT_REGULAR, 8)
    page.drawString(
        40,
        40,
        "Dokument wygenerowany automatycznie przez system LogiFlow. Prototyp projektu stypendialnego.",
    )
    page.showPage()
    page.save()
    buffer.seek(0)
    return buffer


@app.before_request
def load_logged_in_user():
    user_id = session.get("user_id")
    g.user = None
    if user_id is not None:
        g.user = get_db().execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()


@app.context_processor
def inject_globals():
    return {
        "current_user": getattr(g, "user", None),
        "format_eta": format_eta,
        "warehouse_lat": WAREHOUSE_LAT,
        "warehouse_lng": WAREHOUSE_LNG,
        "warehouse_name": WAREHOUSE_NAME,
    }


def login_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login", next=request.path))
            if roles and g.user["role"] not in roles:
                flash("Brak uprawnień do tej części systemu.", "error")
                return redirect(url_for("home"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


def safe_next_url(value):
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return None


@app.route("/")
def home():
    if g.user is None:
        return redirect(url_for("login"))
    if g.user["role"] == "kierowca":
        return redirect(url_for("kierowca"))
    return redirect(url_for("panel"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("home"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Nieprawidłowa nazwa użytkownika lub hasło.", "error")
            return redirect(url_for("login"))

        session.clear()
        session["user_id"] = user["id"]
        add_log(get_db(), f"Użytkownik {user['username']} zalogował się.", user["username"])
        get_db().commit()
        nxt = safe_next_url(request.form.get("next") or request.args.get("next"))
        if nxt:
            return redirect(nxt)
        if user["role"] == "kierowca":
            return redirect(url_for("kierowca"))
        return redirect(url_for("panel"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    username = g.user["username"] if g.user is not None else "nieznany"
    if g.user is not None:
        add_log(get_db(), f"Użytkownik {username} wylogował się.", username)
        get_db().commit()
    session.clear()
    flash("Wylogowano z systemu.", "success")
    return redirect(url_for("login"))


@app.route("/panel")
@login_required("menedzer")
def panel():
    db = get_db()
    wolne = db.execute(
        "SELECT COUNT(*) FROM vehicles WHERE status = 'Wolny'"
    ).fetchone()[0]
    w_trasie = db.execute(
        "SELECT COUNT(*) FROM vehicles WHERE status = 'W trasie'"
    ).fetchone()[0]
    products = db.execute(
        "SELECT name, quantity, min_quantity FROM products ORDER BY name"
    ).fetchall()
    orders_count = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    stock_total = db.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM products"
    ).fetchone()[0]
    alerts = db.execute(
        """
        SELECT name, quantity, min_quantity, location
        FROM products
        WHERE quantity < min_quantity
        ORDER BY quantity ASC
        """
    ).fetchall()
    logs = db.execute(
        "SELECT username, action, created_at FROM logs ORDER BY id DESC LIMIT 40"
    ).fetchall()
    return render_template(
        "panel.html",
        wolne=wolne,
        w_trasie=w_trasie,
        vehicles_total=wolne + w_trasie,
        orders_count=orders_count,
        stock_total=stock_total,
        chart_products=[p["name"] for p in products],
        chart_quantities=[p["quantity"] for p in products],
        alerts=alerts,
        logs=logs,
    )


@app.route("/magazyn", methods=["GET", "POST"])
@login_required("menedzer")
def magazyn():
    db = get_db()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        location = request.form.get("location", "").strip()
        quantity_raw = request.form.get("quantity", "").strip()
        min_raw = request.form.get("min_quantity", "20").strip()

        if not name or not location or not quantity_raw:
            flash("Uzupełnij wszystkie pola produktu.", "error")
            return redirect(url_for("magazyn"))

        try:
            quantity = int(quantity_raw)
            min_quantity = int(min_raw)
            if quantity < 0 or min_quantity < 0:
                raise ValueError
        except ValueError:
            flash("Ilość i limit alarmowy muszą być liczbami całkowitymi ≥ 0.", "error")
            return redirect(url_for("magazyn"))

        db.execute(
            """
            INSERT INTO products (name, quantity, location, min_quantity)
            VALUES (?, ?, ?, ?)
            """,
            (name, quantity, location, min_quantity),
        )
        add_log(db, f"Użytkownik {g.user['username']} dodał produkt {name} (ilość {quantity}).")
        db.commit()
        flash("Produkt został dodany do magazynu.", "success")
        return redirect(url_for("magazyn"))

    query = request.args.get("q", "").strip()
    if query:
        products = db.execute(
            """
            SELECT id, name, quantity, location, min_quantity
            FROM products
            WHERE name LIKE ?
            ORDER BY name
            """,
            (f"%{query}%",),
        ).fetchall()
    else:
        products = db.execute(
            """
            SELECT id, name, quantity, location, min_quantity
            FROM products
            ORDER BY name
            """
        ).fetchall()
    return render_template("magazyn.html", products=products, query=query)


@app.route("/flota", methods=["GET", "POST"])
@login_required("menedzer")
def flota():
    db = get_db()
    if request.method == "POST":
        brand_model = request.form.get("brand_model", "").strip()
        registration = request.form.get("registration", "").strip().upper()
        status = request.form.get("status", "").strip()

        if not brand_model or not registration or status not in ("Wolny", "W trasie"):
            flash("Uzupełnij poprawnie wszystkie pola pojazdu.", "error")
            return redirect(url_for("flota"))

        existing = db.execute(
            "SELECT id FROM vehicles WHERE registration = ?", (registration,)
        ).fetchone()
        if existing:
            flash("Pojazd o tej rejestracji już istnieje.", "error")
            return redirect(url_for("flota"))

        db.execute(
            "INSERT INTO vehicles (brand_model, registration, status) VALUES (?, ?, ?)",
            (brand_model, registration, status),
        )
        add_log(
            db,
            f"Użytkownik {g.user['username']} dodał pojazd {brand_model} ({registration}).",
        )
        db.commit()
        flash("Pojazd został dodany do floty.", "success")
        return redirect(url_for("flota"))

    status_filter = request.args.get("status", "wszystkie")
    if status_filter in ("Wolny", "W trasie"):
        vehicles = db.execute(
            """
            SELECT id, brand_model, registration, status
            FROM vehicles
            WHERE status = ?
            ORDER BY brand_model
            """,
            (status_filter,),
        ).fetchall()
    else:
        status_filter = "wszystkie"
        vehicles = db.execute(
            "SELECT id, brand_model, registration, status FROM vehicles ORDER BY brand_model"
        ).fetchall()
    return render_template("flota.html", vehicles=vehicles, status_filter=status_filter)


@app.route("/zlecenia", methods=["GET", "POST"])
@login_required("menedzer")
def zlecenia():
    db = get_db()
    if request.method == "POST":
        product_id = request.form.get("product_id", "").strip()
        vehicle_id = request.form.get("vehicle_id", "").strip()
        driver_id_raw = request.form.get("driver_id", "").strip()
        quantity_raw = request.form.get("quantity", "").strip()
        distance_raw = request.form.get("distance_km", "").strip()
        destination_name = request.form.get("destination", "Warszawa").strip()

        if not product_id or not vehicle_id or not quantity_raw or not distance_raw:
            flash("Uzupełnij produkt, ilość, pojazd i odległość.", "error")
            return redirect(url_for("zlecenia"))

        try:
            quantity = int(quantity_raw)
            distance_km = float(distance_raw.replace(",", "."))
            product_id_int = int(product_id)
            vehicle_id_int = int(vehicle_id)
            if quantity <= 0 or distance_km <= 0:
                raise ValueError
            driver_id = int(driver_id_raw) if driver_id_raw else None
        except ValueError:
            flash("Nieprawidłowe dane zlecenia.", "error")
            return redirect(url_for("zlecenia"))

        product = db.execute(
            "SELECT id, name, quantity FROM products WHERE id = ?",
            (product_id_int,),
        ).fetchone()
        vehicle = db.execute(
            "SELECT id, brand_model, registration FROM vehicles WHERE id = ?",
            (vehicle_id_int,),
        ).fetchone()

        if product is None or vehicle is None:
            flash("Wybrany produkt lub pojazd nie istnieje.", "error")
            return redirect(url_for("zlecenia"))

        if product["quantity"] < quantity:
            flash(
                (
                    f"Nie można utworzyć zlecenia — brak wystarczającej ilości towaru "
                    f"„{product['name']}” w magazynie. Dostępne: {product['quantity']}, "
                    f"żądane: {quantity}."
                ),
                "error",
            )
            return redirect(url_for("zlecenia"))

        if driver_id is None:
            assigned = db.execute(
                "SELECT id FROM users WHERE role = 'kierowca' AND vehicle_id = ?",
                (vehicle_id_int,),
            ).fetchone()
            driver_id = assigned["id"] if assigned else None
        else:
            driver = db.execute(
                "SELECT id, role FROM users WHERE id = ?", (driver_id,)
            ).fetchone()
            if driver is None or driver["role"] != "kierowca":
                flash("Wybrany kierowca jest nieprawidłowy.", "error")
                return redirect(url_for("zlecenia"))

        dest = destination_by_name(destination_name)
        fuel_cost, eta_minutes = calculate_transport(distance_km)
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        cursor = db.execute(
            """
            INSERT INTO orders (
                product_id, vehicle_id, quantity, created_at, status, driver_id,
                distance_km, fuel_cost, eta_minutes, destination, dest_lat, dest_lng
            )
            VALUES (?, ?, ?, ?, 'W realizacji', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                product_id_int,
                vehicle_id_int,
                quantity,
                created_at,
                driver_id,
                distance_km,
                fuel_cost,
                eta_minutes,
                dest["name"],
                dest["lat"],
                dest["lng"],
            ),
        )
        db.execute(
            "UPDATE products SET quantity = quantity - ? WHERE id = ?",
            (quantity, product_id_int),
        )
        db.execute(
            "UPDATE vehicles SET status = 'W trasie' WHERE id = ?",
            (vehicle_id_int,),
        )
        add_log(
            db,
            (
                f"Użytkownik {g.user['username']} utworzył zlecenie nr {cursor.lastrowid} "
                f"({product['name']}, {quantity} szt., {vehicle['brand_model']})."
            ),
        )
        db.commit()
        flash(
            (
                f"Zlecenie #{cursor.lastrowid} utworzone. Koszt paliwa: {fuel_cost:.2f} zł, "
                f"ETA: {format_eta(eta_minutes)}. Magazyn i status pojazdu zaktualizowano."
            ),
            "success",
        )
        return redirect(url_for("zlecenia"))

    products = db.execute(
        "SELECT id, name, quantity FROM products ORDER BY name"
    ).fetchall()
    vehicles = db.execute(
        "SELECT id, brand_model, registration, status FROM vehicles ORDER BY brand_model"
    ).fetchall()
    drivers = db.execute(
        """
        SELECT u.id, u.username, v.brand_model, v.registration
        FROM users u
        LEFT JOIN vehicles v ON v.id = u.vehicle_id
        WHERE u.role = 'kierowca'
        ORDER BY u.username
        """
    ).fetchall()
    orders = db.execute(
        """
        SELECT
            o.id,
            o.quantity,
            o.created_at,
            o.status,
            o.distance_km,
            o.fuel_cost,
            o.eta_minutes,
            o.destination,
            p.name AS product_name,
            v.brand_model AS vehicle_name,
            v.registration,
            u.username AS driver_name
        FROM orders o
        JOIN products p ON p.id = o.product_id
        JOIN vehicles v ON v.id = o.vehicle_id
        LEFT JOIN users u ON u.id = o.driver_id
        ORDER BY o.id DESC
        """
    ).fetchall()
    return render_template(
        "zlecenia.html",
        products=products,
        vehicles=vehicles,
        drivers=drivers,
        orders=orders,
        destinations=DESTINATIONS,
        fuel_l=FUEL_L_PER_100KM,
        fuel_price=FUEL_PRICE_PLN,
        avg_speed=AVG_SPEED_KMH,
    )


@app.route("/zlecenie/<int:order_id>")
@login_required("menedzer", "kierowca")
def zlecenie_szczegoly(order_id):
    order = fetch_order_details(get_db(), order_id)
    if not driver_can_see_order(g.user, order):
        flash("Nie znaleziono zlecenia albo nie masz do niego dostępu.", "error")
        return redirect(url_for("home"))
    return render_template("zlecenie.html", order=order)


@app.route("/zlecenia/<int:order_id>/pdf")
@login_required("menedzer", "kierowca")
def zlecenie_pdf(order_id):
    order = fetch_order_details(get_db(), order_id)
    if not driver_can_see_order(g.user, order):
        flash("Nie znaleziono zlecenia do wygenerowania listu przewozowego.", "error")
        return redirect(url_for("home"))

    pdf_file = build_waybill_pdf(order)
    filename = f"list_przewozowy_zlecenie_{order_id}.pdf"
    return send_file(
        pdf_file,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/kierowca")
@login_required("kierowca")
def kierowca():
    db = get_db()
    orders = db.execute(
        """
        SELECT
            o.id,
            o.quantity,
            o.created_at,
            o.status,
            o.distance_km,
            o.fuel_cost,
            o.eta_minutes,
            o.destination,
            o.dest_lat,
            o.dest_lng,
            p.name AS product_name,
            v.brand_model AS vehicle_name,
            v.registration
        FROM orders o
        JOIN products p ON p.id = o.product_id
        JOIN vehicles v ON v.id = o.vehicle_id
        WHERE o.driver_id = ? OR o.vehicle_id = ?
        ORDER BY
            CASE o.status WHEN 'W realizacji' THEN 0 ELSE 1 END,
            o.id DESC
        """,
        (g.user["id"], g.user["vehicle_id"] if g.user["vehicle_id"] is not None else -1),
    ).fetchall()
    return render_template("kierowca.html", orders=orders)


@app.route("/zlecenie/<int:order_id>/dostarczone", methods=["POST"])
@login_required("kierowca")
def oznacz_dostarczone(order_id):
    db = get_db()
    order = fetch_order_details(db, order_id)
    if not driver_can_see_order(g.user, order):
        flash("Nie możesz oznaczyć tego zlecenia.", "error")
        return redirect(url_for("kierowca"))
    if order["status"] == "Dostarczone":
        flash("To zlecenie jest już oznaczone jako dostarczone.", "error")
        return redirect(url_for("zlecenie_szczegoly", order_id=order_id))

    db.execute("UPDATE orders SET status = 'Dostarczone' WHERE id = ?", (order_id,))
    db.execute("UPDATE vehicles SET status = 'Wolny' WHERE id = ?", (order["vehicle_id"],))
    add_log(
        db,
        f"Kierowca {g.user['username']} dostarczył zlecenie nr {order_id}.",
    )
    db.commit()
    flash("Zlecenie oznaczone jako dostarczone. Pojazd wrócił do statusu Wolny.", "success")
    return redirect(url_for("kierowca"))


@app.route("/skaner")
@login_required("menedzer", "kierowca")
def skaner():
    return render_template("skaner.html")


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
else:
    init_db()
