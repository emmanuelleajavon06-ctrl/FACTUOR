"""FACTUOR - Ta facture pro en 30 secondes. Avec QR et WhatsApp.
Backend Flask : /generate-pdf, /save-invoice, /verify/<id>
"""
import base64
import hashlib
import io
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from html import escape

import qrcode
from flask import Flask, abort, jsonify, request, send_file, send_from_directory
from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "factuor.db"))
ID_RE = re.compile(r"^FCT-[A-Z0-9]{6}$")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024  # 3 Mo max (logo inclus)


# ---------- Base de données ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    global DB_PATH
    try:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        sqlite3.connect(DB_PATH).close()
    except (OSError, sqlite3.Error):
        DB_PATH = os.path.join("/tmp", "factuor.db")  # repli si le dossier n'est pas accessible
    with db() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS invoices (
                id TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
                data TEXT NOT NULL, total REAL NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
        )


init_db()


# ---------- Utilitaires ----------
def fcfa(n):
    return f"{int(round(n)):,}".replace(",", " ")


CUR = {"XOF": ("FCFA", 0), "EUR": ("€", 2), "USD": ("$", 2), "MAD": ("MAD", 2), "GNF": ("GNF", 0)}
STATUSES = ("PAYÉ", "À CRÉDIT", "ACOMPTE", "EN ATTENTE", "ANNULÉE")
DOCTYPES = ("FACTURE", "DEVIS", "REÇU", "PROFORMA")


def nf(n, cur="XOF"):
    s = f"{n:,.{CUR[cur][1]}f}".replace(",", " ")
    return s.replace(".", ",")


def money(n, cur="XOF"):
    return f"{nf(n, cur)} {CUR[cur][0]}"


def num(v, default=0.0):
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return default


def clean(d):
    """Normalise et borne les données reçues."""
    s = lambda v, n=200: str(v or "").strip()[:n]
    items = []
    for it in (d.get("items") or [])[:60]:
        desc = s(it.get("d"), 300)
        q, p = num(it.get("q"), 1), num(it.get("p"))
        if desc or p:
            items.append({"d": desc, "q": q, "p": p})
    co, cl = d.get("company") or {}, d.get("client") or {}
    logo = d.get("logo") or ""
    if not (isinstance(logo, str) and logo.startswith("data:image/") and len(logo) < 1_500_000):
        logo = ""
    cur = s(d.get("currency"), 3)
    cur = cur if cur in CUR else "XOF"
    dtype = "fixe" if d.get("discount_type") == "fixe" else "%"
    sub = sum(i["q"] * i["p"] for i in items)
    tva = min(num(d.get("tva")), 100)
    remise = num(d.get("remise"))
    remise = min(remise, sub) if dtype == "fixe" else min(remise, 100)
    disc = remise if dtype == "fixe" else sub * remise / 100
    tva_amt = (sub - disc) * tva / 100
    total = sub - disc + tva_amt
    status = s(d.get("status"), 12)
    status = status if status in STATUSES else ""
    paid = total if status == "PAYÉ" else min(num(d.get("deposit")), total)
    sig = d.get("signature") or ""
    if not (isinstance(sig, str) and sig.startswith("data:image/") and len(sig) < 400_000):
        sig = ""
    doctype = s(d.get("doctype"), 10)
    doctype = doctype if doctype in DOCTYPES else "FACTURE"
    return {
        "id": s(d.get("id"), 20),
        "company": {k: s(co.get(k)) for k in ("name", "address", "phone", "ninea")},
        "client": {k: s(cl.get(k)) for k in ("name", "phone", "address")},
        "date": s(d.get("date"), 20),
        "due": s(d.get("due"), 20),
        "items": items,
        "tva": tva,
        "remise": remise,
        "notes": s(d.get("notes"), 1000),
        "logo": logo,
        "sub": sub,
        "disc": disc,
        "tva_amt": tva_amt,
        "total": total,
        "currency": cur,
        "discount_type": dtype,
        "status": status,
        "paid": paid,
        "reste": total - paid,
        "doctype": doctype,
        "pay_method": s(d.get("pay_method"), 40),
        "pay_info": s(d.get("pay_info"), 120),
        "sign_name": s(d.get("sign_name"), 60),
        "signature": sig,
    }


