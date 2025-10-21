from flask import Flask
from flask_cors import CORS

def create_app():
    app = Flask(__name__)
    
    CORS(
        app,
        resources={r"/*": {"origins": ["https://georisk.netlify.app"]}},
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization"],
        methods=["GET", "POST", "OPTIONS"]
    )
    
    from .backend import bp as afq_bp
    from .firealg import bp as firealg_bp
    app.register_blueprint(afq_bp)
    app.register_blueprint(firealg_bp)
    
    return app
