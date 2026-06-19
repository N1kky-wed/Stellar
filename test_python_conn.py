import socket
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(2.0)
print("Connecting...")
try:
    sock.connect(("127.0.0.1", 9090))
    print("Connected successfully!")
    sock.sendall(b'{"node_id": "python_tcp_test", "latency_ns": 999}\n')
    print("Sent!")
except Exception as e:
    import traceback
    traceback.print_exc()
finally:
    sock.close()
