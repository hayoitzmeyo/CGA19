from flask import Flask
from flask_cors import CORS

def create_app():
    app = Flask(__name__)
    CORS(app, origins=["https://cga19.netlify.app"])
    from .backend import bp as afq_bp
    from .firealg import bp as firealg_bp
    app.register_blueprint(afq_bp)
    app.register_blueprint(firealg_bp)
    return app

