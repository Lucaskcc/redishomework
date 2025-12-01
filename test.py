import redis
 
myhost = 'localhost'
r = redis.Redis(host=myhost, port=6379, db=0, decode_responses=True)
 
# 字符串 Strings
r.set('test', 'aaa')
print('[STRING] test:', r.get('test'))
 
# 列表 Lists
for x in range(0, 11):
    r.lpush('list', x)
print('[LIST] list:', r.lrange('list', '0', '10'))
 
# 雜湊 Hashes
dict_hash = {'name': 'tang', 'password': 'tang_passwd'}
#r.hset('hash_test', 'name', 'tang')
#r.hset('hash_test', 'password', 'tang_passwd')
r.hset('hash_test', mapping=dict_hash)
print('[HASH] hash_test:', r.hgetall('hash_test'))
 
# 集合 Sets
r.sadd('set_test', 'aaa', 'bbb')
r.sadd('set_test', 'ccc')
r.sadd('set_test', 'ddd')
print('[SET] set_test:', r.smembers('set_test'))
 
# 有序集 Sorted sets
r.zadd('zset_test', {'aaa': 1, 'bbb': 1})
r.zadd('zset_test', {'ccc': 1})
r.zadd('zset_test', {'ddd': 1})
print('[Sorted SET] zset_test:', r.zrange('zset_test', 0, 10))