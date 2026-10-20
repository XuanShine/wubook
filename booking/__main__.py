import uvicorn
from dotenv import load_dotenv

from booking import Counter
from booking.guest import guest_app


def main() -> None:
    load_dotenv()
    uvicorn.run(guest_app(Counter.open()), host="127.0.0.1", port=8071)


if __name__ == "__main__":
    main()
