import os
import json
import sqlite3
import requests
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, g
from dotenv import load_dotenv

# Load env from PA root
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '../../.env'))

app = Flask(__name__)

APIFY_API_TOKEN = os.getenv('APIFY_API_TOKEN', '')
GOOGLE_AI_API_KEY = os.getenv('GOOGLE_AI_API_KEY', '')
# On Railway/cloud: use /tmp or RAILWAY_VOLUME_MOUNT_PATH if set
_data_dir = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', os.path.dirname(__file__))
DB_PATH = os.path.join(_data_dir, 'tiktok_scraper.db')

DEMO_MODE = not bool(APIFY_API_TOKEN)

# ──────────────────────────────────────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────────────────────────────────────

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS bookmarks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id TEXT UNIQUE NOT NULL,
        video_data TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS recent_searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT NOT NULL,
        date_range TEXT,
        video_count INTEGER,
        result_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        brief_template TEXT,
        brand_bible TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS briefs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER,
        project_name TEXT,
        video_id TEXT,
        video_url TEXT,
        brief_content TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()

# ──────────────────────────────────────────────────────────────────────────────
# APIFY
# ──────────────────────────────────────────────────────────────────────────────

APIFY_HASHTAG_ACTOR = 'clockworks~tiktok-hashtag-scraper'
APIFY_COMMENTS_ACTOR = 'clockworks~tiktok-comments-scraper'

def search_tiktok(keyword: str, date_range: str, max_items: int) -> list:
    if DEMO_MODE:
        return _mock_videos(keyword, max_items)

    # clockworks~tiktok-hashtag-scraper -- strip # and spaces
    tag = keyword.lstrip('#').replace(' ', '')
    input_data = {
        'hashtags': [tag],
        'maxItems': max_items,
    }

    url = f'https://api.apify.com/v2/acts/{APIFY_HASHTAG_ACTOR}/run-sync-get-dataset-items'
    params = {'token': APIFY_API_TOKEN, 'timeout': 120, 'memory': 512}
    try:
        resp = requests.post(url, json=input_data, params=params, timeout=130)
        resp.raise_for_status()
        items = resp.json()
        # Sort by playCount descending
        items.sort(key=lambda x: x.get('playCount', 0), reverse=True)
        # Apply date filter client-side if set
        date_from = _date_from(date_range)
        if date_from:
            cutoff = datetime.fromisoformat(date_from)
            filtered = []
            for v in items:
                ts = v.get('createTimeISO', '')
                if ts:
                    try:
                        vdate = datetime.fromisoformat(ts.replace('Z', '+00:00')).replace(tzinfo=None)
                        if vdate >= cutoff:
                            filtered.append(v)
                    except Exception:
                        filtered.append(v)
                else:
                    filtered.append(v)
            items = filtered
        return [_normalize_video(v) for v in items[:max_items]]
    except Exception as e:
        app.logger.error(f'Apify error: {e}')
        return []

def fetch_comments(video_url: str) -> list:
    if DEMO_MODE:
        return _mock_comments()

    input_data = {'postURLs': [video_url], 'maxComments': 50}
    url = f'https://api.apify.com/v2/acts/{APIFY_COMMENTS_ACTOR}/run-sync-get-dataset-items'
    params = {'token': APIFY_API_TOKEN, 'timeout': 60, 'memory': 256}
    try:
        resp = requests.post(url, json=input_data, params=params, timeout=70)
        resp.raise_for_status()
        return [c.get('text', '') for c in resp.json() if c.get('text')]
    except Exception as e:
        app.logger.error(f'Apify comments error: {e}')
        return []

def _normalize_video(v: dict) -> dict:
    author = v.get('authorMeta') or {}
    video_meta = v.get('videoMeta') or {}
    return {
        'id': v.get('id', str(uuid.uuid4())),
        'caption': v.get('text', ''),
        'author': author.get('nickName') or author.get('name', 'Unknown'),
        'author_handle': author.get('name', ''),
        'author_avatar': author.get('avatar', '') or author.get('originalAvatarUrl', ''),
        'views': v.get('playCount', 0),
        'likes': v.get('diggCount', 0),
        'comments': v.get('commentCount', 0),
        'shares': v.get('shareCount', 0),
        'bookmarks': v.get('collectCount', 0),
        'duration': video_meta.get('duration', 0),
        'cover': video_meta.get('coverUrl', '') or video_meta.get('originalCoverUrl', ''),
        'download_url': (v.get('mediaUrls') or [''])[0],
        'tiktok_url': v.get('webVideoUrl', ''),
        'subtitles': video_meta.get('subtitleLinks', []),
        'transcription_url': video_meta.get('transcriptionLink', ''),
        'created_at': v.get('createTimeISO', v.get('createTime', '')),
    }

