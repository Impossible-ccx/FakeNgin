import os

from webapp import create_app

app = create_app()

if __name__ == "__main__":
    # debug 仅在显式设置 FLASK_DEBUG=1 时开启（开发环境用）
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", port=5000)
