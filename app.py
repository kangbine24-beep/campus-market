import os
import re
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, g
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from google import genai

# .env 파일에서 환경변수 불러오기
load_dotenv()

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'

DATABASE = 'campus_market.db'
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# 💡 최신 google.genai 클라이언트 초기화 (.env의 GEMINI_API_KEY 자동 연동)
client = genai.Client()


def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DATABASE) as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                name TEXT NOT NULL,
                student_id TEXT UNIQUE NOT NULL
            )
        ''')

        cursor = db.cursor()
        cursor.execute("PRAGMA table_info(users)")
        user_columns = [column[1] for column in cursor.fetchall()]
        if 'student_id' not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN student_id TEXT UNIQUE")

        db.execute('''
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                price INTEGER NOT NULL,
                category TEXT NOT NULL,
                condition TEXT NOT NULL,
                department TEXT,
                content TEXT NOT NULL,
                ai_summary TEXT,
                image_filename TEXT,
                status TEXT DEFAULT '판매중',
                user_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        cursor.execute("PRAGMA table_info(posts)")
        post_columns = [column[1] for column in cursor.fetchall()]
        if 'completed_at' not in post_columns:
            db.execute("ALTER TABLE posts ADD COLUMN completed_at TIMESTAMP")

        db.execute('''
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                is_read INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (post_id) REFERENCES posts (id),
                FOREIGN KEY (sender_id) REFERENCES users (id),
                FOREIGN KEY (receiver_id) REFERENCES users (id)
            )
        ''')

        cursor = db.cursor()
        cursor.execute("PRAGMA table_info(chats)")
        chat_columns = [column[1] for column in cursor.fetchall()]
        if 'is_read' not in chat_columns:
            db.execute("ALTER TABLE chats ADD COLUMN is_read INTEGER DEFAULT 0")

        db.execute('''
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (id),
                FOREIGN KEY (post_id) REFERENCES posts (id)
            )
        ''')
        db.commit()


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _fallback_summary(content):
    short_text = content.strip().replace('\n', ' ')[:30]
    return f"• 핵심 내용: {short_text}...\n• 상태 및 특징: 상세 설명 참조\n• 거래 방법: 채팅 협의"


# 💡 경량 모델(gemini-3.5-flash-lite) 우선 호출 및 모델별 1회 시도로 응답 시간 단축
def generate_smart_summary(content):
    if not content or not content.strip():
        return "• 핵심 내용: 등록된 내용이 없습니다.\n• 상태 및 특징: -\n• 거래 방법: -"

    if os.getenv("SKIP_AI_SUMMARY", "").strip() in ("1", "true", "yes"):
        return _fallback_summary(content)

    prompt = (
        "중고거래 글을 아래 3줄 형식으로만 짧게 요약해줘. 다른 말은 쓰지 마.\n"
        "- 핵심 내용: \n- 상태 및 특징: \n- 거래 방법: \n"
        f"내용: {content}"
    )

    api_key = os.getenv("GEMINI_API_KEY")
    local_client = genai.Client(api_key=api_key) if api_key else client

    models_to_try = ["gemini-3.5-flash-lite", "gemini-3.6-flash"]

    for model_name in models_to_try:
        try:
            response = local_client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            error_str = str(e)
            print(f"🔥 [{model_name}] 실패: {error_str}")
            if "RESOURCE_EXHAUSTED" in error_str or "per day" in error_str.lower():
                return _fallback_summary(content)

    return _fallback_summary(content)


@app.context_processor
def inject_unread_count():
    unread_count = 0
    if 'user_id' in session:
        user_id = session['user_id']
        db = get_db()
        res = db.execute(
            'SELECT COUNT(*) as cnt FROM chats WHERE receiver_id = ? AND is_read = 0',
            (user_id,)
        ).fetchone()
        if res:
            unread_count = res['cnt']
    return dict(unread_count=unread_count)


