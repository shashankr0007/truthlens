# Run with the server up:  python run_tests.py
import csv, uuid, sqlite3, requests
con = sqlite3.connect("chat.db")
for row in csv.DictReader(open("test_set.csv", encoding="utf-8")):
    r = requests.post("http://127.0.0.1:8000/api/chat", json={"session_id": str(uuid.uuid4()), "message": row["query"]}).json()
    ans = r.get("reply", str(r)); ok = int(row["expected"].lower() in ans.lower())
    con.execute("INSERT INTO test_results(query,expected,actual,correct,latency_ms) VALUES(?,?,?,?,?)",
                (row["query"], row["expected"], ans, ok, r.get("latency_ms", 0)))
    print("PASS" if ok else "FAIL", "|", row["query"])
con.commit()
