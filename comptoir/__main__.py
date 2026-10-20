import uvicorn

from comptoir.app import create_app
from comptoir.desk import Desk


def main() -> None:
    desk = Desk()
    app = create_app(desk)
    try:
        uvicorn.run(app, host="127.0.0.1", port=8071)
    finally:
        desk.close()


if __name__ == "__main__":
    main()
