# redis_db.py

import redis
import datetime
import json
import secrets 
import time

# 全域 Redis 客戶端變數
r = None

# --- Redis 鍵命名規範 ---
# 班別詳細資訊 (Hash):        slot:<slot_id>
# 開放班別列表 (Sorted Set): open_slots_set (score=work_date的Unix時間戳)
# 報名人數計數器 (String):    slot:<slot_id>:count
# 報名名單 (List):            slot:<slot_id>:bookings
# 報名唯一性索引 (Set):       slot:<slot_id>:members   
# 管理員帳號 (Hash):         admin_user:<username>
# 員工資料 (Hash):           employee:<employee_id>
# 員工資料索引 (Hash):       employee_index:<id_full> -> employee_id

# --- 輔助函式 ---

def _create_slot_data(slot_id, work_date, slot_name, is_open=True, capacity=5):
    """創建或更新班別 Hash 資料並設定計數器和開放索引"""
    pipe = r.pipeline()
    
    # 1. 班別詳細資訊 (Hash)
    slot_data = {
        'work_date': work_date,
        'slot_name': slot_name,
        'is_open': str(is_open), # Redis Hash 儲存字串
        'capacity': str(capacity)
    }
    pipe.hset(f'slot:{slot_id}', mapping=slot_data)
    
    # 2. 報名人數計數器 (String) - 如果不存在才建立並設為 0
    pipe.setnx(f'slot:{slot_id}:count', 0)
    
    # 3. 開放班別索引 (Sorted Set)
    if is_open:
        # 將日期轉換為 Unix Timestamp 作為 Score (確保排序)
        timestamp = int(datetime.datetime.strptime(work_date, '%Y-%m-%d').timestamp())
        pipe.zadd('open_slots_set', {slot_id: timestamp})
    else:
        pipe.zrem('open_slots_set', slot_id)
        
    pipe.execute()

# --- 員工 (Employee) 相關功能 ---

def create_employee(name, id_full, phone):
    """註冊新員工，返回 employee_id"""
    if r.hexists('employee_index', id_full):
        # 由於 Redis 的 HSET 不支援直接儲存密碼或敏感資訊，employee_index 儲存的是 id_full -> employee_id
        # 我們假設如果 index 存在，員工資料就存在
        return None, "員工資料已存在。"
    
    employee_id = secrets.token_urlsafe(8)
    pipe = r.pipeline()
    # 1. 儲存員工資料 Hash
    pipe.hset(f'employee:{employee_id}', mapping={
        'name': name,
        'id_full': id_full,
        'id_last_4': id_full[-4:],
        'phone': phone
    })
    # 2. 建立反向索引 (完整身分證 -> employee_id)
    pipe.hset('employee_index', id_full, employee_id)
    pipe.execute()
    return employee_id, "註冊成功"

def get_employee_by_info(name, id_last_4):
    """透過姓名和身分證後四碼查找員工 ID"""
    
    # 由於沒有直接的 name:last_4 索引，只能查找所有 employee:* 的 key
    # ⚠️ 注意：在生產環境中，如果有大量數據，應避免使用 KEYS 命令
    
    employee_keys = r.keys('employee:*')
    for key in employee_keys:
        employee_data = r.hgetall(key)
        if employee_data.get('name') == name and employee_data.get('id_last_4') == id_last_4:
            return key.split(':')[-1], employee_data # 返回 employee_id 和資料
    
    return None, None # 未找到

# --- 班別 (Slot) 相關功能 ---

def get_slot_by_id(slot_id):
    """獲取單個班別的詳細資訊，包含報名人數"""
    slot_hash = r.hgetall(f'slot:{slot_id}')
    if not slot_hash:
        return None

    slot = {k: v for k, v in slot_hash.items()}
    slot['id'] = slot_id
    slot['is_open'] = slot.get('is_open') == 'True'
    slot['capacity'] = int(slot.get('capacity', 5))
    
    # 獲取目前的報名人數
    slot['current_bookings'] = get_current_booking_count(slot_id)
    
    return slot

