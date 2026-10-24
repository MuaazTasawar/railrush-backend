import platform
import time

for run in range(3):
    started = time.perf_counter()
    total = 0
    for i in range(2000000):
        total += i * i
    print("run %d: %.3f s" % (run + 1, time.perf_counter() - started))
print("python %s %s" % (platform.python_version(), platform.machine()))
