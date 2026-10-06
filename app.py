import hashlib
import os
import sqlite3
import uuid
from datetime import datetime
from io import BytesIO

import qrcode
from flask import Flask, flash, redirect, render_template, request, send_file, session, url_for
from openpyxl import Workbook
from werkzeug.utils import secure_filename


# Workaround: ReportLab on some Python/OpenSSL builds calls md5(..., usedforsecurity=False)
# which isn't supported by older OpenSSL bindings. Patch hashlib.md5 and any
# openssl_md5 entry to ignore that kwarg before importing reportlab so
# ReportLab picks up a compatible function.
_original_md5 = hashlib.md5
try:
    import _hashlib as _lib_hash
except Exception:
    _lib_hash = None

def _compat_md5(*args, **kwargs):
    kwargs.pop('usedforsecurity', None)
    return _original_md5(*args, **kwargs)

def _compat_openssl_md5(*args, **kwargs):
    kwargs.pop('usedforsecurity', None)
    return _original_md5(*args, **kwargs)

hashlib.md5 = _compat_md5
# ensure attribute exists for modules that call openssl_md5 directly
setattr(hashlib, 'openssl_md5', _compat_openssl_md5)
if _lib_hash and hasattr(_lib_hash, 'openssl_md5'):
    try:
        _lib_hash.openssl_md5 = _compat_openssl_md5
    except Exception:
        pass
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
try:
    import reportlab.pdfdoc as _pdfdoc
    def _safe_md5(*args, **kwargs):
        kwargs.pop('usedforsecurity', None)
        return hashlib.md5(*args, **kwargs)
    _pdfdoc.md5 = _safe_md5
    if hasattr(_pdfdoc, 'openssl_md5'):
        _pdfdoc.openssl_md5 = _safe_md5
except Exception:
    pass
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'database.db')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
QR_FOLDER = os.path.join(BASE_DIR, 'static', 'qrcodes')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(QR_FOLDER, exist_ok=True)

app = Flask(__name__)
app.secret_key = 'super-secret-lab-key-2026'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

def build_student_search_filter(search_term):
    term = (search_term or '').strip()
    if not term:
        return '', [], ''
    pattern = f'%{term}%'
    return (
        ' WHERE full_name LIKE ? OR student_number LIKE ? OR level LIKE ? OR department LIKE ? ',
        [pattern, pattern, pattern, pattern],
        term,
    )

