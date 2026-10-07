import sqlite3
import psycopg
from psycopg.rows import dict_row
import os

# 1. رابط قاعدة البيانات السحابية (Supabase)
DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://postgres.itippdkmyrrmvqaooneo:promamal121@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres')

def migrate_data():
    print("🚀 بدء عملية إنشاء الجداول ونقل البيانات إلى Supabase...")

    # الاتصال بقاعدة البيانات المحلية SQLite
    sqlite_conn = sqlite3.connect('database.db')
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cursor = sqlite_conn.cursor()

    # الاتصال بقاعدة البيانات السحابية PostgreSQL
    pg_conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    pg_cursor = pg_conn.cursor()

    # --- أ: إنشاء الجداول على PostgreSQL بنفس هيكل SQLite ---
    pg_cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(255) UNIQUE NOT NULL,
            password VARCHAR(255) NOT NULL,
            full_name VARCHAR(255) NOT NULL,
            role VARCHAR(50) DEFAULT 'supervisor'
        );

        CREATE TABLE IF NOT EXISTS students (
            id SERIAL PRIMARY KEY,
            full_name VARCHAR(255) NOT NULL,
            student_number VARCHAR(100) UNIQUE NOT NULL,
            level VARCHAR(100),
            department VARCHAR(100),
            phone VARCHAR(50),
            photo VARCHAR(255) DEFAULT 'default_student.png',
            qr_code VARCHAR(255) UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS evaluations (
            id SERIAL PRIMARY KEY,
            student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
            supervisor_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            academic_year VARCHAR(50) NOT NULL,
            score INTEGER NOT NULL,
            rating VARCHAR(100) NOT NULL,
            notes TEXT,
            evaluation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS damage_reports (
            id SERIAL PRIMARY KEY,
            student_id INTEGER REFERENCES students(id) ON DELETE CASCADE,
            supervisor_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            damage_type VARCHAR(255) NOT NULL,
            description TEXT,
            damage_photo VARCHAR(255),
            report_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    pg_conn.commit()

    # --- ب: نقل جدول المستخدمين (users) ---
    sqlite_cursor.execute("SELECT * FROM users")
    users = sqlite_cursor.fetchall()
    for u in users:
        pg_cursor.execute('''
            INSERT INTO users (id, username, password, full_name, role)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING;
        ''', (u['id'], u['username'], u['password'], u['full_name'], u['role']))

    # --- ج: نقل جدول الطلاب (students) ---
    sqlite_cursor.execute("SELECT * FROM students")
    students = sqlite_cursor.fetchall()
    for s in students:
        pg_cursor.execute('''
            INSERT INTO students (id, full_name, student_number, level, department, phone, photo, qr_code, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING;
        ''', (s['id'], s['full_name'], s['student_number'], s['level'], s['department'], s['phone'], s['photo'], s['qr_code'], s['created_at']))

    # --- د: نقل جدول التقييمات (evaluations) ---
    sqlite_cursor.execute("SELECT * FROM evaluations")
    evaluations = sqlite_cursor.fetchall()
    for e in evaluations:
        pg_cursor.execute('''
            INSERT INTO evaluations (id, student_id, supervisor_id, academic_year, score, rating, notes, evaluation_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING;
        ''', (e['id'], e['student_id'], e['supervisor_id'], e['academic_year'], e['score'], e['rating'], e['notes'], e['evaluation_date']))

    # --- هـ: نقل جدول الأضرار (damage_reports) ---
    sqlite_cursor.execute("SELECT * FROM damage_reports")
    reports = sqlite_cursor.fetchall()
    for r in reports:
        pg_cursor.execute('''
            INSERT INTO damage_reports (id, student_id, supervisor_id, damage_type, description, damage_photo, report_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING;
        ''', (r['id'], r['student_id'], r['supervisor_id'], r['damage_type'], r['description'], r['damage_photo'], r['report_date']))

    # مزامنة الـ Sequences (الترقيم التلقائي) في PostgreSQL
    tables = ['users', 'students', 'evaluations', 'damage_reports']
    for t in tables:
        pg_cursor.execute(f"""
            SELECT setval(
                pg_get_serial_sequence('{t}', 'id'),
                COALESCE((SELECT MAX(id) FROM {t}), 1),
                true
            );
        """)

    pg_conn.commit()
    sqlite_conn.close()
    pg_conn.close()

    print("🎉 تم نقل جميع البيانات القديمة (الطلاب، المستخدمين، التقييمات، والبلاغات) إلى Supabase بنجاح!")

if __name__ == '__main__':
    migrate_data()