def _date_from(date_range: str) -> str:
    if not date_range or date_range == 'all':
        return ''
    days_map = {'7': 7, '30': 30, '90': 90, '180': 180, '365': 365}
    days = days_map.get(date_range)
    if days:
        return (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    return ''

# ──────────────────────────────────────────────────────────────────────────────
# GEMINI
# ──────────────────────────────────────────────────────────────────────────────

def analyze_video_with_gemini(video: dict, comments: list) -> dict:
    if not GOOGLE_AI_API_KEY:
        return {'error': 'No Gemini API key configured'}

    import google.generativeai as genai
    genai.configure(api_key=GOOGLE_AI_API_KEY)
    model = genai.GenerativeModel('gemini-2.0-flash')

    # Build context from available data
    caption = video.get('caption', '')
    author = video.get('author', '')
    views = video.get('views', 0)
    likes = video.get('likes', 0)

    # Try to get transcript from subtitles
    transcript = _fetch_transcript(video.get('subtitles', []))

    comment_text = '\n'.join(comments[:30]) if comments else 'No comments available'

    prompt = f"""Analyze this TikTok video for creative/marketing research.

VIDEO DETAILS:
- Creator: @{author}
- Caption: {caption}
- Views: {views:,} | Likes: {likes:,}
- Transcript: {transcript or 'No transcript available'}

TOP COMMENTS:
{comment_text}

Provide a detailed analysis in JSON format:
{{
  "visual_hook": "Describe what grabs attention in the first 2-3 seconds",
  "undeniable_proof": "What makes this content credible or compelling?",
  "theme": "Main topic/theme in 2-4 words",
  "funnel_stage": "One of: Awareness / Consideration / Conversion",
  "hook_formula": "The underlying hook formula (curiosity gap / transformation / social proof / etc)",
  "why_it_works": "2-3 sentences on why this performs well",
  "common_questions": ["question from comments 1", "question 2", "question 3"],
  "key_insights": ["insight 1", "insight 2", "insight 3"]
}}

Return ONLY valid JSON, no markdown."""

    try:
        # If video is downloadable, try to use multimodal analysis
        download_url = video.get('download_url', '')
        if download_url and download_url.startswith('http'):
            try:
                return _analyze_with_video_file(model, download_url, prompt)
            except Exception as e:
                app.logger.warning(f'Video file analysis failed, falling back to text: {e}')

        response = model.generate_content(prompt)
        text = response.text.strip()
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        return json.loads(text)
    except json.JSONDecodeError:
        return {'error': 'Could not parse Gemini response', 'raw': response.text[:500]}
    except Exception as e:
        return {'error': str(e)}

def _analyze_with_video_file(model, download_url: str, prompt: str) -> dict:
    import google.generativeai as genai

    resp = requests.get(download_url, timeout=30, stream=True)
    resp.raise_for_status()

    with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
        tmp_path = f.name

    try:
        video_file = genai.upload_file(tmp_path, mime_type='video/mp4')
        # Wait for processing
        for _ in range(10):
            if video_file.state.name == 'ACTIVE':
                break
            time.sleep(2)
            video_file = genai.get_file(video_file.name)

        response = model.generate_content([video_file, prompt])
        text = response.text.strip()
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        return json.loads(text)
    finally:
        os.unlink(tmp_path)
        try:
            genai.delete_file(video_file.name)
        except Exception:
            pass

def _fetch_transcript(subtitle_links: list) -> str:
    if not subtitle_links:
        return ''
    for link in subtitle_links:
        url = link.get('downloadLink') or link.get('tiktokLink') or link.get('Link') or link.get('link') or (link if isinstance(link, str) else '')
        if not url:
            continue
        try:
            resp = requests.get(url, timeout=10)
            if resp.ok:
                # Parse SRT/VTT to plain text
                lines = []
                for line in resp.text.split('\n'):
                    line = line.strip()
                    if line and not line.isdigit() and '-->' not in line and not line.startswith('WEBVTT'):
                        lines.append(line)
                return ' '.join(lines)[:2000]
        except Exception:
            continue
    return ''

def generate_brief(video: dict, project: dict, analysis: dict = None) -> str:
    if not GOOGLE_AI_API_KEY:
        return 'No Gemini API key configured'

    import google.generativeai as genai
    genai.configure(api_key=GOOGLE_AI_API_KEY)
    model = genai.GenerativeModel('gemini-2.0-flash')

    template = project.get('brief_template', '') or _default_brief_template()
    brand_bible = project.get('brand_bible', '') or ''
    project_name = project.get('name', 'Unknown')

    analysis_text = json.dumps(analysis, indent=2) if analysis else 'No prior analysis'

    prompt = f"""You are a creative strategist at an AI agency. Generate a creative brief based on this TikTok video.

TIKTOK VIDEO:
- Caption: {video.get('caption', '')}
- Creator: @{video.get('author', '')}
- Views: {video.get('views', 0):,} | Likes: {video.get('likes', 0):,}
- URL: {video.get('tiktok_url', '')}

AI ANALYSIS OF VIDEO:
{analysis_text}

CLIENT: {project_name}

BRAND BIBLE:
{brand_bible[:3000] if brand_bible else 'Not provided'}

BRIEF TEMPLATE TO FOLLOW:
{template}

Generate a complete creative brief following the template above, adapted to this client's brand and inspired by the TikTok video's hook and style. Be specific and actionable."""

    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f'Error generating brief: {e}'

def _default_brief_template() -> str:
    return """# Creative Brief

**Client:** [CLIENT NAME]
**Date:** [DATE]
**Source Video:** [TIKTOK URL]

## Objective
[What do we want to achieve with this content?]

## Target Audience
[Who are we talking to?]

## Hook (adapted from TikTok source)
[The opening hook to use in our creative]

## Key Message
[The single most important thing to communicate]

## Proof / Credibility
[How do we back up our claim?]

## Call to Action
[What should the audience do?]

## Visual Direction
[Visual style, tone, references]

## Format
- Platform:
- Length:
- Format (video/static/carousel):

## Deliverables
[List of assets needed]"""

# ──────────────────────────────────────────────────────────────────────────────
# MOCK DATA
# ──────────────────────────────────────────────────────────────────────────────

def _mock_videos(keyword: str, count: int) -> list:
    templates = [
        {'views': 2400000, 'likes': 187000, 'comments': 3200, 'theme': f'{keyword} transformation'},
        {'views': 890000, 'likes': 72000, 'comments': 1100, 'theme': f'Secret {keyword} hack'},
        {'views': 4100000, 'likes': 320000, 'comments': 8900, 'theme': f'{keyword} before/after'},
        {'views': 340000, 'likes': 28000, 'comments': 560, 'theme': f'POV: {keyword} routine'},
        {'views': 1200000, 'likes': 95000, 'comments': 2400, 'theme': f'{keyword} review honest'},
        {'views': 760000, 'likes': 61000, 'comments': 980, 'theme': f'{keyword} tips'},
        {'views': 5800000, 'likes': 460000, 'comments': 12000, 'theme': f'{keyword} viral moment'},
        {'views': 290000, 'likes': 22000, 'comments': 410, 'theme': f'Testing {keyword}'},
        {'views': 1600000, 'likes': 128000, 'comments': 3600, 'theme': f'{keyword} unboxing'},
        {'views': 450000, 'likes': 36000, 'comments': 720, 'theme': f'{keyword} day in my life'},
    ]
    covers = [
        'https://picsum.photos/seed/tiktok1/400/700',
        'https://picsum.photos/seed/tiktok2/400/700',
        'https://picsum.photos/seed/tiktok3/400/700',
        'https://picsum.photos/seed/tiktok4/400/700',
        'https://picsum.photos/seed/tiktok5/400/700',
    ]
    creators = ['@sarah.creates', '@brandguru', '@viralvibes', '@contentqueen', '@trendmaster',
                '@creativeflow', '@hookmaster', '@ugcpro', '@influencer.daily', '@adspy']

    results = []
    for i in range(min(count, len(templates))):
        t = templates[i % len(templates)]
        vid_id = f'demo_{keyword}_{i}'
        results.append({
            'id': vid_id,
            'caption': f'{t["theme"]} ✨ #trending #{keyword.replace(" ", "")} #fyp',
            'author': creators[i % len(creators)].replace('@', ''),
            'author_handle': creators[i % len(creators)],
            'author_avatar': f'https://i.pravatar.cc/48?u={i}',
            'views': t['views'],
            'likes': t['likes'],
            'comments': t['comments'],
            'shares': t['likes'] // 10,
            'bookmarks': t['likes'] // 5,
            'duration': 15 + (i * 7) % 45,
            'cover': covers[i % len(covers)],
            'download_url': '',
            'tiktok_url': f'https://www.tiktok.com/@demo/video/{vid_id}',
            'subtitles': [],
            'created_at': (datetime.now() - timedelta(days=i * 3)).isoformat(),
        })
    return sorted(results, key=lambda x: x['views'], reverse=True)

def _mock_comments() -> list:
    return [
        'How long did this take you?',
        'What product are you using?',
        'This is exactly what I needed!',
        'Does this work for beginners?',
        'Where did you get that?',
        'Saving this for later',
        'I tried this and it actually worked',
        'Can you do a tutorial?',
    ]

def _mock_analysis() -> dict:
    return {
        'visual_hook': 'Demo mode: Unexpected visual contrast in the first frame with bold text overlay',
        'undeniable_proof': 'Demo mode: Before/after transformation showing clear, measurable results',
        'theme': 'Product Transformation',
        'funnel_stage': 'Consideration',
        'hook_formula': 'Transformation / Before-After',
        'why_it_works': 'Demo mode: The creator uses a strong visual hook with social proof. The pacing keeps viewers engaged through the first 3 seconds, pushing past the "scroll threshold".',
        'common_questions': ['How long does this take?', 'What product do you use?', 'Does this work for everyone?'],
        'key_insights': ['Authenticity drives engagement more than production quality', 'Specific results outperform vague claims', 'Comment section shows high purchase intent']
    }

# ──────────────────────────────────────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', demo_mode=DEMO_MODE)

@app.route('/api/status')
def status():
    return jsonify({
        'demo_mode': DEMO_MODE,
        'gemini_configured': bool(GOOGLE_AI_API_KEY),
        'apify_configured': bool(APIFY_API_TOKEN),
    })

@app.route('/api/search', methods=['POST'])
def search():
    data = request.json or {}
    keyword = data.get('keyword', '').strip()
    date_range = data.get('date_range', 'all')
    max_items = min(int(data.get('max_items', 10)), 50)

    if not keyword:
        return jsonify({'error': 'Keyword required'}), 400

    videos = search_tiktok(keyword, date_range, max_items)

    db = get_db()
    db.execute(
        'INSERT INTO recent_searches (keyword, date_range, video_count, result_count) VALUES (?, ?, ?, ?)',
        (keyword, date_range, max_items, len(videos))
    )
    db.commit()

    return jsonify({'videos': videos, 'demo_mode': DEMO_MODE, 'count': len(videos)})

@app.route('/api/analyze', methods=['POST'])
def analyze():
    data = request.json or {}
    video = data.get('video', {})

    if not video:
        return jsonify({'error': 'Video data required'}), 400

    if DEMO_MODE:
        return jsonify(_mock_analysis())

    comments = fetch_comments(video.get('tiktok_url', ''))
    analysis = analyze_video_with_gemini(video, comments)
    return jsonify(analysis)

@app.route('/api/transcript', methods=['POST'])
def transcript():
    data = request.json or {}
    subtitles = data.get('subtitles', [])
    transcription_url = data.get('transcription_url', '')

    if DEMO_MODE:
        return jsonify({'transcript': 'Demo mode: Transcript not available. Add APIFY_API_TOKEN to .env for live data.'})

    # Try transcriptionLink first (cleaner)
    if transcription_url:
        try:
            resp = requests.get(transcription_url, timeout=10)
            if resp.ok:
                tj = resp.json()
                # Apify transcription format: list of {text, start, end}
                if isinstance(tj, list):
                    text = ' '.join(s.get('text', '') for s in tj if s.get('text'))
                    if text:
                        return jsonify({'transcript': text})
        except Exception:
            pass

    text = _fetch_transcript(subtitles)
    if not text:
        return jsonify({'transcript': None, 'message': 'No transcript available for this video'})
    return jsonify({'transcript': text})

# Bookmarks
@app.route('/api/bookmarks', methods=['GET'])
def get_bookmarks():
    db = get_db()
    rows = db.execute('SELECT * FROM bookmarks ORDER BY created_at DESC').fetchall()
    return jsonify([dict(r) | {'video_data': json.loads(r['video_data'])} for r in rows])

@app.route('/api/bookmarks', methods=['POST'])
def add_bookmark():
    data = request.json or {}
    video = data.get('video', {})
    video_id = video.get('id', '')

    if not video_id:
        return jsonify({'error': 'Video ID required'}), 400

    db = get_db()
    try:
        db.execute(
            'INSERT INTO bookmarks (video_id, video_data) VALUES (?, ?)',
            (video_id, json.dumps(video))
        )
        db.commit()
        return jsonify({'success': True, 'message': 'Bookmarked'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Already bookmarked'}), 409

@app.route('/api/bookmarks/<video_id>', methods=['DELETE'])
def remove_bookmark(video_id):
    db = get_db()
    db.execute('DELETE FROM bookmarks WHERE video_id = ?', (video_id,))
    db.commit()
    return jsonify({'success': True})

@app.route('/api/bookmarks/check/<video_id>', methods=['GET'])
def check_bookmark(video_id):
    db = get_db()
    row = db.execute('SELECT id FROM bookmarks WHERE video_id = ?', (video_id,)).fetchone()
    return jsonify({'bookmarked': row is not None})

# Recent searches
@app.route('/api/recent-searches', methods=['GET'])
def get_recent_searches():
    db = get_db()
    rows = db.execute('SELECT * FROM recent_searches ORDER BY created_at DESC LIMIT 20').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/recent-searches/<int:search_id>', methods=['DELETE'])
def delete_recent_search(search_id):
    db = get_db()
    db.execute('DELETE FROM recent_searches WHERE id = ?', (search_id,))
    db.commit()
    return jsonify({'success': True})

# Projects
@app.route('/api/projects', methods=['GET'])
def get_projects():
    db = get_db()
    rows = db.execute('SELECT * FROM projects ORDER BY created_at DESC').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/projects', methods=['POST'])
def create_project():
    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Project name required'}), 400

    db = get_db()
    cursor = db.execute(
        'INSERT INTO projects (name, brief_template, brand_bible) VALUES (?, ?, ?)',
        (name, data.get('brief_template', ''), data.get('brand_bible', ''))
    )
    db.commit()
    project = db.execute('SELECT * FROM projects WHERE id = ?', (cursor.lastrowid,)).fetchone()
    return jsonify(dict(project)), 201

@app.route('/api/projects/<int:project_id>', methods=['PUT'])
def update_project(project_id):
    data = request.json or {}
    db = get_db()
    db.execute(
        'UPDATE projects SET brief_template = ?, brand_bible = ? WHERE id = ?',
        (data.get('brief_template', ''), data.get('brand_bible', ''), project_id)
    )
    db.commit()
    project = db.execute('SELECT * FROM projects WHERE id = ?', (project_id,)).fetchone()
    return jsonify(dict(project))

@app.route('/api/projects/<int:project_id>', methods=['DELETE'])
def delete_project(project_id):
    db = get_db()
    db.execute('DELETE FROM projects WHERE id = ?', (project_id,))
    db.commit()
    return jsonify({'success': True})

# Briefs
@app.route('/api/briefs', methods=['GET'])
def get_briefs():
    db = get_db()
    rows = db.execute('SELECT * FROM briefs ORDER BY created_at DESC').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/briefs', methods=['POST'])
def create_brief():
    data = request.json or {}
    video = data.get('video', {})
    project_id = data.get('project_id')
    analysis = data.get('analysis')

    if not video or not project_id:
        return jsonify({'error': 'Video and project_id required'}), 400

    db = get_db()
    project = db.execute('SELECT * FROM projects WHERE id = ?', (project_id,)).fetchone()
    if not project:
        return jsonify({'error': 'Project not found'}), 404

    project_dict = dict(project)
    brief_content = generate_brief(video, project_dict, analysis)

    cursor = db.execute(
        'INSERT INTO briefs (project_id, project_name, video_id, video_url, brief_content) VALUES (?, ?, ?, ?, ?)',
        (project_id, project_dict['name'], video.get('id', ''), video.get('tiktok_url', ''), brief_content)
    )
    db.commit()
    brief = db.execute('SELECT * FROM briefs WHERE id = ?', (cursor.lastrowid,)).fetchone()
    return jsonify(dict(brief)), 201

@app.route('/api/briefs/<int:brief_id>', methods=['GET'])
def get_brief(brief_id):
    db = get_db()
    brief = db.execute('SELECT * FROM briefs WHERE id = ?', (brief_id,)).fetchone()
    if not brief:
        return jsonify({'error': 'Brief not found'}), 404
    return jsonify(dict(brief))

@app.route('/api/briefs/<int:brief_id>', methods=['DELETE'])
def delete_brief(brief_id):
    db = get_db()
    db.execute('DELETE FROM briefs WHERE id = ?', (brief_id,))
    db.commit()
    return jsonify({'success': True})

# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    mode = 'DEMO (add APIFY_API_TOKEN to .env for live data)' if DEMO_MODE else 'LIVE'
    gemini = 'configured' if GOOGLE_AI_API_KEY else 'not configured (add GOOGLE_AI_API_KEY)'
    print(f'\nTikTok Trend Scraper')
    print(f'Mode: {mode}')
    print(f'Gemini: {gemini}')
    print(f'Running on: http://localhost:5050\n')
    app.run(debug=True, port=5050)