@app.context_processor
def inject_user():
    user = session.get('user')
    return {'current_user': user}


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'supervisor'
        )
        '''
    )
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            student_number TEXT NOT NULL UNIQUE,
            level TEXT NOT NULL,
            department TEXT,
            phone TEXT,
            photo TEXT DEFAULT 'default_student.png',
            qr_code TEXT UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        '''
    )
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            supervisor_id INTEGER NOT NULL,
            academic_year TEXT NOT NULL,
            score INTEGER NOT NULL,
            rating TEXT NOT NULL,
            notes TEXT,
            evaluation_date TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(student_id) REFERENCES students(id),
            FOREIGN KEY(supervisor_id) REFERENCES users(id)
        )
        '''
    )
    conn.execute(
        '''
        CREATE TABLE IF NOT EXISTS damage_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            supervisor_id INTEGER NOT NULL,
            damage_type TEXT NOT NULL,
            description TEXT,
            damage_photo TEXT,
            report_date TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(student_id) REFERENCES students(id),
            FOREIGN KEY(supervisor_id) REFERENCES users(id)
        )
        '''
    )
    conn.commit()

    admin = conn.execute('SELECT id FROM users WHERE username = ?', ('admin',)).fetchone()
    if admin is None:
        conn.execute(
            'INSERT INTO users (username, password, full_name, role) VALUES (?, ?, ?, ?)',
            ('asma', 'asma9090', 'Supervisor', 'supervisor')
        )
        conn.commit()
    conn.close()


def login_required(view_func):
    def wrapped(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return view_func(*args, **kwargs)

    wrapped.__name__ = view_func.__name__
    return wrapped


def rating_from_score(score):
    if score >= 90:
        return 'ممتاز'
    if score >= 70:
        return 'جيد'
    if score >= 50:
        return 'متوسط'
    return 'ضعيف'


def save_upload(file, folder):
    if file is None or file.filename == '':
        return ''
    filename = secure_filename(file.filename)
    unique_name = f"{uuid.uuid4().hex}_{filename}"
    file_path = os.path.join(folder, unique_name)
    file.save(file_path)
    return unique_name

def generate_qr_image(student_id, qr_code, base_url=None):
    public_base_url = base_url or os.getenv('PUBLIC_BASE_URL') or 'https://projectran.onrender.com'
    # توجيه الممسوح مباشرة إلى صفحة تفاصيل الطالب id
    data_to_encode = f'{public_base_url.rstrip("/")}/student/{student_id}?public=1'

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data_to_encode)
    qr.make(fit=True)

    img = qr.make_image(fill_color='black', back_color='white')
    file_name = f'student_{student_id}_qr.png'
    img.save(os.path.join(QR_FOLDER, file_name))
    return file_name

@app.route('/regenerate-all-qrs')
@login_required
def regenerate_all_qrs():
    conn = get_db_connection()
    students = conn.execute('SELECT id, qr_code FROM students').fetchall()
    conn.close()

    count = 0
    for student in students:
        if student['qr_code']:
            generate_qr_image(student['id'], student['qr_code'])
            count += 1

    flash(f'تمت إعادة توليد رموز QR لـ {count} طالب بنجاح!', 'success')
    return redirect(url_for('dashboard'))

def get_student_summary(student_id):
    conn = get_db_connection()
    student = conn.execute('SELECT * FROM students WHERE id = ?', (student_id,)).fetchone()
    latest_eval = conn.execute(
        'SELECT score, rating, notes, evaluation_date FROM evaluations WHERE student_id = ? ORDER BY id DESC LIMIT 1',
        (student_id,)
    ).fetchone()
    damage_count = conn.execute('SELECT COUNT(*) as total FROM damage_reports WHERE student_id = ?', (student_id,)).fetchone()
    conn.close()

    return student, latest_eval, damage_count['total'] if damage_count else 0

def ensure_arabic_font():
    if 'ArabicFont' in pdfmetrics.getRegisteredFontNames():
        return True

    candidates = [
        r'C:\Windows\Fonts\arial.ttf',
        r'C:\Windows\Fonts\tahoma.ttf',
        r'C:\Windows\Fonts\times.ttf',
        r'/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]

    for p in candidates:
        if os.path.exists(p):
            try:
                pdfmetrics.registerFont(TTFont('ArabicFont', p))
                return True
            except Exception:
                continue
    return False

def shape_text_for_pdf(text):
    """تشكيل النص العربي وعكس اتجاهه ليتم طباعته بشكل صحيح في الـ PDF"""
    if not text:
        return ''
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        
        # ربط الحروف العربية ببعضها البعض
        reshaped_text = arabic_reshaper.reshape(str(text))
        # ضبط اتجاه الكتابة من اليمين إلى اليسار
        bidi_text = get_display(reshaped_text)
        return bidi_text
    except Exception as e:
        print("Arabic shaping error:", e)
        return str(text)


@app.route('/')
def index():
    if 'user' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        conn = get_db_connection()
        user = conn.execute(
            'SELECT * FROM users WHERE username = ? AND password = ?',
            (username, password)
        ).fetchone()
        conn.close()

        if user:
            session['user'] = {
                'id': user['id'],
                'username': user['username'],
                'full_name': user['full_name'],
                'role': user['role']
            }
            flash('تم تسجيل الدخول بنجاح', 'success')
            return redirect(url_for('dashboard'))

        flash('اسم المستخدم أو كلمة المرور غير صحيحة', 'danger')
        return render_template('login.html')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    conn = get_db_connection()
    students_count = conn.execute('SELECT COUNT(*) AS total FROM students').fetchone()['total']
    evaluations_count = conn.execute('SELECT COUNT(*) AS total FROM evaluations').fetchone()['total']
    damage_count = conn.execute('SELECT COUNT(*) AS total FROM damage_reports').fetchone()['total']
    recent_students = conn.execute(
        'SELECT s.*, (SELECT score FROM evaluations WHERE student_id = s.id ORDER BY id DESC LIMIT 1) AS score '
        'FROM students s ORDER BY s.id DESC LIMIT 5'
    ).fetchall()
    conn.close()

    return render_template(
        'dashboard.html',
        students_count=students_count,
        evaluations_count=evaluations_count,
        damage_count=damage_count,
        recent_students=recent_students,
    )


@app.route('/students')
@login_required
def students():
    search_term = request.args.get('q', '').strip()
    conn = get_db_connection()
    where_sql, params, _ = build_student_search_filter(search_term)
    query = 'SELECT * FROM students' + where_sql + ' ORDER BY id DESC'
    students_list = conn.execute(query, params).fetchall()
    data = []
    for student in students_list:
        latest = conn.execute(
            'SELECT score, rating FROM evaluations WHERE student_id = ? ORDER BY id DESC LIMIT 1',
            (student['id'],)
        ).fetchone()
        student_data = dict(student)
        student_data['latest_score'] = latest['score'] if latest else 0
        student_data['latest_rating'] = latest['rating'] if latest else 'لا يوجد تقييم'
        data.append(student_data)
    conn.close()
    return render_template('students.html', students=data, search_term=search_term)


@app.route('/students/new', methods=['GET', 'POST'])
@login_required
def new_student():
    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        student_number = request.form.get('student_number', '').strip()
        level = request.form.get('level', '').strip()
        department = request.form.get('department', '').strip()
        phone = request.form.get('phone', '').strip()
        photo = request.files.get('photo')

        if not full_name or not student_number or not level:
            flash('يرجى تعبئة الاسم ورقم الطالب والمستوى', 'danger')
            return render_template('add_student.html')

        conn = get_db_connection()
        existing = conn.execute('SELECT id FROM students WHERE student_number = ?', (student_number,)).fetchone()
        if existing:
            conn.close()
            flash('رقم الطالب موجود بالفعل', 'danger')
            return render_template('add_student.html')

        photo_name = 'default_student.png'
        if photo and photo.filename:
            photo_name = save_upload(photo, UPLOAD_FOLDER)

        qr_code = f'LAB-{uuid.uuid4().hex[:10].upper()}'
        cursor = conn.execute(
            '''
            INSERT INTO students (full_name, student_number, level, department, phone, photo, qr_code)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (full_name, student_number, level, department, phone, photo_name, qr_code)
        )
        conn.commit()
        student_id = cursor.lastrowid
        generate_qr_image(student_id, qr_code)
        conn.close()

        flash('تمت إضافة الطالب بنجاح', 'success')
        return redirect(url_for('student_detail', student_id=student_id))

    return render_template('add_student.html')