def store(raw):
    """Enregistre (ou met à jour) une facture. Retourne les données normalisées."""
    inv = clean(raw)
    token = str(raw.get("token") or "")
    if not ID_RE.match(inv["id"]) or len(token) < 16:
        abort(400, "Identifiant de facture invalide")
    th = hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db() as con:
        row = con.execute("SELECT token_hash FROM invoices WHERE id=?", (inv["id"],)).fetchone()
        if row and row["token_hash"] != th:
            abort(403, "Cette facture existe déjà")
        payload = json.dumps({k: v for k, v in inv.items() if k not in ("logo", "signature")})
        if row:
            con.execute("UPDATE invoices SET data=?,total=?,updated_at=? WHERE id=?",
                        (payload, inv["total"], now, inv["id"]))
        else:
            con.execute("INSERT INTO invoices VALUES (?,?,?,?,?,?)",
                        (inv["id"], th, payload, inv["total"], now, now))
    return inv


# ---------- PDF ----------
def build_pdf(inv, verify_url):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    DARK, GOLD = HexColor("#0A0A0B"), HexColor("#FFD700")
    GREY, LINE, SOFT = HexColor("#6B7280"), HexColor("#E5E7EB"), HexColor("#F4F4F5")
    m = 18 * mm
    co, cl = inv["company"], inv["client"]
    cur = inv["currency"]
    M = lambda v: money(v, cur)

    # Bandeau
    c.setFillColor(DARK)
    c.rect(0, H - 40 * mm, W, 40 * mm, fill=1, stroke=0)
    c.setFillColor(GOLD)
    c.rect(0, H - 41.2 * mm, W, 1.2 * mm, fill=1, stroke=0)
    drawn = False
    if inv["logo"]:
        try:
            raw = base64.b64decode(inv["logo"].split(",", 1)[1])
            c.drawImage(ImageReader(io.BytesIO(raw)), m, H - 33 * mm, 42 * mm, 26 * mm,
                        preserveAspectRatio=True, anchor="w", mask="auto")
            drawn = True
        except Exception:
            drawn = False
    if not drawn:
        c.setFillColor(GOLD)
        c.setFont("Helvetica-Bold", 20)
        c.drawString(m, H - 22 * mm, (co["name"] or "FACTUOR").upper()[:28])
    c.setFillColor(GOLD)
    c.setFont("Helvetica-Bold", 22)
    c.drawRightString(W - m, H - 18 * mm, inv["doctype"])
    c.setFillColor(white)
    c.setFont("Helvetica", 9.5)
    c.drawRightString(W - m, H - 25 * mm, f"N° {inv['id']}")
    c.drawRightString(W - m, H - 30.5 * mm, f"Date : {inv['date']}")
    if inv["due"]:
        c.drawRightString(W - m, H - 36 * mm, f"Échéance : {inv['due']}")

    # Émetteur / client
    y = H - 54 * mm

    def block(x, title, lines):
        c.setFillColor(GREY)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(x, y, title)
        c.setFillColor(DARK)
        yy = y - 5.5 * mm
        for i, ln in enumerate([l for l in lines if l]):
            c.setFont("Helvetica-Bold" if i == 0 else "Helvetica", 10 if i == 0 else 9)
            c.drawString(x, yy, ln[:48])
            yy -= 4.6 * mm

    block(m, "ÉMETTEUR", [co["name"], co["address"], co["phone"],
                          f"NINEA : {co['ninea']}" if co["ninea"] else ""])
    block(W / 2 + 6 * mm, "FACTURÉ À", [cl["name"], cl["address"], cl["phone"]])
    y -= 32 * mm

    # Tableau
    cols = {"d": m + 3 * mm, "q": W - m - 78 * mm, "p": W - m - 38 * mm, "t": W - m - 3 * mm}

    def table_head():
        nonlocal y
        c.setFillColor(DARK)
        c.rect(m, y - 2 * mm, W - 2 * m, 8 * mm, fill=1, stroke=0)
        c.setFillColor(GOLD)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(cols["d"], y + 0.8 * mm, "DESCRIPTION")
        c.drawRightString(cols["q"], y + 0.8 * mm, "QTÉ")
        c.drawRightString(cols["p"], y + 0.8 * mm, "PRIX UNIT.")
        c.drawRightString(cols["t"], y + 0.8 * mm, f"TOTAL ({CUR[cur][0]})")
        y -= 8 * mm

    table_head()
    for i, it in enumerate(inv["items"]):
        lines = simpleSplit(it["d"] or "-", "Helvetica", 9, cols["q"] - cols["d"] - 22 * mm)
        h = max(8 * mm, len(lines) * 4.4 * mm + 3.5 * mm)
        if y - h < 40 * mm:
            c.showPage()
            y = H - 25 * mm
            table_head()
        if i % 2 == 0:
            c.setFillColor(SOFT)
            c.rect(m, y - h + 5.2 * mm, W - 2 * m, h, fill=1, stroke=0)
        c.setFillColor(DARK)
        c.setFont("Helvetica", 9)
        for k, ln in enumerate(lines):
            c.drawString(cols["d"], y - k * 4.4 * mm, ln)
        qty = f"{it['q']:g}"
        c.drawRightString(cols["q"], y, qty)
        c.drawRightString(cols["p"], y, nf(it["p"], cur))
        c.setFont("Helvetica-Bold", 9)
        c.drawRightString(cols["t"], y, nf(it["q"] * it["p"], cur))
        y -= h

    # Totaux
    if y < 125 * mm:
        c.showPage()
        y = H - 25 * mm
    y -= 4 * mm
    y_tot = y
    c.setStrokeColor(LINE)
    c.line(m, y + 3 * mm, W - m, y + 3 * mm)
    tx = W - m - 70 * mm

    def row(label, val):
        nonlocal y
        c.setFillColor(GREY)
        c.setFont("Helvetica", 9.5)
        c.drawString(tx, y - 3 * mm, label)
        c.setFillColor(DARK)
        c.drawRightString(W - m - 3 * mm, y - 3 * mm, val)
        y -= 6 * mm

    row("Sous-total", M(inv["sub"]))
    if inv["remise"]:
        row("Remise" if inv["discount_type"] == "fixe" else f"Remise ({inv['remise']:g}%)", "- " + M(inv["disc"]))
    if inv["tva"]:
        row(f"TVA ({inv['tva']:g}%)", M(inv["tva_amt"]))
    y -= 2 * mm
    c.setFillColor(DARK)
    c.roundRect(tx - 3 * mm, y - 10 * mm, 73 * mm, 12 * mm, 2 * mm, fill=1, stroke=0)
    c.setFillColor(GOLD)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(tx, y - 5.5 * mm, "TOTAL À PAYER")
    c.setFont("Helvetica-Bold", 12.5)
    c.drawRightString(W - m - 3 * mm, y - 6 * mm, M(inv["total"]))
    y -= 14 * mm
    if inv["paid"] > 0:
        row("Payé" if inv["status"] == "PAYÉ" else "Acompte versé", "- " + M(inv["paid"]))
        row("Reste à payer", M(inv["reste"]))
    y -= 4 * mm

    # Cachet de statut
    if inv["status"]:
        col = HexColor({"PAYÉ": "#00A862", "À CRÉDIT": "#E11D48", "ACOMPTE": "#D97706",
                        "EN ATTENTE": "#6B7280", "ANNULÉE": "#7F1D1D"}[inv["status"]])
        c.saveState()
        c.translate(m + 44 * mm, y_tot - 16 * mm)
        c.rotate(14)
        c.setStrokeColor(col)
        c.setFillColor(col)
        c.setLineWidth(2.4)
        c.roundRect(-34 * mm, -8 * mm, 68 * mm, 16 * mm, 3 * mm, fill=0, stroke=1)
        c.setFont("Helvetica-Bold", 20)
        c.drawCentredString(0, -2.6 * mm, inv["status"])
        c.restoreState()

    # Paiement
    if inv["pay_method"]:
        c.setFillColor(GREY)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(m, y, "PAIEMENT")
        c.setFillColor(DARK)
        c.setFont("Helvetica", 9)
        c.drawString(m, y - 5 * mm, (inv["pay_method"] + (" : " + inv["pay_info"] if inv["pay_info"] else ""))[:80])
        y -= 13 * mm

    # Notes
    if inv["notes"]:
        c.setFillColor(GREY)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(m, y, "NOTES")
        c.setFillColor(DARK)
        c.setFont("Helvetica", 9)
        yy = y - 5 * mm
        for ln in simpleSplit(inv["notes"], "Helvetica", 9, 100 * mm)[:6]:
            c.drawString(m, yy, ln)
            yy -= 4.4 * mm

    # QR + pied de page
    qr = qrcode.make(verify_url, box_size=8, border=1)
    qb = io.BytesIO()
    qr.save(qb, format="PNG")
    qb.seek(0)
    c.drawImage(ImageReader(qb), m, 14 * mm, 26 * mm, 26 * mm)
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(m + 30 * mm, 33 * mm, "Facture vérifiable")
    c.setFillColor(GREY)
    c.setFont("Helvetica", 8.5)
    c.drawString(m + 30 * mm, 28 * mm, verify_url.replace("https://", "").replace("http://", ""))
    c.drawString(m + 30 * mm, 23.5 * mm, "Scannez le QR code pour authentifier ce document.")
    sx = W - m - 52 * mm
    if inv["signature"]:
        try:
            c.drawImage(ImageReader(io.BytesIO(base64.b64decode(inv["signature"].split(",", 1)[1]))),
                        sx, 22 * mm, 48 * mm, 18 * mm, preserveAspectRatio=True, anchor="c", mask="auto")
        except Exception:
            pass
    c.setStrokeColor(DARK)
    c.setLineWidth(0.5)
    c.line(sx, 21.5 * mm, W - m, 21.5 * mm)
    c.setFillColor(GREY)
    c.setFont("Helvetica", 7.5)
    c.drawString(sx, 17.5 * mm, ("Signature - " + inv["sign_name"]) if inv["sign_name"] else "Signature")
    c.setFillColor(GOLD)
    c.rect(0, 0, W, 6 * mm, fill=1, stroke=0)
    c.setFillColor(DARK)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawCentredString(W / 2, 2.2 * mm, "Généré avec FACTUOR - Ta facture pro en 30 secondes")
    c.save()
    buf.seek(0)
    return buf