@app.route('/')
def index():
    db = get_db()
    db.execute("DELETE FROM posts WHERE status = '거래완료' AND completed_at <= DATETIME('now', '-14 days')")
    db.commit()

    keyword = request.args.get('keyword', '')
    department = request.args.get('department', '')
    category = request.args.get('category', '')
    status = request.args.get('status', '')
    max_price = request.args.get('max_price', '')

    query = 'SELECT posts.*, users.username FROM posts JOIN users ON posts.user_id = users.id WHERE 1=1'
    params = []

    if keyword:
        raw_tokens = re.split(r'[,+\s]+', keyword.strip())
        tokens = [t for t in raw_tokens if t]

        if tokens:
            keyword_clauses = []
            for token in tokens:
                keyword_clauses.append('(posts.title LIKE ? OR posts.content LIKE ?)')
                params.extend([f'%{token}%', f'%{token}%'])
            query += ' AND (' + ' OR '.join(keyword_clauses) + ')'

    if department:
        query += ' AND posts.department LIKE ?'
        params.append(f'%{department}%')
    if category and category != '전체 카테고리':
        query += ' AND posts.category = ?'
        params.append(category)
    if status and status != '전체 상태':
        query += ' AND posts.status = ?'
        params.append(status)
    if max_price:
        query += ' AND posts.price <= ?'
        params.append(int(max_price))

    query += ' ORDER BY posts.created_at DESC'
    posts = db.execute(query, params).fetchall()

    return render_template('index.html', posts=posts)


@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        name = request.form['name'].strip()
        student_id = request.form['student_id'].strip()

        if not student_id or len(student_id) != 7:
            error = '정확한 학번을 입력하세요.'
        else:
            hashed_password = generate_password_hash(password)
            db = get_db()
            try:
                db.execute(
                    'INSERT INTO users (username, password, name, student_id) VALUES (?, ?, ?, ?)',
                    (username, hashed_password, name, student_id)
                )
                db.commit()
                return redirect(url_for('login'))
            except sqlite3.IntegrityError as e:
                err_str = str(e).lower()
                if 'student_id' in err_str:
                    error = '이미 등록된 학번입니다.'
                else:
                    error = '이미 존재하는 아이디입니다.'

    return render_template('register.html', error=error)


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        login_id = request.form['username'].strip()
        password = request.form['password']
        db = get_db()

        user = db.execute(
            'SELECT * FROM users WHERE username = ? OR (student_id = ? AND length(student_id) = 7)',
            (login_id, login_id)
        ).fetchone()

        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('index'))
        else:
            error = '아이디 또는 학번, 혹은 비밀번호가 잘못되었습니다.'
    return render_template('login.html', error=error)


@app.route('/find-id', methods=['GET', 'POST'])
def find_id():
    masked_users = []
    error = None
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        student_id = request.form.get('student_id', '').strip()

        if not name and not student_id:
            error = '이름 또는 학번 중 하나를 입력해주세요.'
        elif student_id and len(student_id) != 7:
            error = '정확한 학번을 입력하세요.'
        else:
            db = get_db()
            query = 'SELECT * FROM users WHERE 1=1'
            params = []

            if name:
                query += ' AND name = ?'
                params.append(name)
            if student_id:
                query += ' AND student_id = ?'
                params.append(student_id)

            users = db.execute(query, params).fetchall()

            if not users:
                error = '일치하는 사용자 정보를 찾을 수 없습니다.'
            else:
                for user in users:
                    username = user['username']
                    length = len(username)
                    masked = (
                        username[0] + '*'
                        if length <= 2
                        else (
                            username[:2] + '*' * (length - 2)
                            if length <= 5
                            else username[:-3] + '***'
                        )
                    )
                    masked_users.append({
                        'id': user['id'],
                        'masked': masked,
                        'student_id': user['student_id']
                    })
    return render_template('find_id.html', masked_users=masked_users, error=error)