@app.route('/student/<int:student_id>')
def student_detail(student_id):
    if 'user' not in session and request.args.get('public') != '1':
        return redirect(url_for('login'))

    student, latest_eval, damage_count = get_student_summary(student_id)
    if not student:
        flash('الطالب غير موجود', 'danger')
        return redirect(url_for('students'))

    conn = get_db_connection()
    evaluations = conn.execute(
        '''
        SELECT e.*, u.full_name AS supervisor_name
        FROM evaluations e
        INNER JOIN users u ON u.id = e.supervisor_id
        WHERE e.student_id = ?
        ORDER BY e.id DESC
        ''',
        (student_id,)
    ).fetchall()
    damage_reports = conn.execute(
        '''
        SELECT d.*, u.full_name AS supervisor_name
        FROM damage_reports d
        INNER JOIN users u ON u.id = d.supervisor_id
        WHERE d.student_id = ?
        ORDER BY d.id DESC
        ''',
        (student_id,)
    ).fetchall()
    conn.close()

    return render_template(
        'student_detail.html',
        student=student,
        latest_eval=latest_eval,
        evaluations=evaluations,
        damage_reports=damage_reports,
        damage_count=damage_count,
        is_public_view=request.args.get('public') == '1',
    )


@app.route('/student/<int:student_id>/evaluate', methods=['POST'])
@login_required
def evaluate_student(student_id):
    score = int(request.form.get('score', 0))
    notes = request.form.get('notes', '').strip()
    academic_year = request.form.get('academic_year', '2026-2027').strip()

    if score < 0 or score > 100:
        flash('درجة التقييم يجب أن تكون بين 0 و 100', 'danger')
        return redirect(url_for('student_detail', student_id=student_id))

    conn = get_db_connection()
    user = session['user']
    rating = rating_from_score(score)
    conn.execute(
        '''
        INSERT INTO evaluations (student_id, supervisor_id, academic_year, score, rating, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        ''',
        (student_id, user['id'], academic_year, score, rating, notes)
    )
    conn.commit()
    conn.close()

    flash('تم حفظ تقييم الطالب بنجاح', 'success')
    return redirect(url_for('student_detail', student_id=student_id))


