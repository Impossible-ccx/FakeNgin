from flask import Flask, jsonify


def create_app():
    app = Flask(__name__, static_folder="/web/static", template_folder="/web/template")

    @app.route("/")
    def index():
        return jsonify({"message": "Hello, Flask!"})

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
