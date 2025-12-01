# redis_db.py (已修正容量檢查邏輯)

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

def init_redis(redis_client):
    """初始化 Redis 連線和預設資料"""
    global r
    r = redis_client
    
    if not r.exists('admin_user:admin'):
        print("Redis 初始化：載入預設資料...")
        
        # 創建預設的管理員帳號 (Super Admin)
        r.hset('admin_user:admin', mapping={'password': 'super', 'role': 'super'})
        r.hset('admin_user:viewer', mapping={'password': 'view', 'role': 'viewer'})
        
        # 創建預設開放班別
        today = datetime.date.today()
        
        slot_id_1 = secrets.token_urlsafe(8)
        work_date_1 = (today + datetime.timedelta(days=1)).isoformat()
        _create_slot_data(slot_id_1, work_date_1, "08:00-12:00 上午班", True, 5)
        
        slot_id_2 = secrets.token_urlsafe(8)
        work_date_2 = (today + datetime.timedelta(days=2)).isoformat()
        _create_slot_data(slot_id_2, work_date_2, "13:00-17:00 下午班", True, 3)
        
        print("Redis 初始化完成。")
    else:
        print("Redis 資料庫已存在資料，跳過初始化。")


def _create_slot_data(slot_id, work_date, slot_name, is_open, capacity):
    """私有函式：創建班別的基礎數據和 ZSET 索引"""
    
    r.hset(f'slot:{slot_id}', mapping={
        'id': slot_id,
        'work_date': work_date,
        'slot_name': slot_name,
        'is_open': '1' if is_open else '0',
        'capacity': str(capacity)
    })
    
    if is_open:
        try:
            timestamp = datetime.datetime.fromisoformat(work_date).timestamp()
        except ValueError:
            timestamp = time.time()
        r.zadd('open_slots_set', {slot_id: timestamp})
    
    # 初始化計數器 (String) 和成員 Set
    r.set(f'slot:{slot_id}:count', 0)
    r.delete(f'slot:{slot_id}:members') 


# --- 管理員功能 (略) ---

def get_admin_user(username):
    """查詢管理員帳號"""
    user_data = r.hgetall(f'admin_user:{username}')
    if user_data:
        return {k: v for k, v in user_data.items()}
    return None

def get_all_slots_for_admin():
    """管理員儀表板：獲取所有班別及報名人數 (使用 Pipeline 加速)"""
    
    all_slot_keys = r.keys('slot:*')
    main_slot_keys = [k for k in all_slot_keys if len(k.split(':')) == 2]
    
    if not main_slot_keys:
        return []

    pipe = r.pipeline()
    for key in main_slot_keys:
        slot_id = key.split(':')[1]
        pipe.hgetall(key)             
        pipe.get(f'slot:{slot_id}:count') 
        
    results = pipe.execute()
    
    slots_data = []
    
    for i in range(0, len(results), 2):
        slot = results[i]
        count = results[i+1]
        
        if slot:
            slot['is_open'] = slot.get('is_open') == '1'
            slot['capacity'] = int(slot.get('capacity', 0))
            slot['count'] = int(count) if count is not None else 0 
            slot['bookings'] = get_bookings_for_slot(slot['id'])[:3] 
            slots_data.append(slot)
            
    slots_data.sort(key=lambda s: s['work_date'])
    return slots_data

# --- 前台功能 (略) ---

def get_open_slots():
    """前台首頁：獲取所有開放報名的班別 (使用 Sorted Set 排序)"""
    
    slot_ids = r.zrange('open_slots_set', 0, -1) 
    
    if not slot_ids:
        return []
        
    pipe = r.pipeline()
    for slot_id in slot_ids:
        pipe.hgetall(f'slot:{slot_id}')
        
    results = pipe.execute()
    
    slots_list = []
    for slot in results:
        if slot and slot.get('is_open') == '1':
            slots_list.append((slot.get('work_date'), slot.get('slot_name'), slot.get('id')))
            
    return slots_list


def get_slot_by_id(slot_id):
    """通過 ID 獲取班別資訊"""
    slot = r.hgetall(f'slot:{slot_id}')
    if slot:
        slot['id'] = slot_id
        slot['is_open'] = slot.get('is_open') == '1'
        slot['capacity'] = int(slot.get('capacity', 0))
        slot['count'] = int(r.get(f'slot:{slot_id}:count') or 0)
        return slot
    return None