@app.route('/student/<int:student_id>/delete', methods=['POST'])
@login_required
def delete_student(student_id):
    conn = get_db_connection()
    student = conn.execute('SELECT * FROM students WHERE id = ?', (student_id,)).fetchone()
    if not student:
        conn.close()
        flash('الطالب غير موجود', 'danger')
        return redirect(url_for('students'))

    damage_photos = conn.execute('SELECT damage_photo FROM damage_reports WHERE student_id = ?', (student_id,)).fetchall()
    conn.execute('DELETE FROM evaluations WHERE student_id = ?', (student_id,))
    conn.execute('DELETE FROM damage_reports WHERE student_id = ?', (student_id,))
    conn.execute('DELETE FROM students WHERE id = ?', (student_id,))
    conn.commit()
    conn.close()

    try:
        if student['photo'] and student['photo'] != 'default_student.png':
            photo_path = os.path.join(UPLOAD_FOLDER, student['photo'])
            if os.path.exists(photo_path):
                os.remove(photo_path)
    except Exception:
        pass

    try:
        qr_path = os.path.join(QR_FOLDER, f"student_{student_id}_qr.png")
        if os.path.exists(qr_path):
            os.remove(qr_path)
    except Exception:
        pass

    for dp in damage_photos:
        try:
            if dp and dp[0]:
                dp_path = os.path.join(UPLOAD_FOLDER, dp[0])
                if os.path.exists(dp_path):
                    os.remove(dp_path)
        except Exception:
            pass

    flash('تم حذف بيانات الطالب والتقييمات والملفات المرتبطة بنجاح', 'success')
    return redirect(url_for('students'))