@app.route('/verify-id/<int:user_id>', methods=['GET', 'POST'])
def verify_id(user_id):
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        return "존재하지 않는 사용자입니다.", 404
    error = None
    full_username = None
    if request.method == 'POST':
        password = request.form['password']
        if check_password_hash(user['password'], password):
            full_username = user['username']
        else:
            error = '비밀번호가 일치하지 않습니다.'
    return render_template('verify_id.html', user=user, error=error, full_username=full_username)


@app.route('/find-password', methods=['GET', 'POST'])
def find_password():
    error = None
    success = None
    if request.method == 'POST':
        username = request.form['username'].strip()
        new_password = request.form['password']
        db = get_db()
        user = db.execute(
            'SELECT * FROM users WHERE username = ? OR (student_id = ? AND length(student_id) = 7)',
            (username, username)
        ).fetchone()
        if not user:
            error = '존재하지 않는 사용자입니다.'
        else:
            hashed_password = generate_password_hash(new_password)
            db.execute('UPDATE users SET password = ? WHERE id = ?', (hashed_password, user['id']))
            db.commit()
            success = '비밀번호가 성공적으로 변경되었습니다.'
    return render_template('find_password.html', error=error, success=success)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/create', methods=['GET', 'POST'])
def create():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if request.method == 'POST':
        title = request.form['title']
        price = request.form['price']
        category = request.form['category']
        condition = request.form['condition']
        department = request.form.get('department', '').strip()
        content = request.form['content']
        user_id = session['user_id']

        if category == '전공서적' and not department:
            return "전공서적 카테고리는 학과 입력이 필수입니다. 뒤로 가기 후 학과를 입력해주세요.", 400

        ai_summary = generate_smart_summary(content)

        image_filename = None
        file = request.files.get('image')
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            image_filename = filename

        db = get_db()
        db.execute(
            'INSERT INTO posts (title, price, category, condition, department, content, ai_summary, image_filename, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (title, price, category, condition, department, content, ai_summary, image_filename, user_id)
        )
        db.commit()
        return redirect(url_for('index'))
    return render_template('create.html')


@app.route('/posts/<int:post_id>')
def detail(post_id):
    db = get_db()
    post = db.execute(
        'SELECT posts.*, users.username FROM posts JOIN users ON posts.user_id = users.id WHERE posts.id = ?',
        (post_id,)
    ).fetchone()
    if not post:
        return "존재하지 않는 상품입니다.", 404

    days_left = None
    if post['status'] == '거래완료' and post['completed_at']:
        try:
            completed_time = datetime.strptime(post['completed_at'], '%Y-%m-%d %H:%M:%S')
            elapsed_days = (datetime.now() - completed_time).days
            days_left = max(0, 14 - elapsed_days)
        except Exception:
            days_left = 14

    is_favorite = False
    if 'user_id' in session:
        fav = db.execute(
            'SELECT * FROM favorites WHERE user_id = ? AND post_id = ?',
            (session['user_id'], post_id)
        ).fetchone()
        if fav:
            is_favorite = True

    return render_template('detail.html', post=post, days_left=days_left, is_favorite=is_favorite)


