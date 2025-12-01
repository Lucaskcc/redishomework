# app.py

from flask import Flask, render_template, request, redirect, url_for, session, abort
import os
import secrets
import datetime
import redis
import redis_db as db # 引入 Redis 資料庫操作模組

# --- Flask 設定 ---
app = Flask(__name__)
# 使用 secrets 模組產生安全密鑰
app.secret_key = secrets.token_hex(16) 

# --- Redis 連線設定 ---
# ⚠️ 請將這裡替換成您的 Redis 連線資訊 ⚠️
REDIS_HOST = 'redis-13609.c16.us-east-1-2.ec2.cloud.redislabs.com'
REDIS_PORT = 13609
REDIS_PASSWORD = 'B0clHH6UC4SQf3FeWmAls9FJEArApTxQ'

# 連線到 Redis
try:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
    r.ping()
    print("成功連線到 Redis！")
except Exception as e:
    print(f"Redis 連線失敗: {e}")
    # 這裡可以選擇退出應用程式或使用一個模擬資料庫
    # 為了本專案，我們假設連線成功

# 啟動時初始化資料庫
db.init_redis(r)

# --- 裝飾器：登入檢查 ---
def login_required(f):
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

# --- 前台報名介面 ---

@app.route('/')
def index():
    """首頁：顯示可報名班別"""
    # 呼叫 redis_db 取得開放報名的班別
    open_slots = db.get_open_slots() 
    return render_template('index.html', slots=open_slots)

@app.route('/signup/<slot_id>', methods=['GET', 'POST'])
def signup(slot_id):
    """報名頁面：輸入姓名和身分證後四碼"""
    
    slot = db.get_slot_by_id(slot_id)
    
    if not slot or not slot['is_open']:
        # 班別不存在或未開放
        return render_template('error.html', 
                                message="此班別不存在或已停止報名。", 
                                title="報名錯誤")

    date_str = slot['work_date']
    slot_name = slot['slot_name']
    
    error = None
    
    if request.method == 'POST':
        name = request.form.get('name').strip()
        id_last_4 = request.form.get('id_last_4').strip()
        
        if not name or len(id_last_4) != 4 or not id_last_4.isdigit():
            error = "請檢查您的姓名和身分證後四碼是否輸入正確。"
        else:
            # 執行報名
            result = db.signup_person(slot_id, name, id_last_4)
            
            if result is True:
                # 報名成功
                return redirect(url_for('success', 
                                        slot_id=slot_id, 
                                        name=name))
            elif result == "DUP":
                error = "您已報名過此班別，請勿重複報名。"
            elif result is False:
                error = "報名失敗！此班別名額已滿，請選擇其他班別。"
            else:
                error = "系統錯誤，報名未成功。"
                
    return render_template('signup_form.html', 
                            slot_id=slot_id, 
                            date_str=date_str, 
                            slot_name=slot_name, 
                            error=error)

@app.route('/success')
def success():
    """報名成功頁面"""
    slot_id = request.args.get('slot_id')
    name = request.args.get('name')
    
    slot = db.get_slot_by_id(slot_id)
    if not slot:
        abort(404)
        
    return render_template('success.html', 
                            name=name,
                            date_str=slot['work_date'],
                            slot_name=slot['slot_name'])

# --- 管理員介面 ---

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """管理員登入"""
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        admin_user = db.get_admin_user(username)
        
        # 驗證帳號密碼
        if admin_user and admin_user['password'] == password:
            session['logged_in'] = True
            session['username'] = username
            session['user_role'] = admin_user['role'] # 'super' 或 'viewer'
            return redirect(url_for('admin_dashboard'))
        else:
            error = "帳號或密碼錯誤。"
            
    return render_template('admin_login.html', error=error)

@app.route('/admin/logout')
def admin_logout():
    """管理員登出"""
    session.pop('logged_in', None)
    session.pop('username', None)
    session.pop('user_role', None)
    return redirect(url_for('index'))

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    """管理員儀表板：查看所有班別及名額"""
    
    # 獲取所有班別資訊
    slots = db.get_all_slots_for_admin()
    
    return render_template('admin_dashboard.html', 
                            slots=slots,
                            user_role=session.get('user_role'))

