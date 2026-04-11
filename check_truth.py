import sqlite3
import json

db_path = "/home/stellaradmin/my_app/stellar_local.db"

def check_truth():
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Find the chat
    cursor.execute("SELECT id, name FROM chats ORDER BY created_at DESC LIMIT 10")
    chats = cursor.fetchall()
    print("Recent Chats:")
    target_chat_id = None
    for chat in chats:
        print(f"ID: {chat['id']} | Name: {chat['name']}")
        if "Firebase" in chat['name']:
            target_chat_id = chat['id']

    if not target_chat_id:
        print("\nCould not find 'Firebase Keys Lab' chat.")
        return

    print(f"\nChecking tool history for Chat ID: {target_chat_id}...")
    cursor.execute("SELECT tool_name, input_params, result, timestamp FROM tool_calls WHERE chat_id = ? ORDER BY timestamp ASC", (target_chat_id,))
    calls = cursor.fetchall()

    if not calls:
        print("NO TOOL CALLS FOUND for this chat. The model might be hallucinating the technical details.")
    else:
        print(f"Found {len(calls)} tool calls:")
        for i, call in enumerate(calls):
            print(f"\n[{i+1}] {call['timestamp']} - Tool: {call['tool_name']}")
            print(f"Input: {call['input_params']}")
            # print(f"Result: {call['result'][:200]}...")

if __name__ == "__main__":
    check_truth()
