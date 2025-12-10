# app.py

from flask import Flask, render_template, request, redirect, url_for, session, abort
import os
import secrets
import datetime
import redis
import redis_db as db # 引入 Redis 資料庫操作模組
import re # 用於正規表達式驗證
from functools import wraps # <<< 修正：新增這行來引入正確的 wraps 函式

# --- Flask 設定 ---
app = Flask(__name__)
# 使用 secrets 模組產生安全密鑰
app.secret_key = secrets.token_hex(16) 
app.config['JSON_AS_ASCII'] = False # 確保中文正常顯示

# --- Redis 連線設定 (請替換為您的連線資訊) ---
# ⚠️ 請將這裡替換成您的 Redis 連線資訊 ⚠️
REDIS_HOST = 'redis-13609.c16.us-east-1-2.ec2.cloud.redislabs.com' # 請替換
REDIS_PORT = 13609 # 請替換
REDIS_PASSWORD = 'B0clHH6UC4SQf3FeWmAls9FJEArApTxQ' # 請替換

# 連線到 Redis
try:
    # 注意：如果您的 Redis 伺服器在遠端，請確保您的環境允許出站連線。
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
    r.ping()
    print("成功連線到 Redis！")
except Exception as e:
    print(f"Redis 連線失敗: {e}")
    # 在實際應用中，這裡可能需要更優雅的處理

# 啟動時初始化資料庫
db.init_redis(r)

# --- 裝飾器：登入檢查 ---
def login_required(f):
    """檢查使用者是否已登入管理員帳號"""
    @wraps(f) # <<< 修正：將 @app.route.wraps(f) 改為 @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

# --- 前台報名介面 ---

@app.route('/')
def index():
    """首頁：顯示可報名天數/班別"""
    open_slots = db.get_open_slots() 
    return render_template('index.html', slots=open_slots)


@app.route('/new_employee', methods=['GET', 'POST'])
def new_employee():
    """新人註冊員工資料"""
    error = None
    success = None
    
    if request.method == 'POST':
        name = request.form.get('name')
        # 統一轉換為大寫
        id_full = request.form.get('id_full').upper() 
        phone = request.form.get('phone')
        
        # 簡單的身分證格式檢查 (A-Z + 9位數字)
        if not re.match(r'^[A-Z][0-9]{9}$', id_full):
             error = "身分證字號格式錯誤，請輸入大寫英文字母開頭，共 10 碼。"
        elif not name or not phone:
            error = "姓名和聯絡電話不可為空。"
        else:
            employee_id, msg = db.create_employee(name, id_full, phone)
            
            if employee_id:
                success = f"恭喜您，**{name}** 先生/小姐，員工資料註冊成功！<br>您現在可以使用身分證後四碼進行報名。"
            else:
                error = msg

    return render_template('new_employee.html', error=error, success=success)


@app.route('/signup/<slot_id>', methods=['GET', 'POST'])
def signup(slot_id):
    """報名表單及處理邏輯"""
    slot = db.get_slot_by_id(slot_id)
    if not slot or not slot['is_open']:
        return render_template('error.html', title="報名失敗", message="該班別不存在或已關閉報名。")

    date_str = slot['work_date']
    slot_name = slot['slot_name']
    error = None

    if request.method == 'POST':
        name = request.form.get('name')
        id_last_4 = request.form.get('id_last_4')
        
        # 1. 檢查員工資料是否存在
        employee_id, employee_data = db.get_employee_by_info(name, id_last_4)

        if not employee_id:
            error = "員工資料驗證失敗。請確認您的姓名與身分證後四碼是否正確，或是否已進行員工註冊。"
        else:
            # 2. 嘗試報名
            success, msg = db.add_booking(slot_id, date_str, employee_id, name, id_last_4)
            
            if success:
                return redirect(url_for('success_page', 
                                        slot_id=slot_id, 
                                        name=name, 
                                        date_str=date_str, 
                                        slot_name=slot_name))
            else:
                error = msg # 可能是額滿或重複報名
                # 重新獲取最新的 slot 資訊，避免顯示過時的容量
                slot = db.get_slot_by_id(slot_id)

    # GET 請求或 POST 失敗時渲染表單
    return render_template('signup_form.html', 
                           slot_id=slot_id, 
                           date_str=date_str, 
                           slot_name=slot_name, 
                           slot=slot,
                           error=error)

@app.route('/success')
def success_page():
    """報名成功頁面"""
    name = request.args.get('name')
    date_str = request.args.get('date_str')
    slot_name = request.args.get('slot_name')
    
    if not name or not date_str or not slot_name:
        return redirect(url_for('index'))

    return render_template('success.html', name=name, date_str=date_str, slot_name=slot_name)

# --- 管理員後台介面 ---

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """管理員登入頁面"""
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = db.get_admin_user(username)
        
        if user and user['password'] == password:
            session['logged_in'] = True
            session['username'] = user['username']
            session['user_role'] = user['role'] # 'super' 或 'viewer'
            return redirect(url_for('admin_dashboard'))
        else:
            error = "帳號或密碼錯誤。"
            
    return render_template('admin_login.html', error=error)

@app.route('/admin/register', methods=['GET', 'POST'])
def admin_register():
    """管理員註冊頁面"""
    error = None
    success = None
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        auth_code = request.form.get('auth_code')
        
        if password != confirm_password:
            error = "兩次輸入的密碼不一致。"
        elif db.get_admin_user(username):
            error = "此管理員帳號已存在。"
        else:
            role = None
            if auth_code == db.SUPER_ADMIN_CODE:
                role = 'super'
            elif auth_code == db.VIEWER_ADMIN_CODE:
                role = 'viewer'
            else:
                error = "權限驗證碼錯誤。"
            
            if role:
                if db.create_admin_user(username, password, role):
                    success = f"管理員帳號 **{username}** 註冊成功，權限等級：**{role.upper()}**。"
                else:
                    error = "註冊失敗，請重試。"

    return render_template('admin_register.html', error=error, success=success)