def get_all_slots():
    """
    獲取所有班別的詳細資訊 (已修正 WRONGTYPE 錯誤)。
    
    此處僅獲取 'slot:<id>' 格式的主鍵，過濾掉輔助鍵 (例如: slot:<id>:count)。
    """
    # 1. 獲取所有以 'slot:' 開頭的鍵
    all_keys = r.keys('slot:*')
    
    # 2. **修正邏輯**：過濾出真正的班別 Hash 鍵 (即鍵中只有一個 ':'，格式為 slot:<id>)
    slot_hash_keys = [key for key in all_keys if len(key.split(':')) == 2]
    
    all_slots = []
    if not slot_hash_keys:
        return []

    # 3. 使用 Pipeline 一次性獲取所有班別的詳細資訊
    pipe = r.pipeline()
    for key in slot_hash_keys:
        pipe.hgetall(key)
        
    slot_data_list = pipe.execute()
    
    # 4. 處理並轉換資料
    for i, slot_data in enumerate(slot_data_list):
        if slot_data: 
            key_name = slot_hash_keys[i]
            slot_id = key_name.split(':')[-1]
            
            # 從 Redis (str) 轉換為 Python 字典
            slot = {k: v for k, v in slot_data.items()}
            
            # 將字串轉換回正確的類型
            slot['id'] = slot_id
            slot['is_open'] = slot.get('is_open') == 'True' 
            slot['capacity'] = int(slot.get('capacity', 5))
            
            # 獲取目前的報名人數和名單
            slot['current_bookings'] = get_current_booking_count(slot_id)
            slot['bookings'] = get_bookings_for_slot(slot_id) 
            
            all_slots.append(slot)
            
    # 根據日期排序
    all_slots.sort(key=lambda s: s['work_date'])
    
    return all_slots

def get_open_slots():
    """獲取目前開放報名的班別，並按日期排序 (使用 ZSET)"""
    
    # 1. 從 Sorted Set 獲取所有開放的 slot_id (按 score/日期升序)
    slot_ids = r.zrange('open_slots_set', 0, -1) 
    
    open_slots = []
    pipe = r.pipeline()
    for slot_id in slot_ids:
        pipe.hgetall(f'slot:{slot_id}')
        
    slot_data_list = pipe.execute()
    
    # 2. 處理資料
    for i, slot_data in enumerate(slot_data_list):
        if slot_data:
            slot_id = slot_ids[i]
            slot = {k: v for k, v in slot_data.items()}
            
            slot['id'] = slot_id
            slot['is_open'] = slot.get('is_open') == 'True'
            slot['capacity'] = int(slot.get('capacity', 5)) 
            
            # 為了首頁顯示容量，必須在這裡取得人數
            slot['current_bookings'] = get_current_booking_count(slot_id)
            
            # 只有開放報名的班別才應被列在首頁清單
            if slot['is_open']:
                open_slots.append(slot)
    
    return open_slots

def add_slot(work_date, slot_name, is_open=True, capacity=5):
    """新增一個班別"""
    slot_id = secrets.token_urlsafe(8)
    _create_slot_data(slot_id, work_date, slot_name, is_open, capacity)
    return slot_id

def update_slot(slot_id, work_date, slot_name, is_open, capacity):
    """更新班別 Hash 資料、容量並重新設定 ZSET 索引"""
    _create_slot_data(slot_id, work_date, slot_name, is_open, capacity)

def delete_slot(slot_id):
    """刪除班別及其所有相關數據"""
    pipe = r.pipeline()
    pipe.delete(f'slot:{slot_id}')              # 刪除 Hash
    pipe.delete(f'slot:{slot_id}:count')        # 刪除計數器
    pipe.delete(f'slot:{slot_id}:bookings')     # 刪除報名名單 List
    pipe.delete(f'slot:{slot_id}:members')      # 刪除成員集合 Set
    pipe.zrem('open_slots_set', slot_id)        # 刪除 ZSET 索引
    pipe.execute()

# --- 報名 (Booking) 相關功能 ---

def is_already_booked(slot_id, employee_id):
    """檢查該員工是否已報名此班別 (使用 Set)"""
    return r.sismember(f'slot:{slot_id}:members', employee_id)

def get_current_booking_count(slot_id):
    """獲取目前報名人數 (使用計數器 String)"""
    count = r.get(f'slot:{slot_id}:count')
    return int(count) if count else 0

def get_bookings_for_slot(slot_id):
    """獲取所有報名名單 (使用 List)"""
    # Lrange 返回的是 JSON 字串列表
    booking_list_json = r.lrange(f'slot:{slot_id}:bookings', 0, -1)
    # 將 JSON 字串轉換為 Python 字典
    bookings = [json.loads(b) for b in booking_list_json]
    # List 是按照報名順序排列的，不需要額外排序
    return bookings

