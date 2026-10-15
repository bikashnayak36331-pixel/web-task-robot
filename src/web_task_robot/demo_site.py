"""A tiny practice website for trying the robot safely, with no internet needed.

It imitates a typical back-office portal:
  /login            sign-in form (demo@example.com / demo-pass, or WEBROBOT_DEMO_USER / _PASSWORD)
  /customers/new    "add customer" form with server-side validation
  /customers        the customers added so far
  /products?page=N  a paginated product catalogue (for extraction)

Set WEBROBOT_DEMO_FLAKY=0.3 to make 30% of form posts fail with a 503, to see retries working.
Everything is stored in memory and is fictional.
"""

from __future__ import annotations

import html
import os
import random
import re
import secrets
import threading
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PLANS = ("Starter", "Pro", "Business")
PER_PAGE = 8
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

CSS = """
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;
color:#1f2933;background:#f4f6f8}header{background:#1f2933;color:#fff;padding:12px 24px;display:flex;gap:20px;
align-items:center}header b{font-size:17px}header a{color:#cbd2d9;text-decoration:none}main{max-width:880px;
margin:28px auto;padding:0 16px}.card{background:#fff;border-radius:10px;padding:24px;
box-shadow:0 1px 3px rgba(0,0,0,.08)}h1{font-size:22px;margin:0 0 16px}label{display:block;font-weight:600;
margin:12px 0 4px}input[type=text],input[type=email],input[type=password],input[type=tel],select,textarea{width:100%;
padding:9px 10px;border:1px solid #cbd2d9;border-radius:6px;font:inherit}textarea{height:70px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}button{margin-top:18px;background:#0b6e4f;color:#fff;
border:0;border-radius:6px;padding:10px 18px;font:inherit;font-weight:600;cursor:pointer}
.alert-success{background:#d1f2e3;border:1px solid #0b6e4f;padding:12px;border-radius:6px;margin-bottom:16px}
.alert-error{background:#f8d7da;border:1px solid #9b2c2c;padding:12px;border-radius:6px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.product-card{background:#fff;border-radius:8px;
padding:12px;box-shadow:0 1px 3px rgba(0,0,0,.08)}.product-card .name{font-weight:600}.price{color:#0b6e4f;
font-weight:700}.muted{color:#616e7c;font-size:13px}.pager{margin-top:16px;display:flex;gap:12px}
table{width:100%;border-collapse:collapse;background:#fff}td,th{padding:8px;border-bottom:1px solid #e4e7eb;
text-align:left}.check{display:flex;gap:8px;align-items:center;margin-top:14px;font-weight:600}
"""


def _products() -> list[dict]:
    rng = random.Random(7)
    kinds = [
        "Desk Lamp",
        "Office Chair",
        "Notebook",
        "Water Bottle",
        "Backpack",
        "Monitor Stand",
        "Keyboard",
        "Mouse Pad",
        "Plant Pot",
        "Wall Clock",
    ]
    styles = ["Classic", "Eco", "Pro", "Mini", "Studio", "Travel"]
    items = []
    for i in range(1, 31):
        name = f"{styles[(i * 5) % len(styles)]} {kinds[i % len(kinds)]}"
        items.append(
            {
                "sku": f"SKU-{1000 + i}",
                "name": name,
                "price": round(rng.uniform(6, 180), 2),
                "stock": rng.choice([0, 3, 8, 15, 24, 40]),
                "rating": round(rng.uniform(3.2, 5.0), 1),
            }
        )
    return items


class DemoState:
    def __init__(self, flaky: float = 0.0, seed: int = 1):
        self.user = os.environ.get("WEBROBOT_DEMO_USER", "demo@example.com")
        self.password = os.environ.get("WEBROBOT_DEMO_PASSWORD", "demo-pass")
        self.sessions: set[str] = set()
        # One customer already exists, so the example job shows how a site-side error is reported.
        self.customers: list[dict] = [
            {
                "ref": "CUS-1001",
                "name": "Tom Becker",
                "company": "Elm & Oak Law",
                "email": "tom@elmandoak.example",
                "plan": "Pro",
                "newsletter": False,
                "phone": "",
                "notes": "",
            }
        ]
        self.products = _products()
        self.flaky = flaky
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.posts = 0


