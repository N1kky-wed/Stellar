import time
from AngelTrace import tracer, active_trace_id

print("Sending python direct trace...")
# Set an active trace ID context
token = active_trace_id.set("python_trace_123")
try:
    tracer.send_trace("test_python_direct", 88888)
finally:
    active_trace_id.reset(token)

time.sleep(1.0)
print("Done")
