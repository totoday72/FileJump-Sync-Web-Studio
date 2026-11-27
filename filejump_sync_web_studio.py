import threading
import webbrowser
from flask import Flask
from config_manager import log_event
from routes_api import api_bp

app = Flask(__name__)
app.register_blueprint(api_bp)

if __name__ == "__main__":
    host = "127.0.0.1"
    port = 5000
    url = f"http://{host}:{port}"
    log_event("info", f"Iniciando FileJump Sync Studio en {url}")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=port, debug=False, threaded=True)