def _page(title: str, body: str, logged_in: bool = True) -> bytes:
    nav = (
        ('<a href="/customers/new">Add customer</a><a href="/customers">Customers</a><a href="/products">Products</a>')
        if logged_in
        else ""
    )
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)} · Acme Portal'
        f"</title><style>{CSS}</style></head><body><header><b>Acme Portal (demo)</b>{nav}</header>"
        f"<main>{body}</main></body></html>"
    ).encode()


def make_handler(state: DemoState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "WebRobotDemo/1.0"

        def log_message(self, *args):  # keep test output clean
            pass

        # ------------------------------------------------------------ helpers
        def _session(self) -> str | None:
            c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            sid = c.get("sid")
            return sid.value if sid and sid.value in state.sessions else None

        def _send(self, body: bytes, status: int = 200, headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, to: str, headers: dict | None = None):
            self.send_response(303)
            self.send_header("Location", to)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length") or 0)
            data = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
            return {k: v[0].strip() for k, v in data.items()}

        # ------------------------------------------------------------ pages
        def _login_page(self, error: str = "") -> bytes:
            err = f'<div class="alert-error">{html.escape(error)}</div>' if error else ""
            return _page(
                "Sign in",
                f"""<div class="card" style="max-width:420px;margin:auto"><h1>Sign in</h1>{err}
<form method="post" action="/login"><label for="email">Email</label><input id="email" name="email" type="email">
<label for="password">Password</label><input id="password" name="password" type="password">
<button type="submit">Sign in</button></form>
<p class="muted">Demo login: {html.escape(state.user)} / {html.escape(state.password)}</p></div>""",
                False,
            )

        def _customer_form(self, error: str = "", values: dict | None = None) -> bytes:
            v = {k: html.escape(x) for k, x in (values or {}).items()}
            err = f'<div class="alert-error" role="alert">{html.escape(error)}</div>' if error else ""
            opts = "".join(f"<option{' selected' if v.get('plan') == p else ''}>{p}</option>" for p in PLANS)
            return _page(
                "Add customer",
                f"""<div class="card"><h1>Add customer</h1>{err}
<form method="post" action="/customers/new" id="customer-form">
<div class="row"><div><label for="name">Full name</label><input id="name" name="name" type="text" value="{v.get("name", "")}"></div>
<div><label for="company">Company</label><input id="company" name="company" type="text" value="{v.get("company", "")}"></div></div>
<div class="row"><div><label for="email">Email</label><input id="email" name="email" type="email" value="{v.get("email", "")}"></div>
<div><label for="phone">Phone</label><input id="phone" name="phone" type="tel" value="{v.get("phone", "")}"></div></div>
<label for="plan">Plan</label><select id="plan" name="plan"><option value="">Choose…</option>{opts}</select>
<label class="check"><input id="newsletter" name="newsletter" type="checkbox" value="yes"> Send the monthly newsletter</label>
<label for="notes">Notes</label><textarea id="notes" name="notes">{v.get("notes", "")}</textarea>
<button type="submit" id="save">Save customer</button></form></div>""",
            )

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path in ("/", "/login"):
                return self._send(self._login_page()) if not self._session() else self._redirect("/customers/new")
            if not self._session():
                return self._redirect("/login")
            if url.path == "/customers/new":
                return self._send(self._customer_form())
            if url.path == "/customers":
                rows = "".join(
                    f"<tr><td>{c['ref']}</td><td>{html.escape(c['name'])}</td><td>{html.escape(c['company'])}</td>"
                    f"<td>{html.escape(c['email'])}</td><td>{c['plan']}</td><td>{'yes' if c['newsletter'] else ''}"
                    f"</td></tr>"
                    for c in state.customers
                )
                return self._send(
                    _page(
                        "Customers",
                        f"""<div class="card"><h1>Customers ({len(state.customers)})</h1>
<table id="customers"><tr><th>Ref</th><th>Name</th><th>Company</th><th>Email</th><th>Plan</th><th>Newsletter</th></tr>
{rows}</table></div>""",
                    )
                )
            if url.path == "/products":
                page = max(1, int((parse_qs(url.query).get("page") or ["1"])[0] or 1))
                pages = (len(state.products) + PER_PAGE - 1) // PER_PAGE
                chunk = state.products[(page - 1) * PER_PAGE : page * PER_PAGE]
                cards = "".join(
                    f'<div class="product-card" data-sku="{p["sku"]}"><a href="/products/{p["sku"]}" class="name">'
                    f'{html.escape(p["name"])}</a><div class="price">${p["price"]:.2f}</div>'
                    f'<div class="muted">Rating <span class="rating">{p["rating"]}</span> · '
                    f'<span class="stock">{"Out of stock" if p["stock"] == 0 else str(p["stock"]) + " in stock"}'
                    f"</span></div></div>"
                    for p in chunk
                )
                prev = f'<a class="prev" href="/products?page={page - 1}">← Previous</a>' if page > 1 else ""
                nxt = f'<a class="next" href="/products?page={page + 1}">Next →</a>' if page < pages else ""
                return self._send(
                    _page(
                        "Products",
                        f"""<h1>Products · page {page} of {pages}</h1>
<div class="grid">{cards}</div><div class="pager">{prev}{nxt}</div>""",
                    )
                )
            self._send(_page("Not found", "<h1>Page not found</h1>"), 404)

        def do_POST(self):  # noqa: N802
            url = urlparse(self.path)
            form = self._form()
            if url.path == "/login":
                if form.get("email") == state.user and form.get("password") == state.password:
                    sid = secrets.token_hex(16)
                    state.sessions.add(sid)
                    return self._redirect("/customers/new", {"Set-Cookie": f"sid={sid}; Path=/; HttpOnly"})
                return self._send(self._login_page("Wrong email or password."), 401)
            if not self._session():
                return self._redirect("/login")
            if url.path == "/customers/new":
                with state.lock:
                    state.posts += 1
                    if state.flaky and state.rng.random() < state.flaky:
                        return self._send(_page("Busy", "<h1>Service temporarily unavailable</h1>"), 503)
                    name, email, plan = form.get("name", ""), form.get("email", ""), form.get("plan", "")
                    error = ""
                    if not name:
                        error = "Full name is required."
                    elif not _EMAIL.match(email):
                        error = f"'{email}' is not a valid email address."
                    elif plan not in PLANS:
                        error = "Choose a plan."
                    elif any(c["email"].lower() == email.lower() for c in state.customers):
                        error = f"A customer with email {email} already exists."
                    if error:
                        return self._send(self._customer_form(error, form), 422)
                    ref = f"CUS-{1001 + len(state.customers)}"
                    state.customers.append(
                        {
                            "ref": ref,
                            "name": name,
                            "company": form.get("company", ""),
                            "email": email,
                            "plan": plan,
                            "newsletter": form.get("newsletter") == "yes",
                            "phone": form.get("phone", ""),
                            "notes": form.get("notes", ""),
                        }
                    )
                return self._send(
                    _page(
                        "Saved",
                        f"""<div class="card"><div class="alert-success" role="status">
Customer saved. Reference <b class="ref">{ref}</b></div><a href="/customers/new" id="add-another">Add another</a>
</div>""",
                    )
                )
            self._send(_page("Not found", "<h1>Page not found</h1>"), 404)

    return Handler


class DemoSite:
    """Run the demo site in a background thread: `with DemoSite() as site: print(site.url)`."""

    def __init__(self, port: int = 0, flaky: float | None = None, seed: int = 1):
        flaky = float(os.environ.get("WEBROBOT_DEMO_FLAKY", "0")) if flaky is None else flaky
        self.state = DemoState(flaky=flaky, seed=seed)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self.state))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> DemoSite:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def serve_forever(self) -> None:
        print(f"Demo site running at {self.url}  (login: {self.state.user} / {self.state.password})  Ctrl+C to stop")
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.httpd.server_close()
