# import memcache
import memcache_o as memcache


def get_value():
    mc = memcache.Client(['127.0.0.1:11211'], debug=0)

    mc.set("some_key", "Some value")
    value = mc.get("some_key")

    mc.set("another_key", 3)
    mc.delete("another_key")

    mc.set("key", "1")  # key used for incr/decr must be string.
    mc.incr("key")
    mc.decr("key")
    return value

# def memcache_db():
#     # The standard way to use memcache with a database is like this:

#     key = derive_key(obj)
#     obj = mc.get(key)
#     if not obj:
#         obj = backend_api.get(...)
#         mc.set(key, obj)

    # we now have obj, and future passes through this code
    # will use the object from the cache.


def main():
    print(get_value())

if __name__ == '__main__':
    main()
