import os
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests
import sqlite3
import json
import random
import string
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__, static_folder='static')
CORS(app)

ONTOPO_BASE = 'https://ontopo.com/api'
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'events.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            city TEXT NOT NULL,
            date TEXT NOT NULL,
            size INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            name TEXT NOT NULL,
            times TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(event_id, name)
        )''')


init_db()
_token_cache = {}

CITY_DATA = {
    # מרכז
    'תל אביב':      ('29421469', 'telavivjaffa'),
    'ירושלים':      ('29384685', 'jerusalem'),
    'רמת גן':       ('87918421', 'rag-giv-area'),
    'גבעתיים':      ('87918421', 'rag-giv-area'),
    'חולון':        ('60235869', 'holon-batyam-area'),
    'מודיעין':      ('49473533', 'modiin_area'),
    'פתח תקווה':    ('74902764', 'petah_tikva_area'),
    # שפלה
    'ראשון לציון':  ('39514882', 'rishon_lezion'),
    'רחובות':       ('58943955', 'rehovot'),
    'נס ציונה':     ('39514882', 'rishon_lezion'),
    'יבנה':         ('93786391', 'ashdod'),
    'אשדוד':        ('93786391', 'ashdod'),
    # דרום
    'באר שבע':      ('83166822', 'beer_sheva'),
    'אילת':         ('71154151', 'eilat_area'),
    # שרון
    'הרצליה':       ('62204663', 'herzeliya'),
    'רעננה':        ('71499188', 'raanana'),
    'כפר סבא':      ('71499188', 'raanana'),
    'הוד השרון':    ('62204663', 'herzeliya'),
    'רמת השרון':    ('62204663', 'herzeliya'),
    'נתניה':        ('19467447', 'netanya_area'),
    'פתח תקווה':    ('74902764', 'petah_tikva_area'),
    # צפון
    'חיפה':         ('11243454', 'haifa'),
    'נהריה':        ('11243454', 'haifa'),
    'עכו':          ('11243454', 'haifa'),
    'טבריה':        ('11243454', 'haifa'),
    'נצרת':         ('11243454', 'haifa'),
}


def get_token():
    if _token_cache.get('jwt'):
        return _token_cache['jwt']
    r = requests.post(f'{ONTOPO_BASE}/loginAnonymously', json={}, timeout=10)
    r.raise_for_status()
    token = r.json()['jwt_token']
    _token_cache['jwt'] = token
    return token


def ontopo_headers():
    return {'Content-Type': 'application/json', 'token': get_token()}


def to_ontopo_date(date_str):
    # YYYY-MM-DD → YYYYMMDD
    return date_str.replace('-', '')


def to_ontopo_time(time_str):
    # HH:MM → HHMM
    return time_str.replace(':', '')


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/event/<event_id>')
def event_page(event_id):
    return send_from_directory('static', 'event.html')


@app.route('/api/events', methods=['POST'])
def create_event():
    data = request.json or {}
    city = data.get('city', 'תל אביב')
    date = data.get('date')
    size = data.get('size', 2)
    if not date:
        return jsonify({'error': 'date required'}), 400
    event_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    with get_db() as conn:
        conn.execute(
            'INSERT INTO events (id, city, date, size, created_at) VALUES (?,?,?,?,?)',
            (event_id, city, date, size, datetime.utcnow().isoformat())
        )
    return jsonify({'id': event_id})


@app.route('/api/events/<event_id>')
def get_event(event_id):
    with get_db() as conn:
        event = conn.execute('SELECT * FROM events WHERE id=?', (event_id,)).fetchone()
        if not event:
            return jsonify({'error': 'not found'}), 404
        participants = conn.execute(
            'SELECT name, times, updated_at FROM participants WHERE event_id=? ORDER BY updated_at',
            (event_id,)
        ).fetchall()
    return jsonify({
        'id': event['id'],
        'city': event['city'],
        'date': event['date'],
        'size': event['size'],
        'participants': [
            {'name': p['name'], 'times': json.loads(p['times'])}
            for p in participants
        ]
    })


@app.route('/api/events/<event_id>/join', methods=['POST'])
def join_event(event_id):
    data = request.json or {}
    name = (data.get('name') or '').strip()
    times = data.get('times', [])
    if not name:
        return jsonify({'error': 'name required'}), 400
    with get_db() as conn:
        event = conn.execute('SELECT id FROM events WHERE id=?', (event_id,)).fetchone()
        if not event:
            return jsonify({'error': 'not found'}), 404
        conn.execute(
            '''INSERT INTO participants (event_id, name, times, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(event_id, name) DO UPDATE SET times=excluded.times, updated_at=excluded.updated_at''',
            (event_id, name, json.dumps(times), datetime.utcnow().isoformat())
        )
    return jsonify({'ok': True})


@app.route('/api/search')
def search():
    date = request.args.get('date')
    time_val = request.args.get('time')
    size = request.args.get('size', '2')
    city = request.args.get('city', 'תל אביב')
    venue_type = request.args.get('venue_type', '')

    city_info = CITY_DATA.get(city, CITY_DATA['תל אביב'])
    marketplace_id, geocode = city_info

    try:
        token_resp = requests.post(
            f'{ONTOPO_BASE}/search_token',
            json={
                'marketplace_id': marketplace_id,
                'criteria': {
                    'date': to_ontopo_date(date),
                    'time': to_ontopo_time(time_val),
                    'size': str(size),
                },
                'locale': 'he',
                'traits': ['reservation'],
                'analytics': {'distributor_id': 'il', 'platform': 'web'},
                'geocodes': [geocode],
                **({'venue_type': venue_type} if venue_type else {}),
            },
            headers=ontopo_headers(),
            timeout=10,
        )
        token_resp.raise_for_status()
        search_id = token_resp.json().get('search_id')

        import time
        time.sleep(2)

        results_resp = requests.post(
            f'{ONTOPO_BASE}/search_request',
            json={'search_id': search_id},
            headers=ontopo_headers(),
            timeout=15,
        )
        results_resp.raise_for_status()
        data = results_resp.json()

        posts = data.get('posts') or []

        # Build basic list first
        basic = []
        for p in posts:
            post = p.get('post', p)
            avail = p.get('availability', {})
            slug = post.get('page_slug') or post.get('slug')
            times = []
            for area in avail.get('areas', []):
                for opt in area.get('options', []):
                    t = opt.get('time', '')
                    if len(t) == 4:
                        t = f"{t[:2]}:{t[2:]}"
                    if t and opt.get('method') == 'seat':
                        times.append(t)
            basic.append({
                'slug': slug,
                'name': post.get('venue_name') or post.get('name'),
                'times': times[:6],
            })

        # Fetch venue details in parallel
        def fetch_details(item):
            try:
                r = requests.get(
                    f'{ONTOPO_BASE}/slug_content',
                    params={'slug': item['slug'], 'locale': 'he'},
                    headers=ontopo_headers(),
                    timeout=8,
                )
                d = r.json()
                gallery = [g['image'] for g in d.get('gallery9', []) if g.get('image')]
                video_url = (d.get('result_video') or {}).get('url')
                menus = d.get('menus') or []
                return {**item,
                    'image': d.get('cover_mobile') or d.get('cover') or d.get('logo'),
                    'gallery': gallery,
                    'video': video_url,
                    'website': d.get('website') if (d.get('website') or '').startswith('http') else None,
                    'instagram': d.get('instagram'),
                    'address': d.get('address'),
                    'cuisine': ' · '.join(filter(None, [d.get('tag1','').strip(), d.get('tag2','').strip()])),
                    'booking_url': f"https://ontopo.com/he/il/page/{item['slug']}",
                    'menus': menus,
                }
            except Exception:
                return {**item, 'booking_url': f"https://ontopo.com/he/il/page/{item['slug']}"}

        restaurants = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(fetch_details, item): item for item in basic}
            for future in as_completed(futures):
                restaurants.append(future.result())

        # Keep original order
        slug_order = [b['slug'] for b in basic]
        restaurants.sort(key=lambda r: slug_order.index(r['slug']) if r['slug'] in slug_order else 999)

        return jsonify({'results': restaurants})

    except Exception as e:
        _token_cache.clear()
        return jsonify({'error': str(e), 'results': []}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=False, host='0.0.0.0', port=port)