def signup_person(slot_id, name, id_last_4):
    """
    報名人員 (已修正容量檢查錯誤)
    回傳值: True (成功報名), False (已滿), "DUP" (重複報名)
    """
    slot = get_slot_by_id(slot_id)
    if not slot or not slot['is_open']:
        return False

    capacity = slot['capacity']
    booking_key = f'slot:{slot_id}:bookings'
    members_key = f'slot:{slot_id}:members'

    # 1. O(1) 檢查是否重複報名 (預檢查)
    if r.sismember(members_key, id_last_4):
        return "DUP"

    # 2. 使用 WATCH/MULTI/EXEC 確保容量檢查和報名是原子性的 (交易)
    with r.pipeline() as pipe:
        while True:
            try:
                # 監視計數器和成員集合
                pipe.watch(f'slot:{slot_id}:count', members_key)
                
                # ⚡️ 關鍵修正：使用 r.get() 讀取計數器的實際值，而不是 pipe.get()
                current_count = int(r.get(f'slot:{slot_id}:count') or 0)
                
                # 再次檢查重複報名 (原子性檢查)
                if r.sismember(members_key, id_last_4):
                     pipe.unwatch()
                     return "DUP"

                if current_count >= capacity:
                    pipe.unwatch()
                    return False # 已滿

                # 建立報名數據
                employee_id = secrets.token_urlsafe(10)
                booking_data = {
                    'employee_id': employee_id,
                    'name': name,
                    'id_last_4': id_last_4,
                    'booking_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                
                # 開始交易
                pipe.multi()
                pipe.incr(f'slot:{slot_id}:count')
                pipe.rpush(booking_key, json.dumps(booking_data))
                pipe.sadd(members_key, id_last_4)
                
                # 執行交易
                pipe.execute()
                return True
                
            except redis.exceptions.WatchError:
                continue
            except Exception as e:
                print(f"Signup error: {e}")
                # 為了安全，將可能已經開始的交易取消
                try:
                    pipe.reset()
                except:
                    pass
                return False


def get_bookings_for_slot(slot_id):
    """獲取特定班別的所有報名名單 (使用 List)"""
    booking_key = f'slot:{slot_id}:bookings'
    bookings_json = r.lrange(booking_key, 0, -1)
    
    bookings_list = []
    for booking_json in bookings_json:
        bookings_list.append(json.loads(booking_json))
        
    return bookings_list

# --- 管理員 CRUD (略) ---

def update_slot(slot_id, new_work_date, new_slot_name, new_is_open, capacity):
    """更新班別資訊 (使用 Hash, ZSET)"""
    
    update_data = {
        'work_date': new_work_date,
        'slot_name': new_slot_name,
        'is_open': '1' if new_is_open else '0',
        'capacity': str(capacity)
    }
    r.hset(f'slot:{slot_id}', mapping=update_data)
    
    try:
        timestamp = datetime.datetime.fromisoformat(new_work_date).timestamp()
    except ValueError:
        timestamp = time.time()
        
    if new_is_open:
        r.zadd('open_slots_set', {slot_id: timestamp})
    else:
        r.zrem('open_slots_set', slot_id)


def delete_slot(slot_id):
    """刪除班別及其所有相關數據"""
    pipe = r.pipeline()
    pipe.delete(f'slot:{slot_id}')              # 刪除 Hash
    pipe.delete(f'slot:{slot_id}:count')        # 刪除計數器
    pipe.delete(f'slot:{slot_id}:bookings')     # 刪除報名名單 List
    pipe.delete(f'slot:{slot_id}:members')      # 刪除成員集合 Set
    pipe.zrem('open_slots_set', slot_id)        # 刪除 ZSET 索引
    pipe.execute()
    
    
def delete_booking(slot_id, employee_id):
    """刪除特定報名者 (使用 List 的 LREM，並減少計數器)"""
    
    booking_key = f'slot:{slot_id}:bookings'
    members_key = f'slot:{slot_id}:members' 
    
    bookings = get_bookings_for_slot(slot_id)
    target_booking_json = None
    target_id_last_4 = None
    
    for booking in bookings:
        if booking['employee_id'] == employee_id:
            target_booking_json = json.dumps(booking)
            target_id_last_4 = booking['id_last_4']
            break

    if target_booking_json:
        pipe = r.pipeline()
        pipe.decr(f'slot:{slot_id}:count')
        pipe.lrem(booking_key, 1, target_booking_json)
        
        if target_id_last_4:
            pipe.srem(members_key, target_id_last_4)
        
        pipe.execute()
        return True
    
    return False

def create_new_slot(work_date, slot_name, is_open, capacity):
    """新增一個班別"""
    slot_id = secrets.token_urlsafe(8)
    _create_slot_data(slot_id, work_date, slot_name, is_open, capacity)
    return slot_id