@app.route('/student/<int:student_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_student(student_id):
    conn = get_db_connection()
    student = conn.execute('SELECT * FROM students WHERE id = ?', (student_id,)).fetchone()
    if not student:
        conn.close()
        flash('الطالب غير موجود', 'danger')
        return redirect(url_for('students'))

    if request.method == 'POST':
        full_name = request.form.get('full_name', '').strip()
        student_number = request.form.get('student_number', '').strip()
        level = request.form.get('level', '').strip()
        department = request.form.get('department', '').strip()
        phone = request.form.get('phone', '').strip()
        photo = request.files.get('photo')

        if not full_name or not student_number or not level:
            flash('يرجى تعبئة الاسم ورقم الطالب والمستوى', 'danger')
            conn.close()
            return render_template('edit_student.html', student=student)

        existing = conn.execute('SELECT id FROM students WHERE student_number = ? AND id != ?', (student_number, student_id)).fetchone()
        if existing:
            conn.close()
            flash('رقم الطالب مستخدم من قبل طالب آخر', 'danger')
            return render_template('edit_student.html', student=student)

        photo_name = student['photo']
        if photo and photo.filename:
            try:
                if student['photo'] and student['photo'] != 'default_student.png':
                    old_path = os.path.join(UPLOAD_FOLDER, student['photo'])
                    if os.path.exists(old_path):
                        os.remove(old_path)
            except Exception:
                pass
            photo_name = save_upload(photo, UPLOAD_FOLDER)

        conn.execute(
            '''
            UPDATE students SET full_name = ?, student_number = ?, level = ?, department = ?, phone = ?, photo = ? WHERE id = ?
            ''',
            (full_name, student_number, level, department, phone, photo_name, student_id)
        )
        conn.commit()
        conn.close()

        flash('تم تحديث بيانات الطالب بنجاح', 'success')
        return redirect(url_for('student_detail', student_id=student_id))

    conn.close()
    return render_template('edit_student.html', student=student)


@app.route('/student/<int:student_id>/damage', methods=['POST'])
@login_required
def report_damage(student_id):
    damage_type = request.form.get('damage_type', '').strip()
    description = request.form.get('description', '').strip()
    photo = request.files.get('damage_photo')

    if not damage_type:
        flash('يرجى تحديد نوع الضرر', 'danger')
        return redirect(url_for('student_detail', student_id=student_id))

    damage_photo_name = ''
    if photo and photo.filename:
        damage_photo_name = save_upload(photo, UPLOAD_FOLDER)

    conn = get_db_connection()
    user = session['user']
    conn.execute(
        '''
        INSERT INTO damage_reports (student_id, supervisor_id, damage_type, description, damage_photo)
        VALUES (?, ?, ?, ?, ?)
        ''',
        (student_id, user['id'], damage_type, description, damage_photo_name)
    )
    conn.commit()
    conn.close()

    flash('تم تسجيل الضرر بنجاح', 'success')
    return redirect(url_for('student_detail', student_id=student_id))


@app.route('/reports')
@login_required
def reports():
    search_term = request.args.get('q', '').strip()
    conn = get_db_connection()
    where_sql, params, _ = build_student_search_filter(search_term)
    query = '''
        SELECT s.id, s.full_name, s.level, s.department,
               (SELECT score FROM evaluations WHERE student_id = s.id ORDER BY id DESC LIMIT 1) AS score,
               (SELECT rating FROM evaluations WHERE student_id = s.id ORDER BY id DESC LIMIT 1) AS rating,
               (SELECT COUNT(*) FROM damage_reports WHERE student_id = s.id) AS damage_count
        FROM students s
    ''' + where_sql + '''
        ORDER BY COALESCE((SELECT score FROM evaluations WHERE student_id = s.id ORDER BY id DESC LIMIT 1), 0) DESC
    '''
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return render_template('reports.html', rows=rows, search_term=search_term)


@app.route('/scan', methods=['GET'])
@app.route('/scan/<qr_code>')
def scan_qr(qr_code=None):
    raw_code = qr_code or request.args.get('code', '').strip()
    if not raw_code:
        flash('رمز QR غير موجود', 'danger')
        return redirect(url_for('dashboard'))

    raw_code = str(raw_code).strip().strip('\"\'' )

    try:
        from urllib.parse import parse_qs, urlparse

        if '://' in raw_code:
            parsed = urlparse(raw_code)
            path_parts = [segment for segment in parsed.path.split('/') if segment]

            if 'scan' in path_parts:
                raw_code = path_parts[-1]
            elif 'student' in path_parts and len(path_parts) >= 2:
                student_id = path_parts[path_parts.index('student') + 1]
                if student_id.isdigit():
                    student = get_db_connection().execute(
                        'SELECT * FROM students WHERE id = ?', (int(student_id),)
                    ).fetchone()
                    if student:
                        return redirect(url_for('student_detail', student_id=student['id'], public='1'))
                    raw_code = student_id
            elif parsed.query:
                params = parse_qs(parsed.query)
                if 'code' in params and params['code']:
                    raw_code = params['code'][0]
        elif '/scan/' in raw_code:
            raw_code = raw_code.rstrip('/').split('/scan/')[-1]
        elif '/student/' in raw_code:
            suffix = raw_code.rstrip('/').split('/student/')[-1]
            if suffix.isdigit():
                student = get_db_connection().execute(
                    'SELECT * FROM students WHERE id = ?', (int(suffix),)
                ).fetchone()
                if student:
                    return redirect(url_for('student_detail', student_id=student['id'], public='1'))
    except Exception:
        pass

    raw_code = raw_code.strip()
    if raw_code.isdigit():
        student = get_db_connection().execute(
            'SELECT * FROM students WHERE id = ?', (int(raw_code),)
        ).fetchone()
        if student:
            return redirect(url_for('student_detail', student_id=student['id'], public='1'))

    conn = get_db_connection()
    student = conn.execute(
        'SELECT * FROM students WHERE qr_code = ?', (raw_code,)
    ).fetchone()
    conn.close()
    if student:
        return redirect(url_for('student_detail', student_id=student['id'], public='1'))

    flash('رمز QR غير موجود', 'danger')
    return redirect(url_for('dashboard'))


@app.route('/scanner')
@login_required
def scanner():
    return render_template('scanner.html')


@app.route('/export/pdf')
@login_required
def export_pdf():
    search_term = request.args.get('q', '').strip()
    conn = get_db_connection()
    where_sql, params, _ = build_student_search_filter(search_term)
    query = '''
        SELECT s.full_name, s.level, s.department,
               (SELECT score FROM evaluations WHERE student_id = s.id ORDER BY id DESC LIMIT 1) AS score,
               (SELECT rating FROM evaluations WHERE student_id = s.id ORDER BY id DESC LIMIT 1) AS rating,
               (SELECT COUNT(*) FROM damage_reports WHERE student_id = s.id) AS damage_count
        FROM students s
    ''' + where_sql + ' ORDER BY s.id DESC'
    rows = conn.execute(query, params).fetchall()
    conn.close()

    have_font = ensure_arabic_font()
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left_margin = 30
    top_margin = height - 50
    table_left = 30
    table_top = top_margin - 20
    col_widths = [170, 70, 110, 70, 70, 70]
    row_height = 22
    title = shape_text_for_pdf('تقرير الطلاب')
    if have_font:
        c.setFont('ArabicFont', 18)
        c.setFillColor(colors.HexColor('#4B0F1A'))
        c.drawRightString(width - 35, top_margin, title)
    else:
        c.setFont('Helvetica-Bold', 18)
        c.setFillColor(colors.HexColor('#4B0F1A'))
        c.drawString(40, top_margin, title)

    headers = ['اسم الطالب', 'المستوى', 'القسم', 'التقييم', 'النقاط', 'الحوادث']
    x = table_left
    y = table_top
    c.setFillColor(colors.HexColor('#EAEAEA'))
    c.rect(table_left, y - row_height, sum(col_widths), row_height, fill=1, stroke=1)
    c.setFillColor(colors.black)
    if have_font:
        c.setFont('ArabicFont', 10)
    else:
        c.setFont('Helvetica-Bold', 10)

    for index, header in enumerate(headers):
        cell_x = x + sum(col_widths[:index])
        value = shape_text_for_pdf(header)
        c.drawRightString(cell_x + col_widths[index] - 8, y - 15, value)

    c.setFillColor(colors.black)
    c.setStrokeColor(colors.grey)
    y -= row_height
    for row in rows:
        if y < 60:
            c.showPage()
            y = height - 70
            x = table_left
            c.setFillColor(colors.HexColor('#EAEAEA'))
            c.rect(table_left, y - row_height, sum(col_widths), row_height, fill=1, stroke=1)
            c.setFillColor(colors.black)
            if have_font:
                c.setFont('ArabicFont', 10)
            else:
                c.setFont('Helvetica-Bold', 10)
            for index, header in enumerate(headers):
                cell_x = x + sum(col_widths[:index])
                value = shape_text_for_pdf(header)
                c.drawRightString(cell_x + col_widths[index] - 8, y - 15, value)
            c.setFillColor(colors.black)
            c.setStrokeColor(colors.grey)
            y -= row_height

        c.rect(table_left, y - row_height, sum(col_widths), row_height, fill=0, stroke=1)
        for idx, col_width in enumerate(col_widths):
            cell_x = table_left + sum(col_widths[:idx])
            c.line(cell_x, y - row_height, cell_x, y)

        values = [
            row['full_name'] or '',
            row['level'] or '',
            row['department'] or '',
            row['rating'] or 'لا يوجد',
            str(row['score'] or 0),
            str(row['damage_count'] or 0),
        ]
        if have_font:
            c.setFont('ArabicFont', 9)
        else:
            c.setFont('Helvetica', 9)
        for index, value in enumerate(values):
            cell_x = table_left + sum(col_widths[:index])
            cell_value = shape_text_for_pdf(str(value))
            c.drawRightString(cell_x + col_widths[index] - 8, y - 15, cell_value)
        y -= row_height

    c.save()
    buffer.seek(0)
    return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name='students_report.pdf')