def add_booking(slot_id, work_date, employee_id, employee_name, id_last_4):
    """新增報名記錄 (使用 List 儲存 JSON，並增加計數器和 Set 索引)"""
    
    # 1. 檢查是否已滿
    slot = get_slot_by_id(slot_id)
    if not slot or slot['current_bookings'] >= slot['capacity']:
        return False, "該班別已額滿或不存在。"
    
    # 2. 檢查是否已報名
    if is_already_booked(slot_id, employee_id):
        return False, "您已報名此班別，請勿重複報名。"

    # 3. 執行報名操作 (使用 Transaction / Pipeline + WATCH)
    booking_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # 報名資料 (存入 List)
    booking_data = {
        'employee_id': employee_id,
        'name': employee_name,
        'id_last_4': id_last_4,
        'booking_time': booking_time
    }
    
    # 使用 WATCH + MULTI/EXEC 確保報名人數檢查是原子操作
    pipe = r.pipeline()
    
    # Watch count key to prevent race condition on capacity check
    pipe.watch(f'slot:{slot_id}:count') 

    # Re-check capacity within the watch block
    current_count = r.get(f'slot:{slot_id}:count')
    current_count = int(current_count) if current_count else 0
    
    if current_count < slot['capacity']:
        # 開始交易
        pipe.multi() 
        
        # 增加報名人數計數器
        pipe.incr(f'slot:{slot_id}:count')
        # 將報名資料推入 List (儲存 JSON 字串)
        pipe.rpush(f'slot:{slot_id}:bookings', json.dumps(booking_data))
        # 將員工 ID 加入 Set (用於快速檢查是否已報名)
        pipe.sadd(f'slot:{slot_id}:members', employee_id)
        
        try:
            pipe.execute() # 執行交易
            return True, "報名成功"
        except redis.exceptions.WatchError:
            # WatchError 表示在 WATCH 期間 key 被修改，需重試
            return False, "系統忙碌，請重試報名。"
    else:
        # 容量已滿
        pipe.unwatch()
        return False, "報名失敗：該班別已額滿。"


def delete_booking(slot_id, employee_id):
    """刪除特定報名者 (使用 List 的 LREM，並減少計數器和 Set 索引)"""
    
    booking_key = f'slot:{slot_id}:bookings'
    members_key = f'slot:{slot_id}:members' 
    
    # 1. 找出要刪除的報名記錄的 JSON 字串
    bookings = get_bookings_for_slot(slot_id)
    target_booking_json = None
    
    for booking in bookings:
        if booking['employee_id'] == employee_id:
            target_booking_json = json.dumps(booking)
            break

    if target_booking_json:
        pipe = r.pipeline()
        
        # 刪除 List 中的 JSON 字串 (只刪除一個)
        pipe.lrem(booking_key, 1, target_booking_json)
        # 從 Set 中刪除員工 ID
        pipe.srem(members_key, employee_id)
        # 減少計數器
        pipe.decr(f'slot:{slot_id}:count')
        
        pipe.execute()
        return True
    return False

# --- 管理員 (Admin) 相關功能 ---

def init_redis(redis_client):
    """初始化 Redis 連線和預設資料"""
    global r
    r = redis_client
    
    if not r.exists('admin_user:admin'):
        print("Redis 初始化：載入預設資料...")
        
        # 創建預設的管理員帳號 (Super Admin & Viewer)
        r.hset('admin_user:admin', mapping={'password': 'super', 'role': 'super'})
        r.hset('admin_user:viewer', mapping={'password': 'view', 'role': 'viewer'})
        
        # 創建預設開放班別
        today = datetime.date.today()
        
        slot_id_1 = secrets.token_urlsafe(8)
        work_date_1 = (today + datetime.timedelta(days=1)).isoformat()
        _create_slot_data(slot_id_1, work_date_1, '上午班 (08:00-12:00)', is_open=True, capacity=5)
        
        slot_id_2 = secrets.token_urlsafe(8)
        work_date_2 = (today + datetime.timedelta(days=2)).isoformat()
        _create_slot_data(slot_id_2, work_date_2, '下午班 (13:00-17:00)', is_open=True, capacity=10)
        
        slot_id_3 = secrets.token_urlsafe(8)
        work_date_3 = (today + datetime.timedelta(days=3)).isoformat()
        _create_slot_data(slot_id_3, work_date_3, '假日全天班 (09:00-17:00)', is_open=False, capacity=5)
        
        print("Redis 初始化完成。")


def get_admin_user(username):
    """根據使用者名稱獲取管理員資料"""
    key = f'admin_user:{username}'
    user_data = r.hgetall(key)
    if user_data:
        # 確保 key 存在，且至少包含 password 和 role
        return {'username': username, 'password': user_data.get('password'), 'role': user_data.get('role')}
    return None

def create_admin_user(username, password, role):
    """新增管理員帳號"""
    key = f'admin_user:{username}'
    if r.exists(key):
        return False # 帳號已存在
    
    r.hset(key, mapping={'password': password, 'role': role})
    return True

def update_admin_password(username, new_password):
    """更新管理員密碼"""
    key = f'admin_user:{username}'
    if r.exists(key):
        r.hset(key, 'password', new_password)
        return True
    return False

# ----------------------------------------------------------------------
# 以下為管理員驗證碼，用於註冊時判斷權限
SUPER_ADMIN_CODE = '7726a9c1-58e1-4c4f-9e2e-2e0f8073b64c'
VIEWER_ADMIN_CODE = '1e0a3b8d-2d4e-4f5c-8b7a-9c6d5e4f3a2b'
# ----------------------------------------------------------------------