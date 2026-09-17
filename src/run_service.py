"""Run the FastAPI Agent Service using the toolkit-style entrypoint."""
import uvicorn


def main() -> None:
    uvicorn.run("app.api.main:app", host="0.0.0.0", port=8000, factory=False)


if __name__ == "__main__":
    main()
