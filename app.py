"""Butter & Batch. Python standard library only. Run: python app.py"""
from contextlib import closing
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
import argparse
import csv
import io
import json
import re
import sqlite3
import threading
import webbrowser

ROOT = Path(__file__).resolve().parent
START = '2026-11-23'
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
PRODUCTS = {'cupcakes': {'price': 40, 'minutes': 30}, 'cake': {'price': 120, 'minutes': 120}}


class InputError(Exception):
    pass


class MessageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.capture = False
        self.messages = []

    def handle_starttag(self, tag, attrs):
        if tag == 'p' and dict(attrs).get('class') == 'message':
            self.capture = True
            self.messages.append('')

    def handle_endtag(self, tag):
        if tag == 'p':
            self.capture = False

    def handle_data(self, text):
        if self.capture:
            self.messages[-1] += text


def parse_product(text):
    match = re.fullmatch(r'(\d+)\s+(dozen cupcakes|cakes?)', text.strip(), re.I)
    if not match or not 1 <= int(match[1]) <= 100:
        raise InputError('Use a whole number of cakes or dozen cupcakes, from 1 to 100.')
    return ('cupcakes' if 'cupcake' in match[2].lower() else 'cake', int(match[1]))


def valid_date(value):
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise InputError('Enter a valid date in YYYY-MM-DD format.')


def notice(received, pickup):
    return (valid_date(pickup) - valid_date(received)).days >= 2


def connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def initialize(path, source=ROOT):
    with closing(connect(path)) as conn, conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders (
          id INTEGER PRIMARY KEY, customer TEXT NOT NULL, product TEXT NOT NULL,
          quantity INTEGER NOT NULL CHECK(quantity>0), pickup TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'accepted', source TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS requests (
          id INTEGER PRIMARY KEY, customer TEXT NOT NULL, kind TEXT NOT NULL,
          product TEXT, quantity INTEGER, pickup TEXT NOT NULL, received TEXT NOT NULL,
          body TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          reason TEXT NOT NULL DEFAULT '', order_id INTEGER REFERENCES orders(id),
          fetched INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS helpers (day TEXT PRIMARY KEY, booked_on TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY, day TEXT NOT NULL, action TEXT NOT NULL,
          detail TEXT NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        ''')
        if conn.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():
            return
        with (source / 'existing_orders.csv').open(newline='', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                product, quantity = parse_product(row['item'])
                pickup = datetime.strptime(row['pickup_day'] + ' 2026', '%a %d %b %Y').date().isoformat()
                conn.execute('INSERT INTO orders VALUES (?,?,?,?,?,?,?)',
                             (int(row['order'].strip('#')), row['customer'], product, quantity, pickup, 'accepted', 'existing_orders.csv'))
        for index, day in enumerate(DAYS):
            filename = f'InstagramOrders_{day}.html'
            parser = MessageParser()
            parser.feed((source / filename).read_text(encoding='utf-8-sig'))
            received = (date.fromisoformat(START) + timedelta(days=index)).isoformat()
            for body in parser.messages:
                fields = dict(line.split(':', 1) for line in body.splitlines() if ':' in line)
                customer, item = fields['Name'].strip(), fields['Order'].strip()
                pickup = datetime.strptime(fields['Pickup Date'].strip() + ' 2026', '%a %b %d %Y').date().isoformat()
                kind = 'cancel' if item.lower() == 'cancel' else 'change' if item.lower().startswith('change to ') else 'new'
                product, quantity = (None, None) if kind == 'cancel' else parse_product(re.sub(r'^Change to\s+', '', item, flags=re.I))
                conn.execute('INSERT INTO requests(customer,kind,product,quantity,pickup,received,body,source) VALUES (?,?,?,?,?,?,?,?)',
                             (customer, kind, product, quantity, pickup, received, body, filename))
        conn.execute('INSERT INTO meta VALUES (?,?)', ('current_day', START))
        conn.execute('INSERT INTO meta VALUES (?,?)', ('seeded', '1'))
        conn.execute('INSERT INTO audit(day,action,detail) VALUES (?,?,?)', (START, 'Imported source data', '13 existing orders and 25 Instagram messages. No new request accepted automatically.'))


def current_day(conn):
    return conn.execute("SELECT value FROM meta WHERE key='current_day'").fetchone()[0]


def used_minutes(conn, pickup, excluding=None):
    return conn.execute("SELECT COALESCE(SUM(quantity * CASE product WHEN 'cake' THEN 120 ELSE 30 END),0) FROM orders WHERE pickup=? AND status IN ('accepted','collected') AND id != ?", (pickup, excluding or -1)).fetchone()[0]


def capacity(conn, pickup):
    return 720 if conn.execute('SELECT 1 FROM helpers WHERE day=?', (pickup,)).fetchone() else 480


def assess(conn, request):
    today = current_day(conn)
    if request['received'] > today:
        return False, 'This message has not arrived yet.', None
    if not notice(request['received'], request['pickup']):
        return False, 'Less than 2 days’ notice from the customer.', None
    if not notice(today, request['pickup']):
        return False, 'The decision deadline has passed: 2 days’ notice is required.', None
    target = None
    if request['kind'] != 'new':
        matches = conn.execute("SELECT * FROM orders WHERE lower(customer)=lower(?) AND pickup=? AND status='accepted'", (request['customer'], request['pickup'])).fetchall()
        if len(matches) != 1:
            return False, 'Needs one matching accepted order. Review the earlier request first.', None
        target = matches[0]['id']
    if request['kind'] == 'cancel':
        return True, 'Cancellation is within the notice period.', target
    minutes = request['quantity'] * PRODUCTS[request['product']]['minutes']
    used = used_minutes(conn, request['pickup'], target)
    limit = capacity(conn, request['pickup'])
    if used + minutes > limit:
        extra = (used + minutes - limit) / 60
        helper_possible = limit == 480 and used + minutes <= 720 and notice(today, request['pickup'])
        return False, f'Over capacity by {extra:g} h.' + (' Book a helper first to add 4 h.' if helper_possible else ' Choose another order or pickup date.'), target
    return True, f'Within notice and capacity. {(limit-used-minutes)/60:g} h left after acceptance.', target


def audit(conn, action, detail):
    conn.execute('INSERT INTO audit(day,action,detail) VALUES (?,?,?)', (current_day(conn), action, detail))


def base_mutate(conn, action, payload):
    today = current_day(conn)
    if action == 'decision':
        request = conn.execute('SELECT * FROM requests WHERE id=?', (payload.get('id'),)).fetchone()
        if not request or request['status'] != 'pending' or request['received'] > today:
            raise InputError('This request is not available for a decision.')
        decision = payload.get('decision')
        if decision not in ('accepted', 'rejected'):
            raise InputError('Choose accept or reject.')
        allowed, reason, target = assess(conn, request)
        if decision == 'accepted':
            if not allowed:
                raise InputError(reason)
            if request['kind'] == 'new':
                cursor = conn.execute('INSERT INTO orders(customer,product,quantity,pickup,source) VALUES (?,?,?,?,?)',
                                      (request['customer'], request['product'], request['quantity'], request['pickup'], request['source']))
                target = cursor.lastrowid
            elif request['kind'] == 'change':
                conn.execute('UPDATE orders SET product=?,quantity=? WHERE id=?', (request['product'], request['quantity'], target))
            else:
                conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (target,))
        else:
            reason = str(payload.get('reason', '')).strip()[:500] or ('Baker declined.' if allowed else reason)
        conn.execute('UPDATE requests SET status=?,reason=?,order_id=? WHERE id=?', (decision, reason, target, request['id']))
        audit(conn, f"{request['kind'].title()} request {decision}", f"{request['customer']} · {request['pickup']} · {reason}")
    elif action == 'helper':
        pickup = payload.get('day')
        valid_date(pickup)
        if not notice(today, pickup):
            raise InputError('Helper bookings and cancellations need 2 days’ notice.')
        existing = conn.execute('SELECT 1 FROM helpers WHERE day=?', (pickup,)).fetchone()
        if payload.get('book') is True:
            if existing:
                raise InputError('A helper is already booked for this day.')
            conn.execute('INSERT INTO helpers VALUES (?,?)', (pickup, today))
            audit(conn, 'Helper booked', f'{pickup} · $100 · adds 4 oven hours')
        elif payload.get('book') is False:
            if not existing:
                raise InputError('No helper is booked for this day.')
            if used_minutes(conn, pickup) > 480:
                raise InputError('Accepted orders need this helper. Capacity would fall below demand.')
            conn.execute('DELETE FROM helpers WHERE day=?', (pickup,))
            audit(conn, 'Helper cancelled', pickup)
        else:
            raise InputError('Choose whether to book the helper.')
    elif action == 'advance':
        next_day = (valid_date(today) + timedelta(days=1)).isoformat()
        conn.execute("UPDATE meta SET value=? WHERE key='current_day'", (next_day,))
        audit(conn, 'Planning day advanced', next_day)
    elif action == 'request':
        customer = str(payload.get('customer', '')).strip()
        if not customer or len(customer) > 80:
            raise InputError('Enter a customer name, up to 80 characters.')
        kind = payload.get('kind')
        if kind not in ('new', 'change', 'cancel'):
            raise InputError('Choose a request type.')
        pickup = payload.get('pickup')
        if valid_date(pickup) < valid_date(today):
            raise InputError('Pickup cannot be in the past.')
        product, quantity = None, None
        if kind != 'cancel':
            product = payload.get('product')
            quantity = payload.get('quantity')
            if product not in PRODUCTS or type(quantity) is not int or not 1 <= quantity <= 100:
                raise InputError('Choose one product and a whole quantity from 1 to 100.')
        body = str(payload.get('body', '')).strip()[:4000] or f'{kind.title()} request recorded by baker'
        conn.execute('INSERT INTO requests(customer,kind,product,quantity,pickup,received,body,source) VALUES (?,?,?,?,?,?,?,?)',
                     (customer, kind, product, quantity, pickup, today, body, 'Manual entry'))
        audit(conn, 'Request recorded', f'{customer} · {kind} · {pickup}')
    elif action == 'collect':
        order = conn.execute('SELECT * FROM orders WHERE id=?', (payload.get('id'),)).fetchone()
        if not order or order['status'] != 'accepted' or order['pickup'] > today:
            raise InputError('Only an accepted order whose pickup day has arrived can be collected.')
        conn.execute("UPDATE orders SET status='collected' WHERE id=?", (order['id'],))
        audit(conn, 'Order collected', f"#{order['id']} · {order['customer']}")
    else:
        raise InputError('Unknown action.')


def apply_fetched_updates(conn):
    """Lecture state transitions: valid fetched changes/cancellations update orders."""
    for request in conn.execute("SELECT * FROM requests WHERE fetched=1 AND status='pending' AND kind!='new' ORDER BY received,id").fetchall():
        allowed, _, _ = assess(conn, request)
        if allowed:
            base_mutate(conn, 'decision', {'id': request['id'], 'decision': 'accepted'})


def batch_preview(conn):
    """Project sequential new-order decisions without changing the database."""
    ledger, accepted, skipped = {}, [], []
    for request in conn.execute("SELECT * FROM requests WHERE fetched=1 AND status='pending' AND kind='new' ORDER BY received,id"):
        allowed, reason, _ = assess(conn, request)
        pickup = request['pickup']
        used = ledger.get(pickup, used_minutes(conn, pickup))
        minutes = request['quantity'] * PRODUCTS[request['product']]['minutes']
        if allowed and used + minutes > capacity(conn, pickup):
            allowed, reason = False, 'Not enough capacity after earlier requests in this batch.'
        if allowed:
            ledger[pickup] = used + minutes
            accepted.append(request['id'])
        else:
            skipped.append({'id': request['id'], 'customer': request['customer'], 'reason': reason})
    return {'accepted': accepted, 'skipped': skipped}


def mutate(conn, action, payload):
    today = current_day(conn)
    if action == 'fetch':
        rows = conn.execute('SELECT id FROM requests WHERE received<=? AND fetched=0', (today,)).fetchall()
        conn.execute('UPDATE requests SET fetched=1 WHERE received<=? AND fetched=0', (today,))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('fetched_day',?)", (today,))
        apply_fetched_updates(conn)
        audit(conn, 'Messages fetched', f'{len(rows)} new messages; valid changes and cancellations applied. New orders await your decision.')
    elif action == 'batch':
        fetched = conn.execute("SELECT value FROM meta WHERE key='fetched_day'").fetchone()
        if not fetched or fetched[0] != today:
            raise InputError('Fetch today’s DMs before accepting orders and moving to the next day.')
        preview = batch_preview(conn)
        for request_id in preview['accepted']:
            base_mutate(conn, 'decision', {'id': request_id, 'decision': 'accepted'})
        apply_fetched_updates(conn)
        pickups = conn.execute("SELECT id,customer FROM orders WHERE pickup<=? AND status='accepted'", (today,)).fetchall()
        for order in pickups:
            conn.execute("UPDATE orders SET status='collected' WHERE id=?", (order['id'],))
            audit(conn, 'Picked up at 5 PM', f"#{order['id']} · {order['customer']}")
        result = {'day': today, 'accepted': len(preview['accepted']), 'skipped': len(preview['skipped']), 'picked_up': len(pickups)}
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_batch',?)", (json.dumps(result),))
        audit(conn, 'Day closed', f"{result['accepted']} new orders accepted; {result['skipped']} incompatible requests kept for review; {result['picked_up']} orders picked up at 5 PM.")
        base_mutate(conn, 'advance', {})
    elif action == 'advance':
        raise InputError('Use Accept all and go to next day so orders and pickups are checked together.')
    elif action == 'decision':
        request = conn.execute('SELECT * FROM requests WHERE id=?', (payload.get('id'),)).fetchone()
        if not request or not request['fetched']:
            raise InputError('Fetch this message before making a decision.')
        base_mutate(conn, action, payload)
        apply_fetched_updates(conn)
    elif action == 'request':
        base_mutate(conn, action, payload)
        # The audit insert changes last_insert_rowid, so identify the request
        # by its own table within the same transaction.
        conn.execute("UPDATE requests SET fetched=1 WHERE id=(SELECT MAX(id) FROM requests WHERE source='Manual entry')")
        apply_fetched_updates(conn)
    else:
        base_mutate(conn, action, payload)
        if action == 'helper':
            apply_fetched_updates(conn)


def state(conn):
    today = current_day(conn)
    requests = []
    for row in conn.execute('SELECT * FROM requests WHERE received<=? AND fetched=1 ORDER BY received,id', (today,)):
        item = dict(row)
        item['allowed'], item['assessment'], item['target'] = assess(conn, row)
        item['classification'] = ('Requested' if item['allowed'] else 'Incompatible') if row['status']=='pending' else row['status'].title()
        requests.append(item)
    fetched = conn.execute("SELECT value FROM meta WHERE key='fetched_day'").fetchone()
    last_batch = conn.execute("SELECT value FROM meta WHERE key='last_batch'").fetchone()
    monday = (valid_date(today) - timedelta(days=valid_date(today).weekday())).isoformat()
    sunday = (valid_date(monday) + timedelta(days=6)).isoformat()
    sales = conn.execute("SELECT COALESCE(SUM(quantity*CASE product WHEN 'cake' THEN 120 ELSE 40 END),0) FROM orders WHERE pickup BETWEEN ? AND ? AND status IN ('accepted','collected')", (monday,sunday)).fetchone()[0]
    costs = 100*conn.execute('SELECT COUNT(*) FROM helpers WHERE day BETWEEN ? AND ?', (monday,sunday)).fetchone()[0]
    return {'day': today, 'orders': [dict(r) for r in conn.execute('SELECT * FROM orders ORDER BY pickup,id')],
            'requests': requests, 'helpers': [dict(r) for r in conn.execute('SELECT * FROM helpers ORDER BY day')],
            'audit': [dict(r) for r in conn.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 200')],
            'source_count': 25, 'future_count': conn.execute('SELECT COUNT(*) FROM requests WHERE received>?', (today,)).fetchone()[0],
            'fetched_today': bool(fetched and fetched[0]==today),
            'unfetched_today': conn.execute('SELECT COUNT(*) FROM requests WHERE received<=? AND fetched=0',(today,)).fetchone()[0],
            'batch_preview': batch_preview(conn), 'last_batch': json.loads(last_batch[0]) if last_batch else None,
            'week_sales': sales, 'week_helper_costs': costs, 'week_start': monday, 'week_end': sunday}


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Butter & Batch | Bakery order tracker</title>
<style>
:root{--ink:#202b3d;--muted:#687183;--navy:#24364b;--green:#396247;--line:#e5e8ed;--paper:#f6f7fa;--mint:#dcebdc;--purple:#ece8f8;--red:#a13d43;--gold:#e5b469}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 'Segoe UI',Arial,sans-serif}button,input,select,textarea{font:inherit}button,a,input,select,textarea{outline-offset:4px}button{cursor:pointer}button:disabled{cursor:not-allowed;opacity:.45}a{color:var(--green)}button{border:1px solid var(--line);border-radius:9px;background:white;padding:9px 15px;color:var(--ink);font-weight:600}button:hover:not(:disabled){background:#f1f4f1;border-color:#a6bba9}.primary{background:var(--navy);border-color:var(--navy);color:white}.primary:hover:not(:disabled){background:#3a5068}.green{background:var(--green);color:white;border-color:var(--green)}.quiet{background:transparent}.danger{color:var(--red)}.small{padding:6px 11px;font-size:14px}.muted{color:var(--muted)}small,.eyebrow{font-size:13px}.eyebrow{font-weight:700;letter-spacing:.13em;text-transform:uppercase}h1,h2,h3,p{margin-top:0}h1{font-size:32px;letter-spacing:-1px;line-height:1.2;margin-bottom:8px}h2{font-size:20px;letter-spacing:-.4px;margin-bottom:4px}h3{font-size:17px;margin-bottom:8px}.shell{display:grid;grid-template-columns:238px 1fr;min-height:100vh}aside{position:fixed;inset:0 auto 0 0;width:238px;background:white;border-right:1px solid var(--line);display:flex;flex-direction:column;padding:32px 20px}.brand{display:flex;gap:11px;align-items:center;margin:0 8px 40px;font-family:Georgia,serif;font-size:23px;font-weight:bold;line-height:1.1;letter-spacing:-.7px}.brand svg{width:40px;height:44px}.brand small{display:block;font:11px 'Segoe UI',sans-serif;letter-spacing:2px;margin-top:8px;color:var(--muted)}nav{display:grid;gap:8px}nav button{border:0;background:transparent;text-align:left;display:flex;gap:12px;align-items:center;padding:12px;font-weight:500}nav button svg{height:20px;width:20px}nav .active{background:#eaf0e9;color:#305a3d;font-weight:700}.navcount{margin-left:auto;background:#396247;color:white;border-radius:6px;padding:0 6px;font-size:13px}.sidebottom{margin-top:auto;padding:22px 10px 0}.sidecard{background:#f5f5fa;padding:16px;border-radius:12px;font-size:14px;margin-bottom:24px}.sidecard b{display:block;margin-bottom:5px}.saved{font-size:13px;color:var(--muted)}.saved:before{content:'✓';color:var(--green);margin-right:7px}main{grid-column:2;min-width:0}.topbar{background:rgba(255,255,255,.8);border-bottom:1px solid var(--line);height:77px;display:flex;justify-content:space-between;align-items:center;padding:0 38px;gap:16px}.crumb{font-size:14px;color:var(--muted)}.account{width:34px;height:34px;border-radius:50%;background:var(--purple);display:grid;place-items:center;font-size:13px;font-weight:bold}.topactions{display:flex;gap:14px;align-items:center}.content{max-width:1490px;padding:34px 38px 50px;margin:auto}.pagehead{display:flex;justify-content:space-between;gap:20px;align-items:center;margin-bottom:24px}.pagehead p{margin:0;color:var(--muted);font-size:15px}.hero{background:#dce9da;border:1px solid #d0dfcd;border-radius:16px;min-height:204px;position:relative;padding:27px 32px;overflow:hidden;display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}.hero h2{font-family:Georgia,serif;font-size:37px;font-weight:normal;line-height:1.13;letter-spacing:-1px;margin:10px 0 14px}.hero p{font-size:14px;margin:0;color:#4f6655;max-width:450px}.hero svg{width:215px;height:160px;flex-shrink:0;margin-right:30px}.hero .eyebrow{color:#45614b}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:26px}.stat{background:white;border:1px solid var(--line);border-radius:12px;padding:19px 22px}.stat .label{font-size:14px;color:var(--muted)}.stat strong{display:block;font-size:30px;font-weight:600;letter-spacing:-1px;margin:4px 0}.stat small{color:var(--muted)}.stat strong em{font-style:normal;font-size:16px;color:var(--muted);font-weight:400}.grid{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:24px;align-items:start}.panel{background:white;border:1px solid var(--line);border-radius:13px;padding:22px;margin-bottom:23px;min-width:0}.panelhead{display:flex;justify-content:space-between;align-items:center;gap:15px;margin-bottom:22px}.panelhead p{margin:0;font-size:14px;color:var(--muted)}.week{display:grid;grid-template-columns:repeat(7,1fr);gap:9px}.day{display:flex;flex-direction:column;text-align:center;padding:14px 7px;border:1px solid var(--line);border-radius:10px;min-width:0;background:white}.day.today{border-color:#658b68;background:#f2f7f1}.day .weekday{font-size:13px;color:var(--muted)}.day .number{font-size:22px;margin:4px 0 16px;font-weight:600}.bar{height:6px;border-radius:5px;background:#edf0f1;overflow:hidden;display:flex;margin:7px 0}.bar span{background:#75a17a;display:block;height:100%}.bar span.full{background:#c88e40}.day .hours{font-size:13px;white-space:nowrap}.day .helpersign{font-size:12px;color:var(--green);height:20px;margin-top:6px}.legend{display:flex;gap:16px;justify-content:space-between;font-size:13px;color:var(--muted);margin-top:18px}.legend b{font-weight:400;color:var(--green)}.tablewrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px;text-align:left}th{color:var(--muted);font-weight:500;font-size:13px;padding:0 12px 12px;border-bottom:1px solid var(--line);white-space:nowrap}td{padding:16px 12px;border-bottom:1px solid #eff0f3;vertical-align:middle}tr:last-child td{border-bottom:0}td:first-child,th:first-child{padding-left:0}td:last-child,th:last-child{padding-right:0}.person{display:flex;gap:10px;align-items:center;white-space:nowrap}.avatar{height:34px;width:34px;border-radius:11px;display:grid;place-items:center;background:var(--purple);color:#625484;font-weight:600;flex-shrink:0}.sub{font-size:12px;color:var(--muted);display:block}.badge{display:inline-flex;font-size:12px;border-radius:6px;padding:4px 8px;white-space:nowrap;font-weight:600;background:#eef0f4;color:#5f6979}.badge.ok{background:#e9f2e9;color:#3e7047}.badge.warn{background:#fff0d9;color:#9a651d}.badge.bad{background:#fae8e8;color:#a23f45}.badge.purple{background:#eee9fa;color:#765b9b}.rowactions{display:flex;gap:7px}.attention{background:#fff8e9;border:1px solid #f0e0be}.attention .icon{font-size:23px;margin-bottom:13px}.attention p{font-size:14px;color:#7d6846;margin:12px 0 17px}.product{padding:14px 0;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);font-size:14px}.product:last-child{border:0;padding-bottom:0}.product .symbol{font-size:25px;margin-right:12px}.productinfo{display:flex;align-items:center}.product small{display:block;color:var(--muted)}.product strong{font-size:17px;font-weight:600}.note{font-size:13px;color:var(--muted);line-height:1.6}.toolbar{display:flex;gap:12px;margin-bottom:22px;flex-wrap:wrap}.toolbar input{flex:1;min-width:180px}.toolbar select{min-width:140px}input,select,textarea{border:1px solid #d7dce3;border-radius:8px;padding:10px 12px;background:white;color:var(--ink);min-width:0}select{appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='m1 1 5 5 5-5' fill='none' stroke='%23687183' stroke-width='1.5'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;padding-right:33px}.empty{padding:36px 20px;text-align:center;color:var(--muted)}.inboxgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:20px}.messagecard{padding:23px;background:white;border:1px solid var(--line);border-radius:13px}.messagecard header{display:flex;justify-content:space-between;align-items:center;margin-bottom:17px}.bubble{background:#f4f3f8;border-radius:4px 16px 16px 16px;padding:17px;white-space:pre-line;font-size:15px;line-height:1.7;min-height:140px}.assessment{font-size:14px;min-height:66px;margin:16px 0;color:var(--green)}.assessment.blocked{color:#9e6736}.messagecard footer{display:flex;gap:9px;align-items:center;flex-wrap:wrap}.messagecard .source{display:block;font-size:12px;margin-top:18px;color:var(--muted)}.planlist{display:grid;gap:15px}.planday{display:grid;grid-template-columns:145px 1fr 190px;gap:25px;align-items:center}.planday .datebig{font-size:24px;font-weight:600}.planday p{font-size:14px;color:var(--muted);margin:3px 0}.planday .right{text-align:right}.planorders{font-size:14px;margin-top:10px;color:var(--muted)}.briefgrid{display:grid;grid-template-columns:1fr 1fr;gap:24px}.briefgrid .panel{margin:0}.briefgrid ul{padding-left:20px}.briefgrid li{margin:12px 0}.briefgrid p{color:var(--muted)}.rule{padding:14px 0;border-bottom:1px solid var(--line)}.rule:last-child{border:0}.rule strong{display:block}.rule p{font-size:14px;margin:5px 0}.sourcefiles{display:flex;flex-wrap:wrap;gap:9px}.sourcefiles a{padding:8px 12px;border:1px solid var(--line);border-radius:7px;text-decoration:none;font-size:14px}.auditrow{display:grid;grid-template-columns:110px 190px 1fr;gap:18px;border-bottom:1px solid var(--line);padding:14px 0;font-size:14px}.auditrow small{color:var(--muted)}dialog{border:0;border-radius:17px;padding:30px;width:520px;max-width:calc(100vw - 30px);color:var(--ink);box-shadow:0 25px 100px #16213144}dialog::backdrop{background:#18233466;backdrop-filter:blur(3px)}.dialoghead{display:flex;justify-content:space-between;gap:16px;margin-bottom:20px}.dialoghead button{border:0;padding:0 8px;font-size:24px;align-self:start;background:none}label{font-size:14px;display:grid;gap:7px;margin-bottom:17px}.formrow{display:grid;grid-template-columns:1fr 1fr;gap:16px}textarea{min-height:95px;resize:vertical}.dialogfooter{display:flex;justify-content:flex-end;gap:9px;margin-top:24px}.error{color:var(--red);font-size:14px;min-height:20px}#toast{position:fixed;bottom:25px;left:calc(50% + 100px);transform:translateX(-50%);padding:13px 22px;border-radius:10px;background:var(--navy);color:white;z-index:10;box-shadow:0 8px 30px #24364b33;max-width:90vw}#toast[hidden]{display:none}.countline{color:var(--muted);font-size:14px;margin-bottom:18px}.capacitykey{font-size:13px;color:var(--muted)}.headingactions{display:flex;gap:10px;flex-wrap:wrap}.mobilebrand{display:none}@media(min-width:1600px){.content{padding-top:40px}.hero{min-height:230px}}@media(max-width:1200px){.grid{grid-template-columns:1fr}.rightcol{display:grid;grid-template-columns:1fr 1fr;gap:20px}.hero svg{margin:0;width:170px}.content{padding:28px}.topbar{padding:0 28px}.stat{padding:16px}.week{gap:7px}}@media(max-width:850px){.shell{grid-template-columns:1fr}aside{position:static;width:auto;padding:16px;border:0;border-bottom:1px solid var(--line)}.brand{margin:0 0 15px;font-size:21px}.brand small{display:none}.brand svg{height:32px;width:32px}nav{display:flex;gap:4px;overflow:auto}nav button{white-space:nowrap;font-size:14px;padding:9px}nav button svg{display:none}.sidebottom{display:none}main{grid-column:1}.topbar{height:auto;min-height:65px;padding:12px 20px}.crumb{display:none}.topactions{width:100%;justify-content:space-between}.content{padding:24px 18px}.stats{grid-template-columns:1fr 1fr;gap:12px}.briefgrid{grid-template-columns:1fr}.pagehead{align-items:start}h1{font-size:28px}.planday{grid-template-columns:100px 1fr}.planday .right{grid-column:2}.hero{padding:25px}.hero h2{font-size:32px}.hero svg{width:120px}.auditrow{grid-template-columns:90px 1fr}.auditrow span:last-child{grid-column:2}#toast{left:50%}.panel{padding:18px}}@media(max-width:540px){.hero svg{display:none}.hero h2{font-size:34px}.rightcol{display:block}.pagehead{flex-direction:column;gap:16px}.week{gap:4px}.day{padding:10px 2px}.day .weekday{font-size:12px}.day .hours{font-size:11px}.day .helpersign{font-size:10px}.stat strong{font-size:27px}.stat .label{font-size:13px}.legend{flex-wrap:wrap}.topactions{gap:7px}.topactions button{font-size:12px;padding:7px}.topactions .date{font-size:13px}.account{display:none}.formrow{grid-template-columns:1fr;gap:0}.panelhead{flex-wrap:wrap}.inboxgrid{grid-template-columns:1fr}}
.shell{grid-template-columns:238px minmax(0,1fr)}aside,.grid>div{min-width:0}.stats{grid-template-columns:repeat(4,minmax(0,1fr))}.week{grid-template-columns:repeat(7,minmax(0,1fr))}@media(max-width:1200px){.grid{grid-template-columns:minmax(0,1fr)}}@media(max-width:850px){.shell{grid-template-columns:minmax(0,1fr)}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.briefgrid{grid-template-columns:minmax(0,1fr)}}
.workflow{background:#eaf0e9;display:flex;justify-content:space-between;gap:28px;align-items:center}.workflow p{margin:5px 0}.doclink{font-size:14px;align-self:center}.finalweek{grid-template-columns:repeat(7,minmax(0,1fr))}.finalweek .day{text-align:left;align-items:stretch;padding:13px 10px}.finalweek .day .number{margin:3px 0 7px}.dayorders{flex:1;font-size:13px;min-height:100px;margin:12px 0}.dayorders div{margin-bottom:13px}.dayorders span{display:block;font-size:12px;color:var(--muted)}.finalweek .small{font-size:12px;padding:6px 3px}.pickupitem{display:flex;align-items:center;gap:25px;padding:10px 0;border-bottom:1px solid var(--line)}.pickupitem .person{min-width:160px}.pickupitem>span{flex:1}.rowactions{flex-wrap:wrap}.topbar{height:auto;min-height:77px;padding-top:12px;padding-bottom:12px}.topactions{flex-wrap:wrap;justify-content:flex-end}@media(max-width:1200px){.finalweek{grid-template-columns:repeat(4,minmax(0,1fr))}.dayorders{min-height:70px}.crumb{display:none}}@media(max-width:700px){.workflow{display:block}.finalweek{grid-template-columns:repeat(2,minmax(0,1fr))}.pickupitem{flex-wrap:wrap;gap:10px}.pickupitem .person{min-width:100%}.topactions{justify-content:flex-start}.topactions .date{width:100%}.dayorders span,.finalweek .small{font-size:14px}.finalweek .day .hours{font-size:14px}}
.pickupitem>.badge{flex:0 0 auto}
</style></head><body>
<div class="shell"><aside><div class="brand"><svg viewBox="0 0 44 48" fill="none" aria-hidden="true"><rect x="1" y="1" width="42" height="46" rx="12" fill="#24364b"/><path d="M12 24h20l-3 12H15zM12 24c-5-7 4-11 7-9-1-7 10-8 10-1 7 0 9 8 3 10M19 28l1 5m5-5-1 5" stroke="#e8dfb5" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg><div>butter & batch<small>THE BAKER'S WORKSPACE</small></div></div><nav id="nav"></nav><div class="sidebottom"><div class="sidecard"><b>A little planning goes a long way.</b><span class="muted">Fresh bakes. Happy customers.<br>A calmer kitchen.</span></div><div class="saved">Saved on this computer</div></div></aside><main><div class="topbar"><div class="crumb">My bakery <span style="margin:0 12px;color:#bcc1cb">/</span> Workspace</div><div class="topactions"><span class="date" id="today"></span><button class="small" id="fetchbutton" onclick="fetchMessages()">Fetch today’s DMs</button><button class="small primary" id="batchbutton" onclick="advanceDialog()">Accept all and go to next day</button><span class="account" title="Home bakery">HB</span></div></div><div class="content" id="content"><p>Opening your bakery…</p></div></main></div>
<dialog id="modal"></dialog><div id="toast" role="status" hidden></div>
<script>
let S, tab='overview', filter='all', search='', weekOffset=0, busy=false;
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icons={overview:'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',orders:'M8 3h8v4H8zM6 5H4v16h16V5h-2M8 12h8M8 16h6',inbox:'M4 3h16v18H4zM8 7h8M8 11h8M8 15h5',plan:'M3 5h18v16H3zM7 2v6M17 2v6M3 11h18M7 15h3',brief:'M5 3h14v18H5zM9 7h6M9 11h6M9 15h4'};
const svg=k=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${icons[k]}"/></svg>`;
const money=n=>'$'+n.toLocaleString('en-CA'), add=(d,n)=>{let x=new Date(d+'T12:00:00');x.setDate(x.getDate()+n);return [x.getFullYear(),String(x.getMonth()+1).padStart(2,'0'),String(x.getDate()).padStart(2,'0')].join('-')}, fmt=(d,o={month:'short',day:'numeric'})=>new Date(d+'T12:00:00').toLocaleDateString('en-CA',o);
const hours=o=>o.quantity*(o.product==='cake'?2:.5), amount=o=>o.quantity*(o.product==='cake'?120:40), active=o=>['accepted','collected'].includes(o.status), label=o=>o.kind==='cancel'?'Cancel order':o.quantity+(o.product==='cake'?(o.quantity===1?' cake':' cakes'):' dozen cupcakes'), used=d=>S.orders.filter(o=>o.pickup===d&&active(o)).reduce((n,o)=>n+hours(o),0), helper=d=>S.helpers.some(h=>h.day===d), cap=d=>helper(d)?12:8;
const pending=()=>S.requests.filter(r=>r.status==='pending');
function badge(text,type=''){return `<span class="badge ${type}">${esc(text)}</span>`}function person(o){return `<div class="person"><span class="avatar">${esc(o.customer.slice(0,1))}</span><div><b>${esc(o.customer)}</b><span class="sub">${o.kind?'Instagram request':'#'+o.id}</span></div></div>`}
async function refresh(){const r=await fetch('/api/state');if(!r.ok)throw Error('Could not open the bakery.');S=await r.json();render()}
async function post(action,payload={}){if(busy)return false;busy=true;try{const r=await fetch('/api/'+action,{method:'POST',headers:{'Content-Type':'application/json','X-Bakery-App':'1'},body:JSON.stringify(payload)});const data=await r.json();if(!r.ok)throw Error(data.error);S=data;render();return true}catch(e){toast(e.message);return false}finally{busy=false}}
function toast(text){$('#toast').textContent=text;$('#toast').hidden=false;clearTimeout(window.tt);window.tt=setTimeout(()=>$('#toast').hidden=true,6000)}
function go(name){tab=name;filter='all';search='';render()}
function render(){const names={overview:'Overview',orders:'Orders',inbox:'Instagram inbox',plan:'Baking plan',brief:'Project brief'};$('#nav').innerHTML=Object.entries(names).map(([k,v])=>`<button class="${tab===k?'active':''}" onclick="go('${k}')" ${tab===k?'aria-current="page"':''}>${svg(k)}${v}${k==='inbox'&&pending().length?`<span class="navcount">${pending().length}</span>`:''}</button>`).join('');$('#batchbutton').disabled=!S.fetched_today;$('#fetchbutton').textContent=S.fetched_today?'Fetch DMs again':'Fetch today’s DMs';$('#today').textContent=fmt(S.day,{weekday:'short',month:'short',day:'numeric',year:'numeric'});$('#content').innerHTML=({overview,orders,inbox,plan,brief}[tab])()}
function head(title,desc,action=''){return `<div class="pagehead"><div><h1>${title}</h1><p>${desc}</p></div><div class="headingactions">${action}</div></div>`}
function weekDays(){let d=new Date(S.day+'T12:00:00'),m=(d.getDay()+6)%7;return Array.from({length:7},(_,i)=>add(S.day,i-m+weekOffset*7))}
function week(){let days=weekDays();return `<div class="panel"><div class="panelhead"><div><h2>The week’s baking plan</h2><p>${fmt(days[0])} – ${fmt(days[6])}, ${days[0].slice(0,4)} · accepted orders only</p></div><button class="small" onclick="go('plan')">Full baking plan</button></div><div class="week finalweek">${days.map(d=>{const os=S.orders.filter(o=>o.pickup===d&&active(o));return `<div class="day ${d===S.day?'today':''}"><span class="weekday">${fmt(d,{weekday:'short'})}</span><span class="number">${+d.slice(-2)}</span><b class="hours">${used(d)} / ${cap(d)} h</b><span class="bar"><span class="${used(d)/cap(d)>=.85?'full':''}" style="width:${used(d)/cap(d)*100}%"></span></span><div class="dayorders">${os.map(o=>`<div><b>${esc(o.customer)}</b><span>${esc(label(o))}</span></div>`).join('')||'<span>No orders</span>'}</div><button class="small" onclick="dayDialog('${d}')">${helper(d)?'Helper booked':d>=add(S.day,2)?'+4 h · $100':'View day'}</button></div>`}).join('')}</div><p class="note" style="margin:16px 0 0">8 oven hours per day. One helper adds 4 hours for $100 with two days’ notice. Cake demand tends to arrive earlier; plan helpers before the Saturday rush.</p></div>`}

function requestRows(list){return `<div class="tablewrap"><table><thead><tr><th>Customer</th><th>Request</th><th>Pickup</th><th>Rules and capacity</th><th>Decision</th></tr></thead><tbody>${list.map(r=>`<tr><td>${person(r)}</td><td>${r.kind==='change'?'<span class="sub">Change to</span>':''}${esc(label(r))}</td><td>${fmt(r.pickup,{weekday:'short',month:'short',day:'numeric'})}<span class="sub">5:00 PM</span></td><td><span>${esc(r.assessment)}</span><span class="sub">${used(r.pickup)} / ${cap(r.pickup)} h already booked</span></td><td><div class="rowactions"><button class="small" onclick="requestDialog(${r.id})">Details</button><button class="small" onclick="rejectDialog(${r.id})">Reject</button></div></td></tr>`).join('')}</tbody></table></div>`}

function overview(){
const newOrders=pending().filter(r=>r.kind==='new'),compatible=newOrders.filter(r=>r.allowed),blocked=pending().filter(r=>!r.allowed),updates=S.requests.filter(r=>r.kind!=='new'&&r.status==='accepted'),pickups=S.orders.filter(o=>o.pickup===S.day&&active(o));
return head('A clear plan for every bake.',`Today: ${fmt(S.day,{weekday:'long',month:'long',day:'numeric'})}. All pickups at 5 PM.`,`<button onclick="newDialog()">+ Record a request</button><a class="doclink" href="/README.html" target="_blank">How to use the app</a>`)+
`<section class="workflow panel"><div><h2>${S.fetched_today?'Today’s messages are fetched.':'Start with today’s messages.'}</h2><p class="note">${S.fetched_today?`${compatible.length} new orders fit individually; the batch preview checks them together. Valid changes and cancellations have been applied.`:`${S.unfetched_today} messages are ready to fetch. Fetch uses the supplied Instagram files for the current planning day.`}</p></div><p class="note">Fetch DMs · review requests and helpers · accept all and close the day</p></section>
<div class="stats"><div class="stat"><span class="label">This week’s booked sales</span><strong>${money(S.week_sales)}</strong><small>${fmt(S.week_start)} – ${fmt(S.week_end)}</small></div><div class="stat"><span class="label">This week’s helper costs</span><strong>${money(S.week_helper_costs)}</strong><small>$100 per booked day</small></div><div class="stat"><span class="label">Balance before other costs</span><strong>${money(S.week_sales-S.week_helper_costs)}</strong><small>Ingredients and other costs excluded</small></div><div class="stat"><span class="label">Today’s pickups</span><strong>${pickups.length}</strong><small>${used(S.day)} / ${cap(S.day)} oven hours</small></div></div>
<section class="panel"><div class="panelhead"><div><h2>Today’s pickup list</h2><p>Baked today · ready at 5 PM</p></div>${badge(`${cap(S.day)-used(S.day)} oven hours free`,'ok')}</div>${pickups.length?`<div class="pickuplist">${pickups.map(o=>`<div class="pickupitem">${person(o)}<span>${esc(label(o))}</span><b>${hours(o)} h</b>${badge(o.status==='collected'?'Picked up':'Accepted','ok')}</div>`).join('')}</div>`:'<p class="muted">No accepted pickups for today.</p>'}</section>
${week()}
<section class="panel"><div class="panelhead"><div><h2>New requests that fit now</h2><p>Facts beside each decision. Reject unwanted requests; use Accept all for the rest.</p></div>${badge(compatible.length+' requested','ok')}</div>${compatible.length?requestRows(compatible):`<p class="muted">${S.fetched_today?'No eligible new orders waiting.':'Fetch today’s DMs to see new requests.'}</p>`}</section>
<section class="panel"><div class="panelhead"><div><h2>Incompatible requests</h2><p>These cannot be accepted with the current notice, capacity or matching order.</p></div>${badge(blocked.length+' incompatible','warn')}</div>${blocked.length?requestRows(blocked):'<p class="muted">No incompatible requests to review.</p>'}</section>
<section class="panel"><h2>Changes and cancellations</h2><p class="note">Valid fetched updates are applied to the original accepted order. Invalid updates above preserve the original.</p>${updates.length?`<div class="tablewrap"><table><thead><tr><th>Customer</th><th>Update</th><th>Pickup</th><th>Result</th></tr></thead><tbody>${updates.slice().reverse().map(r=>`<tr><td>${esc(r.customer)}</td><td>${r.kind==='change'?'Changed to '+esc(label(r)):'Cancelled'}</td><td>${fmt(r.pickup)}</td><td>${badge('Applied','ok')}</td></tr>`).join('')}</tbody></table></div>`:'<p class="muted">No valid changes or cancellations fetched yet.</p>'}</section>`}

function orders(){const list=S.orders.filter(o=>(filter==='all'||o.status===filter)&&`${o.customer} ${o.id} ${o.product}`.toLowerCase().includes(search.toLowerCase()));return head('Every bake, in its place.','Your accepted orders, changes, cancellations and pickups.',`<button onclick="location.href='/api/export'">Export orders</button><button class="primary" onclick="newDialog()">+ New request</button>`)+`<div class="toolbar"><input aria-label="Search orders" placeholder="Search customer, order number or product…" value="${esc(search)}" oninput="search=this.value;renderOrderResults()"><select aria-label="Order status" onchange="filter=this.value;renderOrderResults()">${['all','accepted','collected','cancelled'].map(x=>`<option value="${x}" ${filter===x?'selected':''}>${x==='all'?'All statuses':x[0].toUpperCase()+x.slice(1)}</option>`).join('')}</select></div><div class="panel" id="orderresults">${orderTable(list)}</div>`}
function orderTable(list){return `<div class="countline">${list.length} orders</div><div class="tablewrap"><table><thead><tr><th>Customer</th><th>Product</th><th>Pickup · 5 PM</th><th>Oven time</th><th>Value</th><th>Status</th><th></th></tr></thead><tbody>${list.map(o=>`<tr><td>${person(o)}</td><td>${esc(label(o))}</td><td>${fmt(o.pickup,{weekday:'short',month:'short',day:'numeric'})}</td><td>${hours(o)} h</td><td>${money(amount(o))}</td><td>${badge(o.status==='collected'?'Picked up':o.status,o.status==='cancelled'?'bad':'ok')}</td><td><div class="rowactions">${o.status==='accepted'?`<button class="small" onclick="newDialog(${o.id})">Change</button>${o.pickup<=S.day?`<button class="small" onclick="collect(${o.id})">Collected</button>`:''}`:''}</div></td></tr>`).join('')}</tbody></table>${!list.length?'<div class="empty">No orders match your search.</div>':''}</div>`}
function renderOrderResults(){let list=S.orders.filter(o=>(filter==='all'||o.status===filter)&&`${o.customer} ${o.id} ${o.product}`.toLowerCase().includes(search.toLowerCase()));$('#orderresults').innerHTML=orderTable(list)}
function inbox(){let list=S.requests.filter(r=>(filter==='all'||r.status===filter||filter==='blocked'&&r.status==='pending'&&!r.allowed)&&`${r.customer} ${r.body}`.toLowerCase().includes(search.toLowerCase())).slice().reverse();return head('From DMs to done.','Every Instagram message, with the details and rules in one place.',`<button class="primary" onclick="newDialog()">+ Record a message</button>`)+`<div class="toolbar"><input aria-label="Search messages" placeholder="Search customer or message…" value="${esc(search)}" onchange="search=this.value;render()"><select aria-label="Message status" onchange="filter=this.value;render()">${['all','pending','blocked','accepted','rejected'].map(x=>`<option ${filter===x?'selected':''} value="${x}">${x==='all'?'All messages':x[0].toUpperCase()+x.slice(1)}</option>`).join('')}</select></div><p class="countline">${S.requests.length} messages received through ${fmt(S.day)} · ${S.unfetched_today} ready to fetch · ${S.future_count} arrive on later days. Fetch reads the supplied local source files; there is no live Instagram connection.</p><div class="inboxgrid">${list.map(r=>`<article class="messagecard"><header>${person(r)}${badge(r.classification,r.allowed?'ok':'warn')}</header><div class="bubble">${esc(r.body)}</div><p class="note" style="margin:12px 0 0"><b>${esc(label(r))}</b> · ${fmt(r.pickup,{weekday:'short',month:'short',day:'numeric'})} · 5 PM</p><div class="source">Received ${fmt(r.received,{weekday:'long',month:'short',day:'numeric'})} · ${esc(r.source)}</div><div class="assessment ${r.allowed?'':'blocked'}">${esc(r.status==='pending'?r.assessment:r.reason)}</div><footer>${r.status==='pending'?`<button class="green small" ${r.allowed?'':'disabled'} onclick="decide(${r.id},'accepted')">${r.kind==='cancel'?'Approve cancellation':r.kind==='change'?'Approve change':'Accept order'}</button><button class="small" onclick="rejectDialog(${r.id})">Reject</button>${!r.allowed?`<button class="small quiet" onclick="requestDialog(${r.id})">Details</button>`:''}`:badge(r.status,r.status==='accepted'?'ok':'bad')}</footer></article>`).join('')||'<div class="panel empty">No messages match this view.</div>'}</div>`}
function plan(){let days=weekDays();return head('Room for something sweet.','Protect your oven time. Plan your helpers before the rush.',`<button onclick="weekOffset--;render()">Previous week</button><button onclick="weekOffset=0;render()">This week</button><button onclick="weekOffset++;render()">Next week</button>`)+`<p class="countline">${fmt(days[0])} – ${fmt(days[6])} · helpers add 4 hours, cost $100/day and need two days’ notice.</p><div class="planlist">${days.map(d=>{let os=S.orders.filter(o=>o.pickup===d&&active(o));return `<section class="panel planday"><div><span class="eyebrow muted">${fmt(d,{weekday:'long'})}</span><div class="datebig">${fmt(d)}</div>${d===S.day?badge('Today','purple'):''}</div><div><div style="display:flex;justify-content:space-between"><b>${used(d)} / ${cap(d)} oven hours</b><span class="capacitykey">${cap(d)-used(d)} h free</span></div><div class="bar" style="height:10px"><span class="${used(d)/cap(d)>=.85?'full':''}" style="width:${used(d)/cap(d)*100}%"></span></div><div class="planorders">${os.length?os.map(o=>`${esc(o.customer)} · ${esc(label(o))}`).join('<br>'):'No accepted orders yet.'}</div></div><div class="right">${helper(d)?badge('Helper booked · $100','ok'):badge('Baker only')}<p>${os.length} pickup${os.length===1?'':'s'} · 5:00 PM</p><button class="small" onclick="dayDialog('${d}')">Manage day</button></div></section>`}).join('')}</div>`}
function brief(){const gross=S.orders.filter(active).reduce((n,o)=>n+amount(o),0);return head('The recipe for a calmer bakery.','Project goals, operating rules, source information and a clear decision trail.')+`<div class="briefgrid"><section class="panel"><span class="eyebrow muted">Project goal</span><h2 style="margin-top:12px">Turn Instagram requests into a workable baking plan.</h2><p style="margin-top:14px">Help a home baker track cupcake and cake orders, make every acceptance decision, protect daily capacity and arrange extra help in time.</p><ul><li>Track new orders, quantity changes and cancellations.</li><li>Show rule checks before the baker accepts or rejects.</li><li>Apply valid fetched changes and cancellations; preserve the original if invalid.</li><li>Show oven hours, pickups, revenue and helper costs.</li><li>Keep a record of each decision and its reason.</li></ul></section><section class="panel"><span class="eyebrow muted">The non-negotiables</span><div class="rule"><strong>8 oven hours each day</strong><p>Bake every order on its pickup day. All pickups are at 5 PM.</p></div><div class="rule"><strong>2 calendar days’ notice</strong><p>Orders, changes, cancellations and helper bookings need two days. Monday permits Wednesday or later. Both the received date and decision date must qualify; waiting does not extend a deadline.</p></div><div class="rule"><strong>One product per order</strong><p>Cupcakes: $40/dozen, 30 min/dozen. Cakes: $120 each, 2 h each. Whole quantities only.</p></div><div class="rule"><strong>One helper per day</strong><p>$100 for 4 extra oven hours. A booked helper raises the daily limit to 12 hours. No extra helper beyond the stated option is modeled.</p></div></section><section class="panel"><span class="eyebrow muted">What the Instagram pages add</span><h2 style="margin-top:12px">Plan ahead for the weekend.</h2><p style="margin-top:14px">Cake orders usually arrive a week ahead. Most cupcake orders come in the last three days. Saturday is the busiest day. These are planning hints, not additional acceptance rules.</p><p>13 existing orders (#101–#113) are already accepted. All 25 messages from November 23–29, 2026 are loaded, including changes and cancellations. Fetch reveals messages through the planning day and applies valid changes and cancellations. The starting planning day is Monday, November 23.</p><div class="sourcefiles">${['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'].map(d=>`<a href="/source/${d}" target="_blank" rel="noopener">${d} DMs</a>`).join('')}<a href="/source/orders">Starting orders CSV</a></div></section><section class="panel"><span class="eyebrow muted">How this tracker works</span><div class="rule"><strong>The baker makes the call</strong><p>Checks use fixed rules. New orders require an explicit Accept or Accept all action. Fetch applies valid changes and cancellations. Closing the day marks due orders picked up at 5 PM. Instagram source pages are imported records; there is no live Instagram connection or automatic reply.</p></div><div class="rule"><strong>Saved locally, every time</strong><p>All data is stored in bakery.db on this computer. Export orders for a spreadsheet or download a full database backup.</p><button class="small" onclick="location.href='/api/backup'">Download database backup</button></div><div class="rule"><strong>Revenue is not profit</strong><p>Booked revenue: ${money(gross)}. Helpers: ${money(S.helpers.length*100)}. Balance before ingredients and other costs: ${money(gross-S.helpers.length*100)}. Cancellations are excluded; payments and ingredient costs are not tracked.</p></div><div class="rule"><strong>Local project setup</strong><p>Python standard library, one app.py, plain HTML/CSS/JavaScript and SQLite. No installs or AI services. Run with python app.py at 127.0.0.1:8000. The unrelated Workflow Builder in the archive is separate.</p></div></section></div><section class="panel" style="margin-top:24px"><h2>Decision trail</h2><p class="note">Latest 200 events. Planning dates are shown separately from the database recording timestamp.</p>${S.audit.map(a=>`<div class="auditrow"><small>${fmt(a.day)}<br>${esc(a.recorded_at)} UTC</small><b>${esc(a.action)}</b><span>${esc(a.detail)}</span></div>`).join('')}</section>`}
function modal(title,body,footer=''){let m=$('#modal');m.innerHTML=`<div class="dialoghead"><h2>${title}</h2><button onclick="closeModal()" aria-label="Close dialog">×</button></div>${body}<div class="error" id="formerror" role="alert"></div>${footer?`<div class="dialogfooter">${footer}</div>`:''}`;if(!m.open)m.showModal()}function closeModal(){$('#modal').close()}
function requestDialog(id){let r=S.requests.find(x=>x.id===id);modal(`${esc(r.customer)} · ${r.kind} request`,`<div class="bubble">${esc(r.body)}</div><p class="note" style="margin-top:18px">Received ${fmt(r.received)} · pickup ${fmt(r.pickup)} at 5 PM</p><p class="assessment ${r.allowed?'':'blocked'}">${esc(r.assessment)}</p><p class="note">Accepted oven time on pickup day: ${used(r.pickup)} / ${cap(r.pickup)} h. ${r.kind==='change'?'A change replaces the original quantity; it does not add a second order.':''}</p>`,`<button onclick="rejectDialog(${r.id})">Reject</button>${!r.allowed&&r.assessment.includes('helper first')?`<button onclick="dayDialog('${r.pickup}')">Book a helper</button>`:''}<button class="green" ${r.allowed?'':'disabled'} onclick="decide(${r.id},'accepted')">${r.kind==='new'?'Accept order':'Approve '+r.kind}</button>`)}
async function decide(id,decision,reason=''){if(await post('decision',{id,decision,reason})){closeModal();toast(decision==='accepted'?'Request approved. Your baking plan is up to date.':'Request rejected. Accepted orders are preserved.')}}
function rejectDialog(id){let r=S.requests.find(x=>x.id===id);modal(`Reject ${esc(r.customer)}’s request`,`<label>Reason (optional)<textarea id="rejectreason" maxlength="500" placeholder="Add a note for your records…"></textarea></label><p class="note">This records your decision. No Instagram message is sent.</p>`,`<button onclick="closeModal()">Back</button><button class="primary" onclick="decide(${id},'rejected',$('#rejectreason').value)">Reject request</button>`)}
function newDialog(id){let o=id?S.orders.find(x=>x.id===id):null;modal(o?'Record a change or cancellation':'Record a customer request',`<form id="requestform" onsubmit="submitRequest(event)"><label>Customer name<input name="customer" required maxlength="80" value="${esc(o?.customer||'')}"></label><div class="formrow"><label>Request type<select name="kind" onchange="toggleProduct(this.value)">${(o?['change','cancel']:['new','change','cancel']).map(x=>`<option value="${x}">${x==='new'?'New order':x==='change'?'Change quantity/product':'Cancel order'}</option>`).join('')}</select></label><label>Pickup date<input type="date" name="pickup" required min="${S.day}" value="${o?.pickup||add(S.day,2)}"></label></div><div class="formrow" id="productfields"><label>Product<select name="product"><option value="cupcakes">Cupcakes (per dozen)</option><option value="cake" ${o?.product==='cake'?'selected':''}>Cake (each)</option></select></label><label>Quantity<input type="number" name="quantity" required min="1" max="100" step="1" value="${o?.quantity||1}"></label></div><label>Original message or notes<textarea name="body" maxlength="4000" placeholder="Paste the customer’s message here…"></textarea></label><p class="note">Received on ${fmt(S.day)}. New orders await review. Valid changes and cancellations apply immediately, using the Fetch rules. Changes and cancellations match the customer name and original pickup date.</p><div class="dialogfooter"><button type="button" onclick="closeModal()">Cancel</button><button type="submit" class="primary">Save request</button></div></form>`)}
function toggleProduct(kind){$('#productfields').hidden=kind==='cancel';$('#productfields').style.display=kind==='cancel'?'none':'grid';$('#productfields input').required=kind!=='cancel'}
async function submitRequest(e){e.preventDefault();let f=new FormData(e.target),p=Object.fromEntries(f);p.quantity=Number(p.quantity);if(await post('request',p)){closeModal();tab='inbox';filter='pending';render();toast('Request saved. Review it in your inbox.')}}
function dayDialog(d){let os=S.orders.filter(o=>o.pickup===d&&active(o)),has=helper(d),eligible=d>=add(S.day,2),canRemove=eligible&&used(d)<=8;modal(fmt(d,{weekday:'long',month:'long',day:'numeric'}),`<p><b>${used(d)} of ${cap(d)} oven hours</b> booked · ${cap(d)-used(d)} h available</p><div class="bar" style="height:10px"><span style="width:${used(d)/cap(d)*100}%"></span></div><p class="note">All baking is on this day. Customer pickups are at 5 PM.</p>${os.length?os.map(o=>`<div class="product"><div><b>${esc(o.customer)}</b><small>${esc(label(o))}</small></div><span>${hours(o)} h</span></div>`).join(''):'<p class="muted">No accepted orders.</p>'}<div class="rule"><strong>${has?'Helper booked':'Need extra oven time?'}</strong><p>${has?'Your helper adds 4 hours for $100.':'Book one helper for $100 to add 4 hours, bringing capacity to 12 hours.'}</p>${!eligible?'<p class="error">The two-day deadline for helper bookings or cancellations has passed.</p>':has&&!canRemove?'<p class="error">Accepted orders need this helper’s extra capacity.</p>':''}</div>`,`<button onclick="closeModal()">Close</button><button class="${has?'':'primary'}" ${has?!canRemove?'disabled':'':!eligible?'disabled':''} onclick="setHelper('${d}',${!has})">${has?'Cancel helper':'Book helper · $100'}</button>`)}
async function setHelper(day,book){if(await post('helper',{day,book})){closeModal();toast(book?'Helper booked. Four more oven hours are available.':'Helper booking cancelled.')}}
function advanceDialog(){const preview=S.batch_preview;modal('Accept all and close today?',`<p><b>${preview.accepted.length} eligible new orders</b> will be accepted, checked one at a time in received order.</p><p>${preview.skipped.length} incompatible new requests will stay for review. No helper is booked automatically.</p>${preview.skipped.length?`<div class="bubble">${preview.skipped.map(r=>`${esc(r.customer)}: ${esc(r.reason)}`).join('<br>')}</div>`:''}<p class="note">Closing ${fmt(S.day)} represents passing the 5 PM pickup time. Accepted orders due today become Picked up, then the planning date moves to ${fmt(add(S.day,1),{weekday:'long',month:'long',day:'numeric'})}. Fetch the new day’s DMs next.</p>`,`<button onclick="closeModal()">Keep reviewing</button><button class="primary" onclick="advance()">Confirm accept all and next day</button>`)}

async function advance(){if(await post('batch')){closeModal();weekOffset=0;render();const r=S.last_batch;toast(`${r.accepted} new orders accepted · ${r.skipped} left for review · ${r.picked_up} picked up. Fetch the new day’s DMs.`)}}
async function fetchMessages(){if(await post('fetch')){toast('DMs fetched. Valid changes and cancellations applied; new orders are ready for review.')}}

async function collect(id){if(await post('collect',{id}))toast('Pickup recorded. Happy customer, happy baker.')}
refresh().catch(e=>{$('#content').innerHTML='<div class="panel"><h2>Could not load your bakery</h2><p>'+esc(e.message)+'</p><button onclick="location.reload()">Try again</button></div>'});
</script></body></html>'''


def make_handler(db_path):
    class Handler(BaseHTTPRequestHandler):
        def send(self, status, data, content_type='application/json; charset=utf-8', filename=None):
            if isinstance(data, (dict, list)):
                data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            elif isinstance(data, str):
                data = data.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if filename:
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path == '/':
                    return self.send(200, PAGE, 'text/html; charset=utf-8')
                if path in ('/README.html', '/PLAN.html', '/TEST_RESULTS.html'):
                    return self.send(200, (ROOT / path.lstrip('/')).read_bytes(), 'text/html; charset=utf-8')
                if path == '/favicon.ico':
                    return self.send(204, b'', 'image/x-icon')
                if path.startswith('/source/'):
                    name = path.split('/')[-1]
                    if name not in DAYS + ['orders']:
                        return self.send(404, {'error': 'Source not found.'})
                    file = ROOT / ('existing_orders.csv' if name == 'orders' else f'InstagramOrders_{name}.html')
                    return self.send(200, file.read_bytes(), 'text/csv; charset=utf-8' if name == 'orders' else 'text/html; charset=utf-8')
                with closing(connect(db_path)) as conn:
                    if path == '/api/state':
                        return self.send(200, state(conn))
                    if path == '/api/export':
                        output = io.StringIO(newline='')
                        writer = csv.writer(output)
                        writer.writerow(['Order', 'Customer', 'Product', 'Quantity', 'Pickup date', 'Pickup time', 'Status', 'Oven hours', 'Order value'])
                        for r in conn.execute('SELECT * FROM orders ORDER BY pickup,id'):
                            customer = r['customer']
                            if customer.lstrip().startswith(('=', '+', '-', '@')):
                                customer = "'" + customer
                            writer.writerow([r['id'], customer, r['product'], r['quantity'], r['pickup'], '17:00', r['status'], r['quantity']*PRODUCTS[r['product']]['minutes']/60, r['quantity']*PRODUCTS[r['product']]['price']])
                        return self.send(200, '\ufeff'+output.getvalue(), 'text/csv; charset=utf-8', 'bakery-orders.csv')
                    if path == '/api/backup':
                        with closing(sqlite3.connect(':memory:')) as backup:
                            conn.backup(backup)
                            data = backup.serialize()
                        return self.send(200, data, 'application/vnd.sqlite3', 'bakery-backup.db')
                self.send(404, {'error': 'Not found.'})
            except (OSError, sqlite3.Error) as error:
                print(f'Read failed: {error}')
                self.send(500, {'error': 'Could not read bakery data. Check the terminal for details.'})

        def do_POST(self):
            if self.headers.get('X-Bakery-App') != '1' or self.headers.get('Content-Type') != 'application/json':
                return self.send(403, {'error': 'Use the bakery app to make changes.'})
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return self.send(403, {'error': 'Changes must come from this app.'})
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 16384:
                    raise InputError('Request is empty or too large.')
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise InputError('Expected a request object.')
                with closing(connect(db_path)) as conn, conn:
                    conn.execute('BEGIN IMMEDIATE')
                    mutate(conn, urlparse(self.path).path.removeprefix('/api/'), payload)
                    result = state(conn)
                self.send(200, result)
            except (InputError, ValueError, TypeError) as error:
                self.send(400, {'error': str(error)})
            except sqlite3.Error as error:
                print(f'Save failed: {error}')
                self.send(500, {'error': 'Could not save your change. Please try again.'})
    return Handler


def main():
    parser = argparse.ArgumentParser(description='Run the local bakery order tracker.')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--db', type=Path, default=ROOT / 'bakery.db')
    parser.add_argument('--open-browser', action='store_true', help='Open the bakery in your default browser.')
    args = parser.parse_args()
    initialize(args.db)
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(args.db))
    except OSError:
        if args.open_browser:
            webbrowser.open(f'http://127.0.0.1:{args.port}')
        raise SystemExit(f'Port {args.port} is already in use. If your bakery is already running, use its open browser tab. Otherwise try: python app.py --port 8001')
    print(f'Butter & Batch is ready: http://127.0.0.1:{args.port}', flush=True)
    print(f'Your changes are saved in {args.db.name}. Keep this window open. Press Ctrl+C to stop.', flush=True)
    if args.open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