def base_url(raw):
    b = str(raw.get("base_url") or request.host_url).rstrip("/")
    return b if re.match(r"^https?://[\w.\-:]+$", b) else request.host_url.rstrip("/")


# ---------- Routes ----------
@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/health")
def health():
    return jsonify(ok=True)


@app.post("/save-invoice")
def save_invoice():
    raw = request.get_json(force=True, silent=True) or {}
    inv = store(raw)
    return jsonify(ok=True, id=inv["id"], total=inv["total"])


@app.post("/generate-pdf")
def generate_pdf():
    raw = request.get_json(force=True, silent=True) or {}
    inv = store(raw)
    if not inv["items"]:
        abort(400, "Ajoutez au moins un article")
    url = f"{base_url(raw)}/verify/{inv['id']}"
    pdf = build_pdf(inv, url)
    return send_file(pdf, mimetype="application/pdf", as_attachment=True,
                     download_name=f"Facture-{inv['id']}.pdf")


PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@500;700&family=Space+Grotesk:wght@700&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;background:#0A0A0B;color:#FAFAFA;font-family:Inter,system-ui,sans-serif;display:grid;place-items:center;padding:24px}}
.card{{width:100%;max-width:460px;background:#18181B;border:1px solid #27272A;border-radius:20px;padding:32px;animation:up .8s cubic-bezier(.16,1,.3,1)}}
@keyframes up{{from{{opacity:0;transform:translateY(24px)}}}}
.logo{{font-family:'Space Grotesk';font-weight:700;color:#FFD700;letter-spacing:.04em;font-size:18px}}
.badge{{display:inline-flex;gap:8px;align-items:center;margin:20px 0 6px;padding:6px 12px;border-radius:99px;font-weight:600;font-size:14px}}
.ok{{background:rgba(0,255,148,.1);color:#00FF94}}.ko{{background:rgba(255,80,80,.1);color:#ff6b6b}}
.row{{display:flex;justify-content:space-between;gap:16px;padding:12px 0;border-bottom:1px solid #27272A;font-size:14px}}
.row span:first-child{{color:#A1A1AA}}.row span:last-child{{text-align:right;font-weight:500}}
.total{{font-family:'JetBrains Mono';font-weight:700;color:#FFD700;font-size:22px}}
small{{display:block;margin-top:20px;color:#71717A;font-size:12px;text-align:center}}
</style></head><body><div class="card"><div class="logo">FACTUOR</div>{body}
<small>Vérification FACTUOR - factuor.sn</small></div></body></html>"""


@app.get("/verify/<inv_id>")
def verify(inv_id):
    row = db().execute("SELECT * FROM invoices WHERE id=?", (inv_id.upper(),)).fetchone() \
        if ID_RE.match(inv_id.upper()) else None
    if not row:
        return PAGE.format(title="Facture introuvable", body=(
            '<div class="badge ko">✕ Facture introuvable</div>'
            "<p style='color:#A1A1AA;font-size:14px'>Aucune facture FACTUOR ne correspond à ce code. "
            "Vérifiez le lien ou demandez un nouveau QR à l'émetteur.</p>")), 404
    d = json.loads(row["data"])
    e = lambda v: escape(str(v or "-"))
    body = (
        f'<div class="badge ok">✓ {e(d.get("doctype", "FACTURE")).capitalize()} authentique</div>'
        f'<div class="row"><span>N°</span><span>{e(d["id"])}</span></div>'
        f'<div class="row"><span>Émetteur</span><span>{e(d["company"]["name"])}</span></div>'
        f'<div class="row"><span>Client</span><span>{e(d["client"]["name"])}</span></div>'
        f'<div class="row"><span>Date</span><span>{e(d["date"])}</span></div>'
        f'<div class="row"><span>Statut</span><span>{e(d.get("status"))}</span></div>'
        f'<div class="row"><span>Articles</span><span>{len(d["items"])}</span></div>'
        f'<div class="row"><span>Montant</span><span class="total">{money(row["total"], d.get("currency", "XOF"))}</span></div>'
        f'<div class="row"><span>Reste à payer</span><span>{money(d.get("reste", row["total"]), d.get("currency", "XOF"))}</span></div>'
        f'<small style="margin-top:12px">Dernière mise à jour : {e(row["updated_at"][:10])}</small>'
    )
    return PAGE.format(title=f"Facture {d['id']}", body=body)


@app.errorhandler(400)
@app.errorhandler(403)
def err(e):
    return jsonify(error=e.description), e.code


@app.errorhandler(Exception)
def crash(e):
    if hasattr(e, "code") and e.code and e.code < 500:
        return e
    app.logger.exception(e)
    return jsonify(error="Erreur serveur, réessayez."), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