@app.route('/export/excel')
@login_required
def export_excel():
    search_term = request.args.get('q', '').strip()
    conn = get_db_connection()
    where_sql, params, _ = build_student_search_filter(search_term)
    query = '''
        SELECT s.full_name, s.level, s.department,
               (SELECT score FROM evaluations WHERE student_id = s.id ORDER BY id DESC LIMIT 1) AS score,
               (SELECT rating FROM evaluations WHERE student_id = s.id ORDER BY id DESC LIMIT 1) AS rating,
               (SELECT COUNT(*) FROM damage_reports WHERE student_id = s.id) AS damage_count
        FROM students s
    ''' + where_sql + ' ORDER BY s.id DESC'
    rows = conn.execute(query, params).fetchall()
    conn.close()

    wb = Workbook()
    ws = wb.active
    ws.title = 'تقرير الطلاب'

    headers = ['اسم الطالب', 'المستوى', 'القسم', 'التقييم', 'النقاط', 'الحوادث']
    ws.append(headers)

    for r in rows:
        ws.append([
            r['full_name'] or '',
            r['level'] or '',
            r['department'] or '',
            r['rating'] or 'لا يوجد',
            r['score'] or 0,
            r['damage_count'] or 0,
        ])

    # Adjust column widths
    column_widths = [30, 12, 20, 12, 10, 10]
    for i, width in enumerate(column_widths, start=1):
        col_letter = chr(64 + i)
        try:
            ws.column_dimensions[col_letter].width = width
        except Exception:
            pass

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True,
                     download_name='students_report.xlsx')