@app.route('/admin/edit/<slot_id>', methods=['GET', 'POST'])
@login_required
def admin_edit_slot(slot_id):
    """編輯現有班別"""
    
    # 只有 Super Admin 可以編輯/刪除
    if session.get('user_role') != 'super':
        return redirect(url_for('admin_dashboard'))

    slot = db.get_slot_by_id(slot_id)
    if not slot:
        abort(404)

    error = None
    if request.method == 'POST':
        if 'delete' in request.form:
            # 刪除操作
            db.delete_slot(slot_id)
            return redirect(url_for('admin_dashboard'))

        # 編輯操作
        new_work_date = request.form.get('work_date').strip()
        new_slot_name = request.form.get('slot_name').strip()
        new_is_open = 'is_open' in request.form
        capacity_str = request.form.get('capacity').strip()
        
        try:
            capacity = int(capacity_str)
            if capacity <= 0:
                 error = "最大報名人數必須大於 0。"
        except ValueError:
            error = "最大報名人數必須為數字。"

        try:
            datetime.date.fromisoformat(new_work_date)
        except ValueError:
            error = "日期格式錯誤，請使用 YYYY-MM-DD 格式。"

        if not error and new_work_date and new_slot_name:
            db.update_slot(slot_id, new_work_date, new_slot_name, new_is_open, capacity)
            return redirect(url_for('admin_dashboard'))

    return render_template('admin_edit.html', 
                            slot=slot, 
                            error=error, 
                            is_new=False, 
                            user_role=session.get('user_role'))

@app.route('/admin/new', methods=['GET', 'POST'])
@login_required
def admin_new_slot():
    """新增班別"""
    
    # 只有 Super Admin 可以新增
    if session.get('user_role') != 'super':
        return redirect(url_for('admin_dashboard'))
    
    slot = {'work_date': datetime.date.today().isoformat(), 'slot_name': '', 'is_open': True, 'capacity': 5}
    error = None
    
    if request.method == 'POST':
        new_work_date = request.form.get('work_date').strip()
        new_slot_name = request.form.get('slot_name').strip()
        new_is_open = 'is_open' in request.form
        capacity_str = request.form.get('capacity').strip()

        try:
            capacity = int(capacity_str)
            if capacity <= 0:
                 error = "最大報名人數必須大於 0。"
        except ValueError:
            error = "最大報名人數必須為數字。"

        try:
            datetime.date.fromisoformat(new_work_date)
        except ValueError:
            error = "日期格式錯誤，請使用 YYYY-MM-DD 格式。"

        if not error and new_work_date and new_slot_name:
            db.create_new_slot(new_work_date, new_slot_name, new_is_open, capacity)
            return redirect(url_for('admin_dashboard'))

    return render_template('admin_edit.html', 
                            slot=slot, 
                            error=error, 
                            is_new=True, 
                            user_role=session.get('user_role'))


@app.route('/admin/view_bookings/<slot_id>', methods=['GET'])
@login_required
def admin_view_bookings(slot_id):
    """查看特定班別的報名名單"""
    slot = db.get_slot_by_id(slot_id)
    if not slot:
        abort(404)
        
    bookings = db.get_bookings_for_slot(slot_id)
    
    return render_template('admin_view_bookings.html', 
                            slot=slot, 
                            bookings=bookings, 
                            user_role=session.get('user_role'))
                            
@app.route('/admin/delete_booking/<slot_id>/<employee_id>', methods=['POST'])
@login_required
def admin_delete_booking(slot_id, employee_id):
    """管理員刪除單筆報名 (僅限 Super Admin)"""
    
    if session.get('user_role') != 'super':
        return redirect(url_for('admin_view_bookings', slot_id=slot_id))
        
    db.delete_booking(slot_id, employee_id)
    return redirect(url_for('admin_view_bookings', slot_id=slot_id))


if __name__ == '__main__':
    # 確保資料夾存在
    if not os.path.exists('templates'):
        os.makedirs('templates')
    if not os.path.exists('static'):
        os.makedirs('static')
        
    app.run(debug=True, host='0.0.0.0',port=5001)