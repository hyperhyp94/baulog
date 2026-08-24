from flask import Flask, request, jsonify, send_from_directory
import json, os

app = Flask(__name__, static_folder=".")
DATA = os.path.join(os.path.dirname(__file__), "data", "data.json")

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/api/data", methods=["GET"])
def get_data():
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    if not os.path.exists(DATA):
        return jsonify([])
    with open(DATA, encoding="utf-8") as f:
        return jsonify(json.load(f))

@app.route("/api/data", methods=["POST"])
def save_data():
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(request.get_json(), f, ensure_ascii=False, indent=2)
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("TOOL_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