@app.route('/export/qr-bundle')
@login_required
def export_qr_bundle():
    level = request.args.get('level', '').strip()
    conn = get_db_connection()
    if level:
        students = conn.execute('SELECT * FROM students WHERE level = ? ORDER BY id DESC', (level,)).fetchall()
    else:
        students = conn.execute('SELECT * FROM students ORDER BY id DESC').fetchall()
    conn.close()

    have_font = ensure_arabic_font()
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    header_height = 80
    y_start = height - header_height - 20
    title_text = f'قائمة رموز QR للطلاب {"- مستوى " + level if level else ""}'
    title_text_shaped = shape_text_for_pdf(title_text)
    if have_font:
        c.setFont('ArabicFont', 14)
    else:
        c.setFont('Helvetica-Bold', 12)
    c.setFillColor(colors.HexColor('#4B0F1A'))
    c.drawString(40, height - 30, title_text_shaped)

    left_margin = 40
    qr_size = 140
    gap_x = 30
    gap_y = 90
    per_row = 3
    x_positions = [left_margin + i * (qr_size + gap_x) for i in range(per_row)]
    y = y_start
    col = 0

    for student in students:
        if y < 140:
            c.showPage()
            if have_font:
                c.setFont('ArabicFont', 14)
            else:
                c.setFont('Helvetica-Bold', 12)
            c.setFillColor(colors.HexColor('#4B0F1A'))
            c.drawString(40, height - 30, title_text_shaped)
            y = y_start

        x = x_positions[col]
        qr_path = os.path.join(QR_FOLDER, f'student_{student["id"]}_qr.png')
        if not os.path.exists(qr_path):
            generate_qr_image(student['id'], student['qr_code'], base_url=os.getenv('PUBLIC_BASE_URL'))

        try:
            c.drawImage(qr_path, x, y - qr_size, width=qr_size, height=qr_size)
        except Exception:
            pass

        name = shape_text_for_pdf((student['full_name'] or '') if student['full_name'] is not None else '')
        if have_font:
            c.setFont('ArabicFont', 12)
            c.drawRightString(x + qr_size, y - qr_size - 16, name)
        else:
            c.setFont('Helvetica-Bold', 10)
            c.drawString(x, y - qr_size - 16, name)

        if have_font:
            c.setFont('ArabicFont', 9)
            c.drawRightString(x + qr_size, y - qr_size - 30, (student['student_number'] or '') if student['student_number'] is not None else '')
        else:
            c.setFont('Helvetica', 8)
            c.drawString(x, y - qr_size - 30, (student['student_number'] or '') if student['student_number'] is not None else '')

        col += 1
        if col >= per_row:
            col = 0
            y -= (qr_size + gap_y)

    c.save()
    buffer.seek(0)
    return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name='student_qr_bundle.pdf')


@app.route('/qr-print')
@login_required
def qr_print_page():
    level = request.args.get('level', '').strip()
    conn = get_db_connection()
    if level:
        students_list = conn.execute('SELECT * FROM students WHERE level = ? ORDER BY id DESC', (level,)).fetchall()
    else:
        students_list = conn.execute('SELECT * FROM students ORDER BY id DESC').fetchall()
    conn.close()
    return render_template('qr_print.html', students=students_list, selected_level=level)


@app.route('/student/<int:student_id>/qr.png')
def student_qr_code_image(student_id):
    conn = get_db_connection()
    student = conn.execute('SELECT qr_code FROM students WHERE id = ?', (student_id,)).fetchone()
    conn.close()
    if not student or not student['qr_code']:
        return '', 404

    qr_path = os.path.join(QR_FOLDER, f'student_{student_id}_qr.png')
    if not os.path.exists(qr_path):
        generate_qr_image(student_id, student['qr_code'])

    if not os.path.exists(qr_path):
        return '', 404

    return send_file(qr_path, mimetype='image/png')


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)