@app.route('/posts/<int:post_id>/favorite', methods=['POST'])
def toggle_favorite(post_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    db = get_db()

    fav = db.execute(
        'SELECT * FROM favorites WHERE user_id = ? AND post_id = ?',
        (user_id, post_id)
    ).fetchone()
    if fav:
        db.execute('DELETE FROM favorites WHERE user_id = ? AND post_id = ?', (user_id, post_id))
    else:
        db.execute('INSERT INTO favorites (user_id, post_id) VALUES (?, ?)', (user_id, post_id))

    db.commit()
    return redirect(url_for('detail', post_id=post_id))


@app.route('/mypage')
def mypage():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    db = get_db()

    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    my_posts = db.execute('SELECT * FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user_id,)).fetchall()

    favorite_posts = db.execute('''
        SELECT posts.*, users.username FROM posts 
        JOIN favorites ON posts.id = favorites.post_id 
        JOIN users ON posts.user_id = users.id 
        WHERE favorites.user_id = ? 
        ORDER BY posts.created_at DESC
    ''', (user_id,)).fetchall()

    return render_template('mypage.html', user=user, my_posts=my_posts, favorite_posts=favorite_posts)


@app.route('/chats')
def chat_list():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    user_id = session['user_id']
    db = get_db()

    chats_query = '''
        SELECT DISTINCT posts.id as post_id, posts.title, posts.price, posts.image_filename, posts.user_id as seller_id
        FROM posts
        JOIN chats ON posts.id = chats.post_id
        WHERE chats.sender_id = ? OR chats.receiver_id = ?
        ORDER BY chats.created_at DESC
    '''
    my_chats = db.execute(chats_query, (user_id, user_id)).fetchall()

    return render_template('chat_list.html', my_chats=my_chats)


@app.route('/api/chats')
def api_chats():
    if 'user_id' not in session:
        return {'status': 'fail'}, 401

    user_id = session['user_id']
    db = get_db()

    chats_query = '''
        SELECT DISTINCT posts.id as post_id, posts.title, posts.price, posts.image_filename, posts.user_id as seller_id
        FROM posts
        JOIN chats ON posts.id = chats.post_id
        WHERE chats.sender_id = ? OR chats.receiver_id = ?
        ORDER BY chats.created_at DESC
    '''
    my_chats = db.execute(chats_query, (user_id, user_id)).fetchall()

    chats_list = [{
        'post_id': c['post_id'],
        'title': c['title'],
        'price': c['price'],
        'image_filename': c['image_filename']
    } for c in my_chats]

    return {'chats': chats_list}


@app.route('/api/unread-count')
def api_unread_count():
    unread_count = 0
    if 'user_id' in session:
        user_id = session['user_id']
        db = get_db()
        res = db.execute(
            'SELECT COUNT(*) as cnt FROM chats WHERE receiver_id = ? AND is_read = 0',
            (user_id,)
        ).fetchone()
        if res:
            unread_count = res['cnt']
    return {'unread_count': unread_count}


@app.route('/posts/<int:post_id>/chat', methods=['GET', 'POST'])
def chat_room(post_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    post = db.execute(
        'SELECT posts.*, users.id as seller_id, users.username as seller_name FROM posts JOIN users ON posts.user_id = users.id WHERE posts.id = ?',
        (post_id,)
    ).fetchone()
    if not post:
        return "존재하지 않는 사용자/상품입니다.", 404

    current_user_id = session['user_id']
    seller_id = post['seller_id']

    db.execute(
        'UPDATE chats SET is_read = 1 WHERE post_id = ? AND receiver_id = ? AND is_read = 0',
        (post_id, current_user_id)
    )
    db.commit()

    if request.method == 'POST':
        data = request.get_json(silent=True)
        message = data.get('message') if data else request.form.get('message')

        if message and message.strip():
            if current_user_id == seller_id:
                last_msg = db.execute(
                    'SELECT sender_id FROM chats WHERE post_id = ? AND sender_id != ? ORDER BY created_at DESC LIMIT 1',
                    (post_id, seller_id)
                ).fetchone()
                receiver_id = last_msg['sender_id'] if last_msg else None
                if not receiver_id:
                    other_user = db.execute(
                        'SELECT id FROM users WHERE id != ? ORDER BY id DESC LIMIT 1',
                        (seller_id,)
                    ).fetchone()
                    receiver_id = other_user['id'] if other_user else seller_id
            else:
                receiver_id = seller_id

            if receiver_id:
                db.execute(
                    'INSERT INTO chats (post_id, sender_id, receiver_id, message, is_read) VALUES (?, ?, ?, ?, 0)',
                    (post_id, current_user_id, receiver_id, message.strip())
                )
                db.commit()

                if data:
                    return {
                        'status': 'success',
                        'message': message.strip(),
                        'username': session.get('username'),
                        'time': datetime.now().strftime('%H:%M')
                    }

        if data:
            return {'status': 'fail'}, 400
        return redirect(url_for('chat_room', post_id=post_id))

    messages = db.execute(
        'SELECT chats.*, sender.username as sender_username FROM chats JOIN users sender ON chats.sender_id = sender.id WHERE chats.post_id = ? ORDER BY chats.created_at ASC',
        (post_id,)
    ).fetchall()
    return render_template('chat.html', post=post, messages=messages, seller_id=seller_id)


@app.route('/posts/<int:post_id>/chat/messages')
def get_chat_messages(post_id):
    db = get_db()
    messages = db.execute(
        'SELECT chats.*, sender.username FROM chats JOIN users sender ON chats.sender_id = sender.id WHERE chats.post_id = ? ORDER BY chats.created_at ASC',
        (post_id,)
    ).fetchall()
    msg_list = []
    for m in messages:
        msg_list.append({
            'sender_id': m['sender_id'],
            'username': m['username'],
            'message': m['message'],
            'time': m['created_at']
        })
    return {'messages': msg_list}


@app.route('/posts/<int:post_id>/chat/send', methods=['POST'])
def send_chat_message(post_id):
    if 'user_id' not in session:
        return {'status': 'fail', 'reason': 'unauthorized'}, 401

    data = request.get_json(silent=True)
    message = data.get('message') if data else None

    if not message or not message.strip():
        return {'status': 'fail', 'reason': 'empty'}, 400

    db = get_db()
    post = db.execute('SELECT * FROM posts WHERE id = ?', (post_id,)).fetchone()
    if not post:
        return {'status': 'fail', 'reason': 'not_found'}, 404

    current_user_id = session['user_id']
    seller_id = post['user_id']

    if current_user_id == seller_id:
        last_msg = db.execute(
            'SELECT sender_id FROM chats WHERE post_id = ? AND sender_id != ? ORDER BY created_at DESC LIMIT 1',
            (post_id, seller_id)
        ).fetchone()
        receiver_id = last_msg['sender_id'] if last_msg else None
        if not receiver_id:
            other_user = db.execute(
                'SELECT id FROM users WHERE id != ? ORDER BY id DESC LIMIT 1',
                (seller_id,)
            ).fetchone()
            receiver_id = other_user['id'] if other_user else seller_id
    else:
        receiver_id = seller_id

    if receiver_id:
        db.execute(
            'INSERT INTO chats (post_id, sender_id, receiver_id, message, is_read) VALUES (?, ?, ?, ?, 0)',
            (post_id, current_user_id, receiver_id, message.strip())
        )
        db.commit()
        return {
            'status': 'success',
            'message': message.strip(),
            'username': session.get('username')
        }

    return {'status': 'fail'}, 400


@app.route('/posts/<int:post_id>/status', methods=['POST'])
def update_status(post_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    post = db.execute('SELECT * FROM posts WHERE id = ?', (post_id,)).fetchone()
    if not post or post['user_id'] != session['user_id']:
        return "권한이 없습니다.", 403

    if post['status'] == '판매중':
        new_status = '거래완료'
        db.execute("UPDATE posts SET status = ?, completed_at = DATETIME('now') WHERE id = ?", (new_status, post_id))
    else:
        new_status = '판매중'
        db.execute("UPDATE posts SET status = ?, completed_at = NULL WHERE id = ?", (new_status, post_id))

    db.commit()
    return redirect(url_for('detail', post_id=post_id))


@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    post = db.execute('SELECT * FROM posts WHERE id = ?', (post_id,)).fetchone()
    if post and post['user_id'] == session['user_id']:
        db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
        db.commit()
    return redirect(url_for('index'))


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)