@app.route('/admin/account', methods=['GET', 'POST'])
@login_required
def admin_account():
    """管理員帳號設定 (修改密碼)"""
    username = session.get('username')
    user_role = session.get('user_role')
    error = None
    success = None

    if request.method == 'POST':
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        if new_password != confirm_password:
            error = "新密碼與確認密碼不一致。"
        elif len(new_password) < 4:
            error = "密碼長度至少需要 4 個字元。"
        else:
            if db.update_admin_password(username, new_password):
                success = "密碼已成功更新！"
            else:
                error = "密碼更新失敗，請聯絡系統管理員。"

    return render_template('admin_account.html', 
                           username=username, 
                           user_role=user_role, 
                           error=error, 
                           success=success)

@app.route('/admin/logout')
def admin_logout():
    """管理員登出"""
    session.clear()
    return redirect(url_for('admin_login'))

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    """管理員儀表板：顯示所有班別和摘要名單"""
    slots = db.get_all_slots()
    
    return render_template('admin_dashboard.html', 
                           slots=slots,
                           user_role=session.get('user_role'))

@app.route('/admin/add_slot', methods=['GET', 'POST'])
@login_required
def admin_add_slot():
    """管理員：新增班別"""
    # 權限檢查：只有 super 才能新增/編輯
    if session.get('user_role') != 'super':
        return render_template('admin_edit.html', slot={'work_date': '', 'slot_name': '', 'capacity': 5, 'is_open': True}, error="⚠️ 權限不足！您無權新增。", is_new=True, user_role=session.get('user_role'))

    error = None
    
    if request.method == 'POST':
        work_date = request.form.get('work_date')
        slot_name = request.form.get('slot_name')
        capacity_str = request.form.get('capacity')
        # 檢查 checkbox 是否被勾選
        is_open = 'is_open' in request.form 
        
        try:
            capacity = int(capacity_str)
            if capacity < 1:
                raise ValueError("容量必須大於 0")
        except ValueError as e:
            error = f"容量格式錯誤: {e}"
        
        try:
            # 簡單的日期格式檢查
            datetime.date.fromisoformat(work_date)
        except ValueError:
            error = "日期格式錯誤，請使用 YYYY-MM-DD。"

        if not error and work_date and slot_name:
            db.add_slot(work_date, slot_name, is_open, capacity)
            return redirect(url_for('admin_dashboard'))

    # GET 請求或 POST 失敗時渲染
    # 傳遞一個空的 slot 字典給模板用於預設值
    empty_slot = {'work_date': datetime.date.today().isoformat(), 'slot_name': '', 'capacity': 5, 'is_open': True}
    return render_template('admin_edit.html', 
                           slot=empty_slot, 
                           error=error, 
                           is_new=True, 
                           user_role=session.get('user_role'))

@app.route('/admin/edit_slot/<slot_id>', methods=['GET', 'POST'])
@login_required
def admin_edit_slot(slot_id):
    """管理員：編輯班別"""
    
    slot = db.get_slot_by_id(slot_id)
    if not slot:
        abort(404)

    # 權限檢查：非 super 只能查看
    if session.get('user_role') != 'super' and request.method == 'POST':
        return render_template('admin_edit.html', slot=slot, error="⚠️ 權限不足！您無權編輯。", is_new=False, user_role=session.get('user_role'))

    error = None
    
    if request.method == 'POST':
        # 檢查是否為刪除請求 (僅限 super admin)
        if 'delete' in request.form:
            if session.get('user_role') == 'super':
                db.delete_slot(slot_id)
                return redirect(url_for('admin_dashboard'))
            else:
                error = "權限不足，無法刪除班別。"
                
        # 處理編輯請求
        new_work_date = request.form.get('work_date')
        new_slot_name = request.form.get('slot_name')
        new_capacity_str = request.form.get('capacity')
        new_is_open = 'is_open' in request.form
        
        if not error: # 檢查刪除請求是否有權限錯誤
            try:
                new_capacity = int(new_capacity_str)
                if new_capacity < 1:
                    raise ValueError("容量必須大於 0")
            except ValueError as e:
                error = f"容量格式錯誤: {e}"
            
            try:
                datetime.date.fromisoformat(new_work_date)
            except ValueError:
                error = "日期格式錯誤，請使用 YYYY-MM-DD。"
                
            if not error and new_work_date and new_slot_name:
                db.update_slot(slot_id, new_work_date, new_slot_name, new_is_open, new_capacity) 
                return redirect(url_for('admin_dashboard'))

    # GET 請求或 POST 失敗時渲染
    return render_template('admin_edit.html', 
                           slot=slot, 
                           error=error, 
                           is_new=False, 
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
        # 即使是 POST 請求，如果沒有權限，也只導向查看頁面
        return redirect(url_for('admin_view_bookings', slot_id=slot_id))
        
    db.delete_booking(slot_id, employee_id)
    return redirect(url_for('admin_view_bookings', slot_id=slot_id))

# --- 錯誤處理 ---
@app.errorhandler(404)
def page_not_found(e):
    return render_template('error.html', title="404 找不到頁面", message="您訪問的頁面不存在，請檢查網址是否正確。"), 404

if __name__ == '__main__':
    # Flask 啟動
    app.run(debug=True,port=5001)