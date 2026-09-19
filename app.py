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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'database.db')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
QR_FOLDER = os.path.join(BASE_DIR, 'static', 'qrcodes')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(QR_FOLDER, exist_ok=True)

app = Flask(__name__)
app.secret_key = 'super-secret-lab-key-2026'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER


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

def generate_qr_image(student_id, qr_code):
  # استخدام نطاق موقعك الفعلي على Render مباشرة لحل مشكلة البروكسي
  base_url = 'https://projectran.onrender.com'
  data_to_encode = f'{base_url}/scan/{qr_code}'

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
    """Register a TTF font that supports Arabic if available on the system."""
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
    """Shape Arabic text for proper display using arabic_reshaper and python-bidi when available.
    Falls back to the original text if packages are missing.
    """
    if not text:
        return ''
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        reshaped = arabic_reshaper.reshape(text)
        bidi_text = get_display(reshaped)
        return bidi_text
    except Exception:
        return text


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
    conn = get_db_connection()
    students_list = conn.execute('SELECT * FROM students ORDER BY id DESC').fetchall()
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
    return render_template('students.html', students=data)


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
@login_required
def student_detail(student_id):
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

    # collect damage photos to remove
    damage_photos = conn.execute('SELECT damage_photo FROM damage_reports WHERE student_id = ?', (student_id,)).fetchall()

    # delete related evaluations and damage reports
    conn.execute('DELETE FROM evaluations WHERE student_id = ?', (student_id,))
    conn.execute('DELETE FROM damage_reports WHERE student_id = ?', (student_id,))

    # delete student record
    conn.execute('DELETE FROM students WHERE id = ?', (student_id,))
    conn.commit()
    conn.close()

    # remove files: student photo (if uploaded) and QR image
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

    # remove any damage photos
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

        # check unique student_number (allow same for this student)
        existing = conn.execute('SELECT id FROM students WHERE student_number = ? AND id != ?', (student_number, student_id)).fetchone()
        if existing:
            conn.close()
            flash('رقم الطالب مستخدم من قبل طالب آخر', 'danger')
            return render_template('edit_student.html', student=student)

        # handle photo upload
        photo_name = student['photo']
        if photo and photo.filename:
            # remove old photo if not default
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
    conn = get_db_connection()
    rows = conn.execute(
        '''
        SELECT s.id, s.full_name, s.level, s.department,
               (SELECT score FROM evaluations WHERE student_id = s.id ORDER BY id DESC LIMIT 1) AS score,
               (SELECT rating FROM evaluations WHERE student_id = s.id ORDER BY id DESC LIMIT 1) AS rating,
               (SELECT COUNT(*) FROM damage_reports WHERE student_id = s.id) AS damage_count
        FROM students s
        ORDER BY COALESCE((SELECT score FROM evaluations WHERE student_id = s.id ORDER BY id DESC LIMIT 1), 0) DESC
        '''
    ).fetchall()
    conn.close()
    return render_template('reports.html', rows=rows)



# احذفي @login_required من فوق هذه الدالة لكي يتمكن الهاتف من قراءة الرابط
@app.route('/scan/<qr_code>')
def scan_qr(qr_code):
  conn = get_db_connection()
  student = conn.execute(
      'SELECT * FROM students WHERE qr_code = ?', (qr_code,)
  ).fetchone()
  conn.close()
  if student:
    return redirect(url_for('student_detail', student_id=student['id']))
  flash('رمز QR غير موجود', 'danger')
  return redirect(url_for('dashboard'))

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


@app.route('/export/excel')
@login_required
def export_excel():
    conn = get_db_connection()
    students_list = conn.execute('SELECT * FROM students ORDER BY id DESC').fetchall()
    conn.close()

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Students Reports'
    sheet.append(['اسم الطالب', 'رقم الطالب', 'المستوى', 'القسم', 'الهاتف', 'تاريخ الإضافة'])

    for student in students_list:
        sheet.append([
            student['full_name'],
            student['student_number'],
            student['level'],
            student['department'],
            student['phone'],
            student['created_at'],
        ])

    file_path = os.path.join(BASE_DIR, 'students_report.xlsx')
    workbook.save(file_path)
    return send_file(file_path, as_attachment=True, download_name='students_report.xlsx')


@app.route('/export/pdf')
@login_required
def export_pdf():
    conn = get_db_connection()
    rows = conn.execute(
        '''
        SELECT s.full_name, s.level, s.department,
               (SELECT score FROM evaluations WHERE student_id = s.id ORDER BY id DESC LIMIT 1) AS score,
               (SELECT rating FROM evaluations WHERE student_id = s.id ORDER BY id DESC LIMIT 1) AS rating
        FROM students s
        ORDER BY s.id DESC
        '''
    ).fetchall()
    conn.close()

    # ensure we have an Arabic-capable font registered if possible
    have_font = ensure_arabic_font()
    pdf_path = os.path.join(BASE_DIR, 'students_report.pdf')
    c = canvas.Canvas(pdf_path, pagesize=A4)
    width, height = A4
    y = height - 40
    c.setTitle('Students Report')
    title = shape_text_for_pdf('تقرير الطلاب')
    if have_font:
        c.setFont('ArabicFont', 18)
        c.setFillColor(colors.HexColor('#4B0F1A'))
        # right-align Arabic title
        c.drawRightString(width - 40, y, title)
    else:
        c.setFont('Helvetica-Bold', 18)
        c.setFillColor(colors.HexColor('#4B0F1A'))
        c.drawString(40, y, title)
    y -= 30

    header_labels = ['اسم الطالب', 'المستوى', 'القسم', 'التقييم', 'النقاط']
    if have_font:
        c.setFont('ArabicFont', 11)
    else:
        c.setFont('Helvetica', 11)
    c.setFillColor(colors.black)
    # header columns (use right-aligned for Arabic-capable font)
    if have_font:
        c.drawRightString(150, y, shape_text_for_pdf(header_labels[0]))
        c.drawRightString(240, y, shape_text_for_pdf(header_labels[1]))
        c.drawRightString(340, y, shape_text_for_pdf(header_labels[2]))
        c.drawRightString(440, y, shape_text_for_pdf(header_labels[3]))
        c.drawRightString(520, y, shape_text_for_pdf(header_labels[4]))
    else:
        c.drawString(40, y, shape_text_for_pdf(header_labels[0]))
        c.drawString(170, y, shape_text_for_pdf(header_labels[1]))
        c.drawString(260, y, shape_text_for_pdf(header_labels[2]))
        c.drawString(350, y, shape_text_for_pdf(header_labels[3]))
        c.drawString(470, y, shape_text_for_pdf(header_labels[4]))
    y -= 20

    for row in rows:
        if y < 60:
            c.showPage()
            y = height - 40
        name = shape_text_for_pdf(row['full_name'] or '')
        dept = shape_text_for_pdf(row['department'] or '')
        rating = shape_text_for_pdf(row['rating'] or 'لا يوجد')
        if have_font:
            c.setFont('ArabicFont', 11)
            # right-align Arabic columns
            c.drawRightString(150, y, name)
            c.drawRightString(240, y, shape_text_for_pdf(row['level'] or ''))
            c.drawRightString(340, y, dept)
            c.drawRightString(440, y, rating)
            c.drawRightString(520, y, str(row['score'] or 0))
        else:
            c.drawString(40, y, name)
            c.drawString(170, y, shape_text_for_pdf(row['level'] or ''))
            c.drawString(260, y, dept)
            c.drawString(350, y, rating)
            c.drawString(470, y, str(row['score'] or 0))
        y -= 18

    c.save()
    return send_file(pdf_path, as_attachment=True, download_name='students_report.pdf')


@app.route('/export/qr-bundle')
@login_required
def export_qr_bundle():
    # optional query parameter `level` to filter by student level
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

    # Leave header space for a university logo/title
    header_height = 80
    y_start = height - header_height - 20

    # draw header title (logo can be added manually to static/logo.png and drawn here)
    title_text = f'قائمة رموز QR للطلاب {"- مستوى " + level if level else ""}'
    title_text_shaped = shape_text_for_pdf(title_text)
    if have_font:
        c.setFont('ArabicFont', 14)
    else:
        c.setFont('Helvetica-Bold', 12)
    c.setFillColor(colors.HexColor('#4B0F1A'))
    c.drawString(40, height - 30, title_text_shaped)

    # layout grid
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
            # redraw header on new page
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
            generate_qr_image(student['id'], student['qr_code'])

        # draw QR and student text
        try:
            c.drawImage(qr_path, x, y - qr_size, width=qr_size, height=qr_size)
        except Exception:
            pass

        # draw name below QR, shaped for Arabic if possible
        name = shape_text_for_pdf((student['full_name'] or '') if student['full_name'] is not None else '')
        if have_font:
            c.setFont('ArabicFont', 12)
            # right-align name near the QR's right edge
            c.drawRightString(x + qr_size, y - qr_size - 16, name)
        else:
            c.setFont('Helvetica-Bold', 10)
            c.drawString(x, y - qr_size - 16, name)

        # student number smaller
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

@app.route('/student/<int:student_id>/qr.png')
def student_qr_code_image(student_id):
  conn = get_db_connection()
  student = conn.execute(
      'SELECT qr_code FROM students WHERE id = ?', (student_id,)
  ).fetchone()
  conn.close()

  if not student or not student['qr_code']:
    return 'Not Found', 404

  # رابط مسار الاستجابة الصريح والرسمي
  base_url = 'https://projectran.onrender.com'
  target_url = f"{base_url}/scan/{student['qr_code']}"

  qr = qrcode.QRCode(version=1, box_size=10, border=4)
  qr.add_data(target_url)
  qr.make(fit=True)
  img = qr.make_image(fill_color='black', back_color='white')

  img_io = BytesIO()
  img.save(img_io, 'PNG')
  img_io.seek(0)

  return send_file(img_io, mimetype='image/png')


def init_db():
    conn = get_db_connection()
    # إنشاء الجداول إذا لم تكن موجودة
    # ... (تظل باقي استعلامات CREATE TABLE كما هي لديكِ) ...
    conn.commit()

    # تحديث كل الـ QRs تلقائياً عند التشغيل
    with app.app_context():
        try:
            students = conn.execute('SELECT id, qr_code FROM students').fetchall()
            for student in students:
                if student['qr_code']:
                    generate_qr_image(student['id'], student['qr_code'])
        except Exception as e:
            print("QR Sync Error:", e)

    conn.close()

if __name__ == '__main__':
    app.run(debug=True)